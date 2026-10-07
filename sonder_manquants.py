"""Reconnaissance : que contiennent vraiment les pages dont on ne tire rien ?

Le recensement local a montré deux trous dans la base Travaux :

  * 9 826 consultations closes depuis plus de sept mois n'ont aucun PV lisible en HTML.
    Le portail y publie peut-être le procès-verbal sous forme de PDF joint.
  * 1 135 marchés allotis n'ont ni estimation ni montant pour les concurrents perdants.
    Ces chiffres sont probablement derrière le bouton « plus de détails », lot par lot.

Ce programme ne collecte rien et n'écrit dans aucun dossier de production. Il ouvre un
échantillon de ces pages, les conserve telles quelles dans « sondage\\ », et dresse le compte de
ce qu'elles contiennent : un marqueur, un tableau de montants, un lien de téléchargement, un
bouton de détail. C'est ce compte qui dira s'il vaut la peine d'écrire un extracteur, et lequel.

    cd pv_travaux\\site
    python sonder_manquants.py --cas absents --nombre 40
    python sonder_manquants.py --cas lots    --nombre 20
    python sonder_manquants.py --cas tout

Puis envoie-moi « sondage\\rapport.csv » et deux ou trois des pages HTML conservées.
"""
from __future__ import annotations

import argparse
import csv
import datetime
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
import extrait_pv                                            # noqa: E402

DONNEES = ICI.parent
SORTIE = ICI / "sondage"
DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
NB_LOTS = re.compile(r"(\d+)")

# Ce qu'on cherche dans une page. Chaque indice est un nom et un motif ; le rapport dit
# simplement, pour chaque page, lesquels sont présents.
INDICES = [
    ("marqueur", re.compile(re.escape(extrait_pv.MARQUEUR))),
    ("montants_actes", re.compile(r"Montant des actes")),
    ("concurrents_deposes", re.compile(r"ayant d[ée]pos[ée] un pli", re.I)),
    ("infructueux", re.compile(r"D[ée]claration Infructueux", re.I)),
    ("lien_pdf", re.compile(r"\.pdf", re.I)),
    ("telechargement", re.compile(r"t[ée]l[ée]charg|download|DownloadFile", re.I)),
    ("piece_jointe", re.compile(r"pi[èe]ce[s]? jointe|document[s]? joint", re.I)),
    ("proces_verbal", re.compile(r"proc[èe]s[- ]verbal", re.I)),
    ("plus_de_details", re.compile(r"plus de d[ée]tail", re.I)),
    ("detail_lot", re.compile(r"detailLot|DetailLot|detail_lot", re.I)),
    ("tableau_lots", re.compile(r"Num[ée]ro\s*(du\s*)?lot", re.I)),
    ("estimation", re.compile(r"Estimation", re.I)),
    ("acces_refuse", re.compile(r"acc[èe]s refus|non autoris|session expir", re.I)),
]

# Les liens et les boutons de la page : c'est là que se trouve le chemin vers le PDF ou le détail.
LIEN = re.compile(r"""<a\b[^>]*?(?:href|onclick)\s*=\s*["']([^"']{4,200})["'][^>]*>(.{0,80}?)</a>""",
                  re.I | re.S)
BOUTON = re.compile(r"""<input\b[^>]*type\s*=\s*["'](?:submit|button|image)["'][^>]*>""", re.I)
ATTR = re.compile(r"""(name|value|id|src)\s*=\s*["']([^"']{0,120})["']""", re.I)


