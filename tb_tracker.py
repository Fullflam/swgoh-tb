import requests
import os
import json
import re
from collections import defaultdict
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

COMLINK_URL = os.environ.get("COMLINK_URL", "https://swgoh-comlink-latest-13vg.onrender.com")
ALLY_CODE = os.environ.get("ALLY_CODE", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
DRIVE_FOLDER_ID = "1d8uIyrLSLl4F9Ro3mXf8DrAPezDZF0A0"
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO_TRACKER = "Fullflam/swgoh-tracker"

def get_drive_service():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)

def lire_jsons_drive():
    service = get_drive_service()
    date_aujd = datetime.now().strftime("%Y-%m-%d")
    
    results = service.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/json' and name contains '{date_aujd}'",
        fields="files(id, name)",
        orderBy="name"
    ).execute()
    fichiers = results.get("files", [])
    
    if not fichiers:
        print(f"Aucun fichier trouvé pour la date {date_aujd}")
        return []
    
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
    membres = {m.get("playerId"): {"nom": m.get("playerName"), "allyCode": ""} for m in guilde.get("member", [])}
    ally_to_pid = get_ally_to_pid()
    return membres, ally_to_pid

def analyser_ops(ops, membres, ally_to_pid):
    
    assignations = defaultdict(lambda: defaultdict(list))
    
    for op in ops:
        filename = op.get("_filename", "")
        phase_match = re.search(r'P(\d+)', filename)
        phase = int(phase_match.group(1)) if phase_match else 0
        
        for a in op.get("platoonAssignments", []):
            ally_code = str(a.get("allyCode", ""))
            unit = a.get("unitBaseId", "")
            assignations[phase][ally_code].append(unit)
    
    return assignations

def envoie_discord(membres, ally_to_pid, assignations):
    aujd = datetime.now().strftime("%d/%m/%Y")
    lignes = [f"# test - Assignations WookieeBot — {aujd}"]
    lignes.append(f"> {sum(len(v) for p in assignations.values() for v in p.values())} assignations au total\n")

    for phase in sorted(assignations.keys()):
        lignes.append(f"**Phase {phase}**")
        for ally_code, unites in sorted(assignations[phase].items()):
            pid = ally_to_pid.get(ally_code)
            nom = membres[pid]["nom"] if pid else f"AllyCode {ally_code}"
            unites_str = ", ".join(unites)
            lignes.append(f"• {nom} → {unites_str}")
        lignes.append("")

    message = "\n".join(lignes)
    if len(message) > 1900:
        chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
        for chunk in chunks:
            requests.post(DISCORD_WEBHOOK, json={"content": chunk})
    else:
        requests.post(DISCORD_WEBHOOK, json={"content": message})
    print("Message envoyé !")

if __name__ == "__main__":
    print(f"=== TB Tracker {datetime.now().strftime('%d/%m/%Y %H:%M')} ===")
    ops = lire_jsons_drive()
    if not ops:
        print("Aucun fichier trouvé dans Drive !")
    else:
        membres, ally_to_pid = get_guild_data()
        assignations = analyser_ops(ops, membres, ally_to_pid)
        envoie_discord(membres, ally_to_pid, assignations)
