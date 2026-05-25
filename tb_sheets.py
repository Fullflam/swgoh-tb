import requests
import os
import json
import re
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from collections import defaultdict
import io
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

COMLINK_URL = os.environ.get("COMLINK_URL", "https://swgoh-comlink-latest-13vg.onrender.com")
ALLY_CODE = os.environ.get("ALLY_CODE", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
SHEET_ID = "1A7eqze-H4bqjgfTg4JrDNlEqNu57LBdTjl77mlo_Vbs"
DRIVE_FOLDER_ID = "1d8uIyrLSLl4F9Ro3mXf8DrAPezDZF0A0"

def get_gspread_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
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

def get_guild_data():
    joueur = comlink_post("/player", {"allyCode": ALLY_CODE})
    id_guilde = joueur.get("guildId")
    brut = comlink_post("/guild", {"guildId": id_guilde, "includeRecentGuildActivityInfo": True})
    guilde = brut.get("guild", brut)
    membres = {}
    ally_to_pid = {}
    for m in guilde.get("member", []):
        pid = m.get("playerId")
        nom = m.get("playerName")
        try:
            profil = comlink_post("/player", {"playerId": pid})
            ally_code = str(profil.get("allyCode", ""))
        except Exception as e:
            print(f"Erreur pour {nom}: {e}")
            ally_code = ""
        membres[pid] = {"nom": nom, "allyCode": ally_code}
        if ally_code:
            ally_to_pid[ally_code] = pid
        print(f"Récupéré : {nom} → {ally_code}")
    return membres, ally_to_pid

def lire_jsons_drive():
    service = get_drive_service()
    # Cherche les fichiers les plus récents — 7 derniers jours
    dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    
    tous_fichiers = []
    for date in dates:
        results = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/json' and name contains '{date}'",
            fields="files(id, name)",
            orderBy="name"
        ).execute()
        fichiers = results.get("files", [])
        if fichiers:
            tous_fichiers = fichiers
            print(f"Fichiers trouvés pour {date}")
            break

    if not tous_fichiers:
        print("Aucun fichier trouvé dans les 7 derniers jours")
        return []

    ops = []
    for fichier in tous_fichiers:
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

def analyser_ops(ops, membres, ally_to_pid):
    assignations = defaultdict(lambda: defaultdict(list))
    for op in ops:
        filename = op.get("_filename", "")
        phase_match = re.search(r'P(\d+)', filename)
        phase = int(phase_match.group(1)) if phase_match else 0
        for a in op.get("platoonAssignments", []):
            ally_code = str(a.get("allyCode", ""))
            unit = a.get("unitBaseId", "")
            pid = ally_to_pid.get(ally_code)
            if pid:
                assignations[phase][pid].append(unit)
    return assignations

def update_sheet(membres, assignations):
    client = get_gspread_client()
    wb = client.open_by_key(SHEET_ID)

    for phase in sorted(assignations.keys()):
        nom_onglet = f"Phase {phase}"

        try:
            ws = wb.worksheet(nom_onglet)
            # Vérifie si déjà rempli
            valeur_a2 = ws.acell("A2").value
            if valeur_a2:
                print(f"Onglet '{nom_onglet}' déjà rempli, on skip.")
                continue
        except gspread.exceptions.WorksheetNotFound:
            ws = wb.add_worksheet(title=nom_onglet, rows=100, cols=50)

        # Entêtes
        entetes = ["Pseudo", "PlayerId", "Erreurs totales"]
        # Trouve le max d'unités assignées pour dimensionner les colonnes
        max_unites = max((len(u) for u in assignations[phase].values()), default=0)
        for i in range(1, max_unites + 1):
            entetes.append(f"Unité {i}")

        ws.update([entetes], "A1", value_input_option="RAW")

        # Données
        lignes = []
        for pid, infos in sorted(membres.items(), key=lambda x: x[1]["nom"].lower()):
            unites = assignations[phase].get(pid, [])
            # TODO: erreurs totales — à remplir quand TB active
            ligne = [infos["nom"], pid, "TODO"] + unites + [""] * (max_unites - len(unites))
            lignes.append(ligne)

        ws.update(lignes, "A2", value_input_option="RAW")

        # Cache la colonne B (PlayerId)
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
    membres, ally_to_pid = get_guild_data()
    ops = lire_jsons_drive()
    if ops:
        assignations = analyser_ops(ops, membres, ally_to_pid)
        update_sheet(membres, assignations)
    else:
        print("Aucun fichier WookieeBot trouvé.")
