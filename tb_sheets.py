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
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO_TRACKER = "Fullflam/swgoh-tracker"
DRIVE_FOLDER_ID = "1d8uIyrLSLl4F9Ro3mXf8DrAPezDZF0A0"
SHEET_ID = "1A7eqze-H4bqjgfTg4JrDNlEqNu57LBdTjl77mlo_Vbs"

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
    res = requests.post(
        f"{COMLINK_URL}{endpoint}",
        json={"payload": payload, "enums": False},
        timeout=30
    )
    res.raise_for_status()
    return res.json()

def get_ally_to_pid():
    url = f"https://api.github.com/repos/{GH_REPO_TRACKER}/contents/ally_to_pid.json"
    res = requests.get(url, headers={"Authorization": f"token {GH_TOKEN}"})
    if res.status_code != 200:
        print("Impossible de lire ally_to_pid.json")
        return {}
    contenu = base64.b64decode(res.json()["content"]).decode("utf-8")
    return json.loads(contenu)

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
        phase = int(phase_match.group(1))
        zone = int(conflict_match.group(1))
        for ps in stat.get("playerStat", []):
            pid = ps.get("memberId")
            score = int(ps.get("score", 0))
            deploiements[phase][pid][zone] += score
    return deploiements

def update_sheet(membres, assignations, zones_par_phase, deploiements):
    client = get_gspread_client()
    wb = client.open_by_key(SHEET_ID)

    for phase in sorted(assignations.keys()):
        nom_onglet = f"Phase {phase}"
        zones = sorted(zones_par_phase[phase])

        try:
            ws = wb.worksheet(nom_onglet)
            valeur_a2 = ws.acell("A2").value
            if valeur_a2:
                print(f"Onglet '{nom_onglet}' déjà rempli, on skip.")
                continue
        except gspread.exceptions.WorksheetNotFound:
            ws = wb.add_worksheet(title=nom_onglet, rows=100, cols=50)

        # Entêtes
        entetes = ["Pseudo", "PlayerId", "Total assigné", "Total déployé"]
        for zone in zones:
            entetes.append(f"Zone {zone} assigné")
            entetes.append(f"Zone {zone} déployé")
        ws.update([entetes], "A1", value_input_option="RAW")

        # Données
        lignes = []
        for pid, nom in sorted(membres.items(), key=lambda x: x[1].lower()):
            total_assigne = sum(assignations[phase][pid].values())
            total_deploye = sum(deploiements[phase][pid].values())
            ligne = [nom, pid, total_assigne, total_deploye]
            for zone in zones:
                ligne.append(assignations[phase][pid].get(zone, 0))
                ligne.append(deploiements[phase][pid].get(zone, 0))
            lignes.append(ligne)

        ws.update(lignes, "A2", value_input_option="RAW")
        # Colorier les pseudos
        format_requests = []
        for i, (pid, nom) in enumerate(sorted(membres.items(), key=lambda x: x[1].lower())):
            row = i + 2
            total_assigne = sum(assignations[phase][pid].values())
            total_deploye = sum(deploiements[phase][pid].values())

            if total_assigne > 0 and total_deploye < total_assigne:
                # Rouge — n'a pas tout déployé
                couleur = {"red": 0.89, "green": 0.27, "blue": 0.27}
            elif total_assigne > 0:
                # Vert — a tout déployé
                couleur = {"red": 0.30, "green": 0.69, "blue": 0.51}
            else:
                # Gris — pas assigné
                couleur = {"red": 0.9, "green": 0.9, "blue": 0.9}

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
        ops = lire_jsons_drive()
        if ops:
            assignations, zones_par_phase = analyser_assignations(ops, ally_to_pid)
            deploiements = analyser_deploiements(tb)
            update_sheet(membres, assignations, zones_par_phase, deploiements)
        else:
            print("Aucun fichier WookieeBot trouvé.")
