import requests
import os
import re
import json
import base64
import gspread
from collections import defaultdict
from datetime import datetime
from google.oauth2.service_account import Credentials

COMLINK_URL = os.environ.get("COMLINK_URL", "")
ALLY_CODE = os.environ.get("ALLY_CODE", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO_TRACKER = "Fullflam/swgoh-tracker"
GH_REPO_TB = "Fullflam/swgoh-tb"
GH_WOOKIE_PATH = "wookieebot"
SHEET_ID = "1ascT5K_knXHzLi5qhCjX0B24-DSopFPMZ6n_jKnGiGY"

def get_gspread_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds)

def comlink_post(endpoint, payload):
    for tentative in range(3):
        try:
            res = requests.post(
                f"{COMLINK_URL}{endpoint}",
                json={"payload": payload, "enums": False},
                timeout=60
            )
            res.raise_for_status()
            return res.json()
        except Exception as e:
            print(f"Tentative {tentative + 1} échouée : {e}")
            if tentative < 2:
                import time
                time.sleep(10)
    raise Exception(f"Échec après 3 tentatives sur {endpoint}")

def get_ally_to_pid():
    url = f"https://api.github.com/repos/{GH_REPO_TRACKER}/contents/ally_to_pid.py"
    res = requests.get(url, headers={"Authorization": f"token {GH_TOKEN}"})
    if res.status_code != 200:
        print("Impossible de lire ally_to_pid.py")
        return {}
    contenu = base64.b64decode(res.json()["content"]).decode("utf-8")
    local_vars = {}
    exec(contenu, local_vars)
    return local_vars.get("ALLY_TO_PID", {})

def get_guild_data():
    joueur = comlink_post("/player", {"allyCode": ALLY_CODE})
    id_guilde = joueur.get("guildId")
    brut = comlink_post("/guild", {"guildId": id_guilde, "includeRecentGuildActivityInfo": True})
    guilde = brut.get("guild", brut)
    membres = {m.get("playerId"): m.get("playerName") for m in guilde.get("member", [])}
    tb = guilde.get("recentTerritoryBattleResult", [])
    ally_to_pid = get_ally_to_pid()
    return membres, ally_to_pid, tb[0] if tb else None

def lire_jsons_github():
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    url = f"https://api.github.com/repos/{GH_REPO_TB}/contents/{GH_WOOKIE_PATH}"
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        print(f"Impossible de lire le dossier {GH_WOOKIE_PATH}")
        return []

    fichiers = [f for f in res.json() if f["name"].endswith(".json") and f["name"] != ".gitkeep"]
    if not fichiers:
        print("Aucun fichier WookieeBot trouvé dans le repo")
        return []

    ops = []
    for fichier in sorted(fichiers, key=lambda x: x["name"]):
        res_fichier = requests.get(fichier["download_url"], headers=headers)
        if res_fichier.status_code == 200:
            data = res_fichier.json()
            data["_filename"] = fichier["name"]
            ops.append(data)
            print(f"Lu : {fichier['name']}")

    return ops

def analyser_assignations(ops, ally_to_pid):
    # zone_id complet (ex: tb3_mixed_phase01_conflict01_recon01) au lieu du simple numéro
    assignations = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    zones_par_phase = defaultdict(set)  # phase -> set de zone_id complets
    for op in ops:
        filename = op.get("_filename", "")
        phase_match = re.search(r'P(\d+)', filename)
        phase = int(phase_match.group(1)) if phase_match else 0
        for a in op.get("platoonAssignments", []):
            ally_code = str(a.get("allyCode", ""))
            pid = ally_to_pid.get(ally_code)
            zone_id = a.get("zoneId", "")
            if pid and zone_id:
                assignations[phase][pid][zone_id] += 1
                zones_par_phase[phase].add(zone_id)
    return assignations, zones_par_phase

def analyser_deploiements(tb):
    # Clé = zone_id complet reconstruit depuis mapStatId
    deploiements = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for stat in tb.get("finalStat", []):
        map_id = stat.get("mapStatId", "")
        if "unit_donated" not in map_id:
            continue
        # mapStatId ex: tb3_mixed_phase01_conflict01_recon01_unit_donated
        # On extrait le zone_id en retirant le suffixe _unit_donated*
        zone_id_match = re.match(r'(tb3_mixed_phase\d+_conflict\d+(?:_bonus)?_recon\d+)', map_id)
        phase_match = re.search(r'phase(\d+)', map_id)
        if not zone_id_match or not phase_match:
            continue
        zone_id = zone_id_match.group(1)
        phase = int(phase_match.group(1))
        for ps in stat.get("playerStat", []):
            pid = ps.get("memberId")
            score = int(ps.get("score", 0))
            deploiements[phase][pid][zone_id] += score
    return deploiements

def analyser_summary(tb):
    summary = {}
    summary_par_phase = defaultdict(dict)
    for stat in tb.get("finalStat", []):
        map_id = stat.get("mapStatId", "")
        if map_id == "summary":
            for ps in stat.get("playerStat", []):
                summary[ps.get("memberId")] = int(ps.get("score", 0))
        elif map_id.startswith("summary_round"):
            phase_match = re.search(r'round_(\d+)', map_id)
            if phase_match:
                phase = int(phase_match.group(1))
                for ps in stat.get("playerStat", []):
                    summary_par_phase[phase][ps.get("memberId")] = int(ps.get("score", 0))
    return summary, summary_par_phase

