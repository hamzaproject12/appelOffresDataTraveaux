"""Deuxième reconnaissance : la liste des annonces d'extrait de PV, et le détail par lot.

La première sonde a montré deux choses. Les pages « ExtraitPV » dont on ne tire rien sont des
coquilles vides — 54 640 octets de menu, pas une ligne de contenu : le procès-verbal n'y est pas,
il est en PDF attaché à l'ANNONCE, qui est une autre page. Et le détail par lot s'ouvre par une
adresse toute simple, « commun.PopUpDetailLots ».

Ce programme va chercher ces deux pages-là, les conserve et décrit ce qu'elles contiennent. Il
n'écrit rien dans les dossiers de production et ne modifie aucune donnée.

    cd pv_travaux\\site
    python sonder_pv_et_lots.py                 les deux reconnaissances
    python sonder_pv_et_lots.py --lots 12       seulement le détail par lot
    python sonder_pv_et_lots.py --annonces      seulement la liste des annonces de PV

Puis envoie-moi le dossier « sondage2\\ », ou dis-moi seulement qu'il est prêt : je sais le lire
depuis ton disque.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from html import unescape
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
import telecharger_pv as tp                                  # noqa: E402
import collecter_consultations as cc                         # noqa: E402

DONNEES = ICI.parent
SORTIE = ICI / "sondage2"
NB = re.compile(r"(\d+)")

# L'adresse du détail par lot, lue dans le bouton « plus de détails » d'une page d'extrait.
# Le portail écrit « orgAccronyme » avec deux c ici, et « orgAcronyme » partout ailleurs.
POPUP = (tp.BASE + "?page=commun.PopUpDetailLots"
                   "&orgAccronyme={org}&refConsultation={ref}&lang=fr")

# La recherche des annonces d'extrait de PV : la liste qui fait foi, 34 556 pour les Travaux.
URL_ANNONCES = tp.BASE + "?page=entreprise.EntrepriseAdvancedSearch&AvisExtraitPV"


def propre(texte: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", texte or ""))).strip()


def decrire(html: str, titre: str) -> None:
    """Ce qu'il y a dans la page : titres de colonnes, liens, boutons, taille."""
    entetes = [propre(m) for m in re.findall(r"<th[^>]*>(.*?)</th>", html, re.S | re.I)]
    entetes = [e for e in dict.fromkeys(entetes) if e]
    liens = []
    for cible, texte in re.findall(r"""<a\b[^>]*?(?:href|onclick)\s*=\s*["']([^"']{4,250})["']"""
                                   r"""[^>]*>(.{0,60}?)</a>""", html, re.I | re.S):
        if cible.startswith("#") or "EntrepriseAdvancedSearch" in cible:
            continue                                        # les entrées de menu n'apprennent rien
        liens.append(f"{propre(texte)[:34] or '(image)'} -> {cible[:110]}")
    print(f"  {titre} : {len(html)} octets")
    if entetes:
        print(f"    colonnes : {' | '.join(entetes[:14])}")
    for l in list(dict.fromkeys(liens))[:12]:
        print(f"    lien : {l}")


def echantillon_allotis(nombre: int) -> list[tuple[str, str, str]]:
    lot = []
    for f in (DONNEES / "extraits").glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not d.get("present"):
            continue
        m = NB.search((d.get("allotissement") or "").strip())
        if not m or int(m.group(1)) < 2:
            continue
        perdants = sum(1 for c in d.get("soumissionnaires") or []
                       if c.get("statut") != "attributaire" and c.get("montant_acte_engagement"))
        lot.append((d["refConsultation_annonce"], d["orgAcronyme"],
                    f"{m.group(1)} lots, {perdants} perdants chiffrés"))
    random.seed(7)
    random.shuffle(lot)
    # On veut les deux formes : celles où les perdants sont chiffrés et celles où ils ne le sont pas.
    muets = [x for x in lot if x[2].endswith("0 perdants chiffrés")]
    autres = [x for x in lot if not x[2].endswith("0 perdants chiffrés")]
    return (muets[:nombre // 2] + autres[:nombre - nombre // 2])


def sonder_lots(nombre: int) -> None:
    dossier = SORTIE / "lots"
    dossier.mkdir(parents=True, exist_ok=True)
    lot = echantillon_allotis(nombre)
    print(f"\n=== DÉTAIL PAR LOT — {len(lot)} marchés ===")
    client = tp.Client()
    for ref, org, note in lot:
        try:
            html = client.html(POPUP.format(ref=ref, org=org))
        except Exception as e:                               # noqa: BLE001
            print(f"  {ref} : échec ({type(e).__name__})")
            continue
        (dossier / f"{ref}.html").write_text(html, encoding="utf-8", errors="replace")
        decrire(html, f"{ref} ({org}) — {note}")
        time.sleep(0.8)


def sonder_annonces() -> None:
    """Ouvre la recherche des annonces d'extrait de PV et conserve la première page de résultats."""
    dossier = SORTIE / "annonces"
    dossier.mkdir(parents=True, exist_ok=True)
    print("\n=== LISTE DES ANNONCES D'EXTRAIT DE PV ===")
    client = tp.Client()
    page = client.html(URL_ANNONCES)
    (dossier / "formulaire.html").write_text(page, encoding="utf-8", errors="replace")
    print(f"  formulaire : {len(page)} octets -> {dossier / 'formulaire.html'}")

    cc.CATEGORIE, cc.DATE_DEBUT, cc.DATE_FIN = "1", "01/01/2022", "31/12/2027"
    try:
        html = tp.postback(client, page, cc.PFX + "lancerRecherche", cc.criteres(),
                           button=(cc.PFX + "lancerRecherche", "Lancer la recherche"))
    except Exception as e:                                   # noqa: BLE001
        print(f"  la recherche a échoué ({type(e).__name__}: {e})")
        print("  le formulaire est conservé : les noms de champs y sont, je m'en servirai.")
        return
    (dossier / "resultats_page1.html").write_text(html, encoding="utf-8", errors="replace")
    total, pages = tp.pagination(html)
    print(f"  résultats : {total} annonces, {pages} page(s) -> {dossier / 'resultats_page1.html'}")

    lignes = cc.lignes(html)
    print(f"  {len(lignes)} lignes lues par le lecteur actuel")
    decrire(html, "première page de résultats")

    # Ce qui nous intéresse vraiment : par où se télécharge le PDF d'une annonce.
    print("\n  adresses contenant un téléchargement :")
    vues = set()
    for cible in re.findall(r"""(?:href|onclick|src)\s*=\s*["']([^"']{6,250})["']""", html, re.I):
        if re.search(r"download|telecharg|fichier|\.pdf|PopUp|DetailAvis|ExtraitPV", cible, re.I):
            net = unescape(cible)[:150]
            if net not in vues:
                vues.add(net)
                print(f"    {net}")
    if not vues:
        print("    aucune — le lien est probablement derrière un bouton de formulaire.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lots", type=int, default=12, metavar="N", help="marchés allotis à ouvrir")
    ap.add_argument("--annonces", action="store_true", help="seulement la liste des annonces")
    ap.add_argument("--sans-annonces", action="store_true", help="seulement le détail par lot")
    a = ap.parse_args()

    SORTIE.mkdir(parents=True, exist_ok=True)
    if not a.annonces:
        sonder_lots(a.lots)
    if not a.sans_annonces:
        sonder_annonces()
    print(f"\n-> tout est dans {SORTIE}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu — ce qui est déjà conservé reste utilisable.")
