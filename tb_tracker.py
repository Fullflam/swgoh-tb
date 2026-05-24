import requests
import os
import re
from collections import defaultdict
from datetime import datetime

COMLINK_URL = os.environ.get("COMLINK_URL", "https://swgoh-comlink-latest-13vg.onrender.com")
ALLY_CODE = os.environ.get("ALLY_CODE", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

def comlink_post(endpoint, payload):
    res = requests.post(
        f"{COMLINK_URL}{endpoint}",
        json={"payload": payload, "enums": False},
        timeout=30
    )
    res.raise_for_status()
    return res.json()

def get_tb_data():
    joueur = comlink_post("/player", {"allyCode": ALLY_CODE})
    id_guilde = joueur.get("guildId")
    brut = comlink_post("/guild", {"guildId": id_guilde, "includeRecentGuildActivityInfo": True})
    guilde = brut.get("guild", brut)
    membres = {m.get("playerId"): m.get("playerName") for m in guilde.get("member", [])}
    tb = guilde.get("recentTerritoryBattleResult", [])
    if not tb:
        print("Aucune TB récente trouvée.")
        return None, None
    return membres, tb[0]

def analyser_tb(membres, tb):
    total_par_joueur = defaultdict(int)
    par_phase = defaultdict(lambda: defaultdict(int))

    for stat in tb.get("finalStat", []):
        map_id = stat.get("mapStatId", "")
        if "unit_donated" not in map_id:
            continue

        phase_match = re.search(r'phase(\d+)', map_id)
        conflict_match = re.search(r'conflict(\d+)', map_id)

        for ps in stat.get("playerStat", []):
            member_id = ps.get("memberId")
            score = int(ps.get("score", 0))
            nom = membres.get(member_id, "INCONNU")

            if map_id == "unit_donated":
                total_par_joueur[nom] = score
            elif phase_match and not conflict_match:
                phase = int(phase_match.group(1))
                par_phase[phase][nom] = score

    return total_par_joueur, par_phase

def envoie_discord(guild_name, tb, membres, total_par_joueur, par_phase):
    definition_id = tb.get("definitionId", "TB inconnue")
    total_etoiles = tb.get("totalStars", 0)
    aujd = datetime.now().strftime("%d/%m/%Y")

    tous_noms = set(membres.values())
    ayant_deploye = set(total_par_joueur.keys())
    absents = sorted(tous_noms - ayant_deploye - {"INCONNU"})

    lignes = [f"# Rapport TB — {definition_id} — {aujd}"]
    lignes.append(f"> Étoiles : **{total_etoiles}** · Phases jouées : **{len(par_phase)}**\n")

    # Classement global
    lignes.append(f"**Classement global ({len(ayant_deploye)} participants)**")
    for nom, total in sorted(total_par_joueur.items(), key=lambda x: x[1], reverse=True):
        lignes.append(f"• {nom} — {total} unités")

    # Absents
    if absents:
        lignes.append(f"\n**N'ont pas déployé ({len(absents)})**")
        for nom in absents:
            lignes.append(f"• {nom}")

    # Par phase
    for phase in sorted(par_phase.keys()):
        lignes.append(f"\n**Phase {phase}**")
        for nom, score in sorted(par_phase[phase].items(), key=lambda x: x[1], reverse=True):
            lignes.append(f"• {nom} — {score} unités")

    message = "\n".join(lignes)

    # Discord limite à 2000 caractères
    if len(message) > 1900:
        chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
        for chunk in chunks:
            requests.post(DISCORD_WEBHOOK, json={"content": chunk})
    else:
        requests.post(DISCORD_WEBHOOK, json={"content": message})

    print("Rapport TB envoyé !")

if __name__ == "__main__":
    print(f"=== TB Tracker {datetime.now().strftime('%d/%m/%Y %H:%M')} ===")
    membres, tb = get_tb_data()
    if tb:
        guild_name = "Guilde"
        total_par_joueur, par_phase = analyser_tb(membres, tb)
        envoie_discord(guild_name, tb, membres, total_par_joueur, par_phase)