def nettoyer(texte: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", texte or ""))).strip()


def url_extrait(ref: str, org: str) -> str:
    return tp.BASE + f"?page=entreprise.ExtraitPV&refConsultation={ref}&orgAcronyme={org}"


def charger() -> tuple[dict, dict]:
    """(état de chaque extrait déjà collecté, fiche de chaque consultation)."""
    extraits, fiches = {}, {}
    dossier = DONNEES / "extraits"
    if not dossier.is_dir():
        sys.exit(f"{dossier} introuvable — lance ce programme depuis le dossier du site.")
    for f in dossier.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ref = d.get("refConsultation_annonce")
        if ref:
            extraits[ref] = d
    for f in (DONNEES / "consultations" / "estimations").glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ref = d.get("refConsultation_annonce")
        if ref:
            fiches[ref] = d
    print(f"{len(extraits)} extraits et {len(fiches)} fiches lus sur le disque")
    return extraits, fiches


def mois_depuis_cloture(fiche: dict) -> int | None:
    ch = fiche.get("champs") or {}
    m = DATE.search(ch.get("Date et heure limite de remise des plis") or "")
    if not m:
        return None
    jour = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return (datetime.date.today() - jour).days // 30


def choisir(cas: str, nombre: int, extraits: dict, fiches: dict) -> list[dict]:
    """L'échantillon, étalé sur toute la population plutôt que pris au début."""
    lot = []
    for ref, fiche in fiches.items():
        e = extraits.get(ref)
        org = fiche.get("orgAcronyme")
        if not org:
            continue
        if cas == "absents":
            # PV jamais lu en HTML, et plis clos depuis assez longtemps pour qu'un PV existe.
            if e is None or e.get("present"):
                continue
            mois = mois_depuis_cloture(fiche)
            if mois is None or mois < 7:
                continue
            lot.append({"ref": ref, "org": org, "cas": cas, "mois": mois,
                        "lots": (fiche.get("champs") or {}).get("Allotissement") or ""})
        elif cas == "lots":
            # Marché alloti dont le PV est lu : on cherche le détail par lot qui nous manque.
            if not e or not e.get("present"):
                continue
            m = NB_LOTS.search((e.get("allotissement") or "").strip())
            if not m or int(m.group(1)) < 2:
                continue
            perdants = sum(1 for c in e.get("soumissionnaires") or []
                           if c.get("statut") != "attributaire" and c.get("montant_acte_engagement"))
            lot.append({"ref": ref, "org": org, "cas": cas, "mois": mois_depuis_cloture(fiche) or -1,
                        "lots": e.get("allotissement") or "", "perdants_chiffres": perdants})
    random.seed(12)                                  # même échantillon d'une fois sur l'autre
    random.shuffle(lot)
    print(f"cas « {cas} » : {len(lot)} consultations concernées, {min(nombre, len(lot))} seront ouvertes")
    return lot[:nombre]


def examiner(html: str) -> dict:
    # Le portail écrit « proc&egrave;s-verbal » : sans décodage, la moitié des indices manquent.
    lisible = unescape(html)
    trouve = {nom: bool(motif.search(html) or motif.search(lisible)) for nom, motif in INDICES}
    liens, boutons = [], []
    for cible, texte in LIEN.findall(html):
        t = nettoyer(texte)
        if cible.startswith("#") or not t:
            continue
        liens.append(f"{t[:40]} -> {cible[:80]}")
    for b in BOUTON.findall(html):
        attrs = dict((k.lower(), v) for k, v in ATTR.findall(b))
        etiquette = attrs.get("value") or attrs.get("src") or ""
        nom = attrs.get("name") or attrs.get("id") or ""
        if nom:
            boutons.append(f"{nettoyer(etiquette)[:40]} [{nom[:60]}]")
    return {**trouve, "taille": len(html),
            "liens": " | ".join(dict.fromkeys(liens))[:900],
            "boutons": " | ".join(dict.fromkeys(boutons))[:900]}


def sonder(lot: list[dict]) -> list[dict]:
    pages = SORTIE / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    client = tp.Client()
    resultats = []
    for i, c in enumerate(lot, 1):
        try:
            html = client.html(url_extrait(c["ref"], c["org"]))
        except Exception as e:                       # noqa: BLE001
            print(f"  {c['ref']} : échec ({type(e).__name__})")
            resultats.append({**c, "echec": type(e).__name__})
            continue
        (pages / f"{c['cas']}_{c['ref']}.html").write_text(html, encoding="utf-8", errors="replace")
        vu = examiner(html)
        # Ce que notre lecteur actuel en tire, pour comparer.
        lu = extrait_pv.lire(html)
        vu["lecteur_actuel"] = ("rien" if not lu else
                                f"{len(lu.get('soumissionnaires') or [])} concurrents")
        resultats.append({**c, **vu})
        drapeaux = [k for k in ("montants_actes", "lien_pdf", "telechargement",
                                "plus_de_details", "tableau_lots", "infructueux") if vu.get(k)]
        print(f"  {i}/{len(lot)} {c['ref']:<10} {vu['taille']:>7} o  "
              f"{vu['lecteur_actuel']:<14} {' '.join(drapeaux)}", flush=True)
        time.sleep(0.8)
    return resultats


def rapport(resultats: list[dict]) -> None:
    if not resultats:
        return
    colonnes = list(dict.fromkeys(k for r in resultats for k in r))
    fichier = SORTIE / "rapport.csv"
    with open(fichier, "w", encoding="utf-8-sig", newline="") as f:
        ecrire = csv.DictWriter(f, fieldnames=colonnes, delimiter=";")
        ecrire.writeheader()
        for r in resultats:
            ecrire.writerow(r)

    print(f"\n{'INDICE':<24}{'PAGES':>8}{'PART':>8}")
    n = len(resultats)
    for nom, _ in INDICES:
        c = sum(1 for r in resultats if r.get(nom))
        if c:
            print(f"{nom:<24}{c:>8}{100 * c / n:>7.0f} %")
    rien = sum(1 for r in resultats if r.get("lecteur_actuel") == "rien")
    print(f"\n{n} pages ouvertes, notre lecteur n'en tire rien dans {rien} cas ({100*rien/n:.0f} %)")
    print(f"-> {fichier}")
    print(f"-> {SORTIE / 'pages'} ({len(list((SORTIE / 'pages').glob('*.html')))} pages conservées)")
    print("\nEnvoie-moi rapport.csv et deux ou trois pages, surtout celles qui montrent")
    print("« lien_pdf », « telechargement » ou « plus_de_details ».")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cas", default="tout", choices=("absents", "lots", "tout"))
    ap.add_argument("--nombre", type=int, default=30, metavar="N", help="pages par cas (défaut 30)")
    a = ap.parse_args()

    extraits, fiches = charger()
    resultats = []
    for cas in (("absents", "lots") if a.cas == "tout" else (a.cas,)):
        print(f"\n=== {cas.upper()} ===")
        lot = choisir(cas, a.nombre, extraits, fiches)
        if lot:
            resultats += sonder(lot)
    rapport(resultats)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu.")