def update_sheet(membres, assignations, zones_par_phase, deploiements, tb, summary, summary_par_phase):
    client = get_gspread_client()
    wb = client.open_by_key(SHEET_ID)

    end_time = int(tb.get("endTime", 0))
    date_tb = datetime.fromtimestamp(end_time / 1000).strftime('%d/%m/%y') if end_time else datetime.now().strftime('%d/%m/%y')
    nom_onglet = f"TB {date_tb}"
    total_stars = int(tb.get("totalStars", 0))

    try:
        ws = wb.worksheet(nom_onglet)
        print(f"Onglet '{nom_onglet}' déjà existant, skip.")
        return
    except gspread.exceptions.WorksheetNotFound:
        ws = wb.add_worksheet(title=nom_onglet, rows=500, cols=50)
        print(f"Onglet '{nom_onglet}' créé.")

    toutes_lignes = []
    format_requests = []
    row_actuelle = 1

    for phase in sorted(assignations.keys()):
        # Trie les zone_id par numéro de conflict pour un affichage cohérent
        zones = sorted(zones_par_phase[phase], key=lambda z: re.search(r'conflict(\d+)', z).group(1) if re.search(r'conflict(\d+)', z) else z)

        toutes_lignes.append([f"=== PHASE {phase} ==="])
        row_actuelle += 1

        # Entêtes : zone_id brut (traduit côté dashboard)
        entetes = ["Pseudo", "PlayerId", "Total déployé", "Total assigné", ""]
        for zone_id in zones:
            entetes.append(f"{zone_id}_deployed")
            entetes.append(f"{zone_id}_assigned")
        toutes_lignes.append(entetes)
        row_actuelle += 1

        for pid, nom in sorted(membres.items(), key=lambda x: x[1].lower()):
            total_assigne = sum(assignations[phase][pid].values())
            total_deploye = sum(deploiements[phase][pid].values())
            ligne = [nom, pid, total_deploye, total_assigne, ""]
            for zone_id in zones:
                ligne.append(deploiements[phase][pid].get(zone_id, 0))
                ligne.append(assignations[phase][pid].get(zone_id, 0))
            toutes_lignes.append(ligne)

            if total_deploye > total_assigne:
                couleur = {"red": 0.89, "green": 0.27, "blue": 0.27}
            else:
                couleur = {"red": 1, "green": 1, "blue": 1}

            format_requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": row_actuelle - 1,
                        "endRowIndex": row_actuelle,
                        "startColumnIndex": 0,
                        "endColumnIndex": 1
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": couleur
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor"
                }
            })
            row_actuelle += 1

        toutes_lignes.append([])
        row_actuelle += 1

    # Bloc récap
    toutes_lignes.append(["=== RÉCAP ==="])
    row_actuelle += 1

    entetes_recap = ["Pseudo", "PlayerId", "Score total TB"] + [f"Score Phase {p}" for p in range(1, 7)]
    toutes_lignes.append(entetes_recap)
    row_actuelle += 1

    for pid, nom in sorted(membres.items(), key=lambda x: x[1].lower()):
        ligne = [nom, pid]
        ligne.append(summary.get(pid, 0))
        for phase in range(1, 7):
            ligne.append(summary_par_phase.get(phase, {}).get(pid, 0))
        toutes_lignes.append(ligne)
        row_actuelle += 1

    ws.update(toutes_lignes, "A1", value_input_option="RAW")
    ws.update([[str(total_stars)]], "Z1", value_input_option="RAW")

    if format_requests:
        wb.batch_update({"requests": format_requests})

    wb.batch_update({
        "requests": [{
            "updateDimensionProperties": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "COLUMNS",
                    "startIndex": 1,
                    "endIndex": 2
                },
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser"
            }
        }]
    })

    print(f"Onglet '{nom_onglet}' rempli ! ({total_stars} étoiles)")

if __name__ == "__main__":
    print(f"=== TB Sheets {datetime.now().strftime('%d/%m/%Y %H:%M')} ===")
    membres, ally_to_pid, tb = get_guild_data()

    if not tb:
        print("Aucune TB récente trouvée.")
    else:
        ops = lire_jsons_github()
        deploiements = analyser_deploiements(tb)
        summary, summary_par_phase = analyser_summary(tb)

        if ops:
            assignations, zones_par_phase = analyser_assignations(ops, ally_to_pid)
            update_sheet(membres, assignations, zones_par_phase, deploiements, tb, summary, summary_par_phase)
        else:
            print("Aucun fichier WookieeBot trouvé, écriture sans assignations.")
            # Reconstruit zones_par_phase depuis les deploiements
            zones_par_phase = defaultdict(set)
            for phase, pid_data in deploiements.items():
                for pid, zone_data in pid_data.items():
                    for zone_id in zone_data.keys():
                        zones_par_phase[phase].add(zone_id)
            assignations = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
            update_sheet(membres, assignations, zones_par_phase, deploiements, tb, summary, summary_par_phase)
