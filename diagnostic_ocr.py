"""Pourquoi l'OCR rend-il si peu sur cette machine, alors qu'il réussit ailleurs ?

Les mêmes PDF, lus avec le même code, donnent 96 % de documents exploitables sur une machine
et 23 % sur celle-ci. La différence n'est donc ni dans les documents ni dans le programme :
elle est dans l'installation. Ce diagnostic interroge l'installation et mesure, document par
document, où la lecture se perd.

    cd pv_travaux\\site
    python diagnostic_ocr.py

Deux minutes, aucune écriture ailleurs que dans diagnostic_ocr.txt. Envoie-moi ce fichier.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
PDF = ICI / "rattrapage" / "pdf"
RAPPORT = ICI / "diagnostic_ocr.txt"

_sortie = []


def dire(*mots) -> None:
    ligne = " ".join(str(m) for m in mots)
    print(ligne, flush=True)
    _sortie.append(ligne)


def installation() -> None:
    dire("=" * 68)
    dire("L'INSTALLATION")
    dire("=" * 68)
    dire(f"  système        : {platform.platform()}")
    dire(f"  python         : {sys.version.split()[0]}")
    dire(f"  processeurs    : {os.cpu_count()}")
    try:
        import psutil                                       # noqa: F401
        dire(f"  mémoire totale : {psutil.virtual_memory().total / 1e9:.1f} Go, "
             f"libre {psutil.virtual_memory().available / 1e9:.1f} Go")
    except ImportError:
        dire("  mémoire        : (psutil absent — pip install psutil pour l'avoir)")

    for module in ("cv2", "pypdfium2", "pytesseract", "PIL"):
        try:
            m = __import__(module)
            dire(f"  {module:<14} : {getattr(m, '__version__', '?')}")
        except ImportError as e:
            dire(f"  {module:<14} : ABSENT ({e})")

    dire("")
    dire("  TESSERACT")
    chemin = shutil.which("tesseract")
    try:
        import pytesseract
        chemin = pytesseract.pytesseract.tesseract_cmd or chemin
    except ImportError:
        pass
    dire(f"    exécutable   : {chemin}")
    dire(f"    TESSDATA_PREFIX : {os.environ.get('TESSDATA_PREFIX', '(non défini)')}")
    for commande, titre in ((["--version"], "version"), (["--list-langs"], "langues")):
        try:
            r = subprocess.run([chemin or "tesseract"] + commande, capture_output=True,
                               text=True, timeout=60)
            texte = (r.stdout or "") + (r.stderr or "")
            for l in [x for x in texte.splitlines() if x.strip()][:6]:
                dire(f"    {titre:<12} : {l.strip()}")
        except Exception as e:                               # noqa: BLE001
            dire(f"    {titre:<12} : ÉCHEC — {type(e).__name__}: {e}")


def lecture() -> None:
    """Six documents, lus un par un, en mesurant chaque étape."""
    dire("")
    dire("=" * 68)
    dire("LA LECTURE, DOCUMENT PAR DOCUMENT")
    dire("=" * 68)
    if not PDF.is_dir():
        dire(f"  {PDF} introuvable.")
        return
    try:
        import pv_ocr, pv_parser
        import rattraper_pv as rp
    except ImportError as e:
        dire(f"  impossible de charger les modules : {e}")
        return

    fichiers = sorted(PDF.glob("*.pdf"))[:6]
    dire(f"  {len(fichiers)} documents, un seul à la fois (pas de parallélisme ici)")
    dire("")
    dire(f"  {'document':<11}{'Ko':>7}{'mode':>9}{'méthode':>13}{'mots':>7}{'sec':>7}  verdict")
    for f in fichiers:
        donnees = f.read_bytes()
        for mode in ("--psm 4", "--psm 6", "--psm 3"):
            t0 = time.time()
            try:
                pv_ocr.TESS_CONFIG = mode
                pages, methode = pv_ocr.read_pages(donnees, max_pages=12)
                mots = sum(len(p.split()) for p in pages)
                res = pv_parser.parse(pages)
                d = res[0] if isinstance(res, tuple) and len(res) == 3 else res
                verdict = rp.classer(rp.en_forme_extrait(f.stem, "", d, [], []))[0]
            except Exception as e:                           # noqa: BLE001
                methode, mots, verdict = "—", 0, f"ERREUR {type(e).__name__}: {e}"
            dire(f"  {f.stem:<11}{len(donnees)//1024:>7}{mode[-1]:>9}{methode:>13}"
                 f"{mots:>7}{time.time()-t0:>7.0f}  {verdict}")
            if not str(verdict).startswith(("insuffisant", "ERREUR")):
                break

    dire("")
    dire("  « mots » est le chiffre décisif. Au-dessus de 150, l'OCR a fait son travail et")
    dire("  c'est la lecture qui échoue. En dessous de 50, c'est l'OCR qui ne rend rien,")
    dire("  et la cause est dans l'installation ci-dessus — pas dans les documents.")


def main() -> None:
    dire(f"diagnostic du {time.strftime('%d/%m/%Y à %H:%M')}")
    installation()
    lecture()
    RAPPORT.write_text("\n".join(_sortie), encoding="utf-8")
    dire("")
    dire(f"-> {RAPPORT}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrompu.")
    except Exception as e:                                   # noqa: BLE001
        print(f"ARRÊT : {type(e).__name__}: {e}")
        if _sortie:
            RAPPORT.write_text("\n".join(_sortie), encoding="utf-8")
            print(f"Ce qui a été mesuré est dans {RAPPORT}")
