"""Sépare les PV saisis sur le portail de ceux lus par OCR, mêlés dans le même dossier.

« rattraper_pv.py --installer » a déversé 32 569 procès-verbaux lus par OCR dans « extraits\\ »,
où se trouvaient déjà ceux que les acheteurs avaient tapés dans le portail. Les deux jeux
cohabitent depuis, et il n'est plus possible de reconstruire l'une ou l'autre base à volonté :
toute reconstruction prend les deux.

Chaque fiche porte pourtant sa provenance, dans son champ « source ». Ce programme s'en sert
pour les remettre dans deux dossiers distincts :

    extraits\\        les PV saisis par l'acheteur sur le portail — exacts, c'est la base de « main »
    extraits_ocr\\    les PV lus sur un scan ou dans un Word — la base de la branche « ocr-travaux »

Ensuite, deux bases se construisent depuis le même disque, sans confusion possible :

    python construire_base.py --base travaux               le portail seul
    python construire_base.py --base travaux --avec-ocr    portail + OCR

    cd pv_travaux\\site
    python separer_extraits.py --etat     compte sans rien déplacer
    python separer_extraits.py            sépare pour de bon

Aucune fiche n'est effacée ni modifiée : elles changent seulement de dossier, et une relance
après coup ne fait rien de plus.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

ICI = Path(__file__).resolve().parent
DONNEES = Path(os.environ.get("DONNEES") or ICI.parent)
PORTAIL = DONNEES / "extraits"
OCR = DONNEES / "extraits_ocr"

# Ce qui a été tapé dans le portail par l'acheteur. Tout le reste vient d'une lecture de document.
SOURCES_PORTAIL = {"extrait_html", "portail", ""}


def provenance(fiche: dict) -> str:
    return str(fiche.get("source") or "").strip()


def separer(etat_seulement: bool) -> None:
    if not PORTAIL.is_dir():
        sys.exit(f"{PORTAIL} est introuvable.")
    t0 = time.time()
    comptes: Counter = Counter()
    a_deplacer: list = []
    illisibles = 0

    for f in sorted(PORTAIL.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            illisibles += 1
            comptes["illisible (laissée sur place)"] += 1
            continue
        s = provenance(d)
        comptes[s or "(sans source — portail par défaut)"] += 1
        if s not in SOURCES_PORTAIL:
            a_deplacer.append(f)

    deja = len(list(OCR.glob("*.json"))) if OCR.is_dir() else 0
    print(f"=== {sum(comptes.values())} fiches dans {PORTAIL} ===")
    for source, n in comptes.most_common():
        cote = "-> extraits_ocr" if source not in SOURCES_PORTAIL \
            and not source.startswith(("(sans", "illisible")) else "   reste ici"
        print(f"  {n:>7}  {source:<34} {cote}")
    if deja:
        print(f"  {deja:>7}  déjà dans {OCR.name}")
    print(f"\n  à déplacer : {len(a_deplacer)}")
    print(f"  resteront dans {PORTAIL.name} : {sum(comptes.values()) - len(a_deplacer)}")
    if illisibles:
        print(f"  {illisibles} fiche(s) illisibles, laissées en place")

    if etat_seulement:
        print("\n(recensement seulement — rien n'a été déplacé)")
        return
    if not a_deplacer:
        print("\nRien à déplacer : les deux jeux sont déjà séparés.")
        return

    OCR.mkdir(parents=True, exist_ok=True)
    deplacees = erreurs = 0
    for i, f in enumerate(a_deplacer, 1):
        cible = OCR / f.name
        try:
            if cible.exists():
                cible.unlink()                               # une relance écrase la copie précédente
            os.replace(f, cible) if f.drive == cible.drive else shutil.move(str(f), str(cible))
            deplacees += 1
        except OSError as e:
            erreurs += 1
            print(f"  échec sur {f.name} : {type(e).__name__}: {e}")
        if i % 5000 == 0:
            print(f"  {i}/{len(a_deplacer)}…")

    restantes = len(list(PORTAIL.glob("*.json")))
    partis = len(list(OCR.glob("*.json")))
    print(f"\n{deplacees} fiche(s) déplacées" + (f", {erreurs} échec(s)" if erreurs else ""))
    print(f"  {PORTAIL}  : {restantes} fiches  (la base de « main »)")
    print(f"  {OCR}  : {partis} fiches  (la base de « ocr-travaux »)")
    print(f"({time.time() - t0:.0f} s)")
    print("\nReconstruis la base du portail seul :")
    print("   python construire_base.py --base travaux")
    print("Et celle qui ajoute l'OCR, quand tu la voudras :")
    print("   python construire_base.py --base travaux --avec-ocr")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--etat", action="store_true", help="compter sans rien déplacer")
    separer(ap.parse_args().etat)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu — ce qui est déplacé le reste, relance pour finir.")
