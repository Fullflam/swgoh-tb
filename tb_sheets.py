import requests
import os
import re
import json
import base64
import io
import gspread
from collections import defaultdict
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.service_account import Credentials

COMLINK_URL = os.environ.get("COMLINK_URL", "https://swgoh-comlink-latest-13vg.onrender.com")
ALLY_CODE = os.environ.get("ALLY_CODE", "")
#ALLY_CODE = "492133242"
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO_TRACKER = "Fullflam/swgoh-tracker"
DRIVE_FOLDER_ID = "1d8uIyrLSLl4F9Ro3mXf8DrAPezDZF0A0"
SHEET_ID = "1A7eqze-H4bqjgfTg4JrDNlEqNu57LBdTjl77mlo_Vbs"

def debug_tb_phases(tb):
    print("\n=== DEBUG UNIT_DONATED MAP IDS ===")

    phases = set()

    for stat in tb.get("finalStat", []):
        map_id = stat.get("mapStatId", "")

        if "unit_donated" not in map_id:
            continue

        print(map_id)

        phase_match = re.search(r"phase(\d+)", map_id)
        if phase_match:
            phases.add(int(phase_match.group(1)))

    print("\nPhases trouvées :", sorted(phases))
    
def get_gspread_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds)

def get_drive_service():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)

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

def lire_jsons_drive():
    service = get_drive_service()
    for i in range(14):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        results = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/json' and name contains '{date}'",
            fields="files(id, name)",
            orderBy="name"
        ).execute()
        fichiers = results.get("files", [])
        if fichiers:
            print(f"Fichiers trouvés pour {date}")
            ops = []
            for fichier in fichiers:
                request = service.files().get_media(fileId=fichier["id"])
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                fh.seek(0)
                data = json.load(fh)
                data["_filename"] = fichier["name"]
                ops.append(data)
                print(f"Lu : {fichier['name']}")
            return ops
    print("Aucun fichier trouvé dans les 14 derniers jours")
    return []

def analyser_assignations(ops, ally_to_pid):
    assignations = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    zones_par_phase = defaultdict(set)
    for op in ops:
        filename = op.get("_filename", "")
        phase_match = re.search(r'P(\d+)', filename)
        phase = int(phase_match.group(1)) if phase_match else 0
        for a in op.get("platoonAssignments", []):
            ally_code = str(a.get("allyCode", ""))
            pid = ally_to_pid.get(ally_code)
            zone_id = a.get("zoneId", "")
            conflict_match = re.search(r'conflict(\d+)', zone_id)
            zone = int(conflict_match.group(1)) if conflict_match else 0
            if pid:
                assignations[phase][pid][zone] += 1
                zones_par_phase[phase].add(zone)
    return assignations, zones_par_phase

def analyser_deploiements(tb):
    deploiements = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for stat in tb.get("finalStat", []):
        map_id = stat.get("mapStatId", "")
        if "unit_donated" not in map_id:
            continue
        phase_match = re.search(r'phase(\d+)', map_id)
        conflict_match = re.search(r'conflict(\d+)', map_id)
        if not phase_match or not conflict_match:
            continue
        print(f"{map_id} → {len(stat.get('playerStat', []))} playerStats")
        phase = int(phase_match.group(1))
        zone = int(conflict_match.group(1))
        for ps in stat.get("playerStat", []):
            pid = ps.get("memberId")
            score = int(ps.get("score", 0))
            deploiements[phase][pid][zone] += score
    return deploiements

def clear_sheet(ws):
    """Efface tout le contenu de l'onglet et remet les colonnes en visible."""
    ws.clear()
    # Remettre la colonne B (PlayerId) visible avant de re-cacher après écriture
    print(f"Onglet '{ws.title}' nettoyé.")

def update_sheet(membres, assignations, zones_par_phase, deploiements, wb):
    for phase in sorted(assignations.keys()):
        nom_onglet = f"Phase {phase}"
        zones = sorted(zones_par_phase[phase])

        # Récupère ou crée l'onglet
        try:
            ws = wb.worksheet(nom_onglet)
        except gspread.exceptions.WorksheetNotFound:
            ws = wb.add_worksheet(title=nom_onglet, rows=100, cols=50)

        # Nettoie l'onglet avant d'écrire
        clear_sheet(ws)

        # Entêtes
        entetes = ["Pseudo", "PlayerId", "Total déployé", "Total assigné", ""]
        for zone in zones:
            entetes.append(f"Zone {zone} déployé")
            entetes.append(f"Zone {zone} assigné")
        ws.update([entetes], "A1", value_input_option="RAW")

        # Données
        lignes = []
        for pid, nom in sorted(membres.items(), key=lambda x: x[1].lower()):
            total_assigne = sum(assignations[phase][pid].values())
            total_deploye = sum(deploiements[phase][pid].values())
            ligne = [nom, pid, total_deploye, total_assigne, ""]
            for zone in zones:
                ligne.append(deploiements[phase][pid].get(zone, 0))
                ligne.append(assignations[phase][pid].get(zone, 0))
            lignes.append(ligne)

        ws.update(lignes, "A2", value_input_option="RAW")

        # Colorier les pseudos
        format_requests = []
        for i, (pid, nom) in enumerate(sorted(membres.items(), key=lambda x: x[1].lower())):
            row = i + 2
            total_assigne = sum(assignations[phase][pid].values())
            total_deploye = sum(deploiements[phase][pid].values())

            if total_deploye > total_assigne:
                couleur = {"red": 0.89, "green": 0.27, "blue": 0.27}
            else:
                couleur = {"red": 1, "green": 1, "blue": 1}

            format_requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": row - 1,
                        "endRowIndex": row,
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

        if format_requests:
            wb.batch_update({"requests": format_requests})

        # Cache PlayerId
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

        print(f"Onglet '{nom_onglet}' rempli !")

if __name__ == "__main__":
    print(f"=== TB Sheets {datetime.now().strftime('%d/%m/%Y %H:%M')} ===")
    membres, ally_to_pid, tb = get_guild_data()

    if not tb:
        print("Aucune TB récente trouvée.")
    else:
        debug_tb_phases(tb)

        ops = lire_jsons_drive()

        if ops:
            assignations, zones_par_phase = analyser_assignations(ops, ally_to_pid)
            deploiements = analyser_deploiements(tb)
            client = get_gspread_client()
            wb = client.open_by_key(SHEET_ID)
            update_sheet(membres, assignations, zones_par_phase, deploiements, wb)
        else:
            print("Aucun fichier WookieeBot trouvé.")
