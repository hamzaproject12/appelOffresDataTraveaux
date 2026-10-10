"""Rattrape les procès-verbaux que le portail ne publie qu'en PDF scanné.

Le portail annonce 34 556 extraits de PV pour les Travaux depuis 2022. On en lit 20 162 : pour
les autres, la page « ExtraitPV » est une coquille vide de 54 640 octets — le menu du site et
rien d'autre — et le procès-verbal est un PDF joint à l'annonce.

Quatre étapes, chacune reprenable. Chaque unité de travail est un fichier : une annonce lue,
un PDF téléchargé, un PV analysé. Au lancement, ce qui existe déjà est sauté. Couper la
connexion, fermer la fenêtre, redémarrer la machine : relancer la même commande reprend là où
elle s'était arrêtée, sans rien refaire.

    cd pv_travaux\\site
    python rattraper_pv.py --essai 200      ESSAI : les 4 étapes sur 200 annonces, puis rapport
    python rattraper_pv.py --liste          1. les 34 556 annonces          ~1 h
    python rattraper_pv.py --avis           2. où est le PDF de chacune     ~6 h
    python rattraper_pv.py --pdf            3. télécharger les PDF          ~6 h, ~10 Go
    python rattraper_pv.py --ocr            4. OCR et analyse               une nuit
    python rattraper_pv.py --rapport        où en est-on
    python rattraper_pv.py --installer      verser les PV acceptés dans ..\\extraits

Commence par --essai 200. Il dira quelle proportion a vraiment un PDF, passe l'OCR et sort
des concurrents chiffrés. C'est sur ce chiffre qu'on décide de lancer les nuits complètes,
pas sur une estimation.

Rien n'est versé dans les données de production avant --installer, qui ne prend que les PV
dont l'analyse tient debout. Les autres restent dans rattrapage\\rejets pour être regardés.
"""
from __future__ import annotations

import argparse
import csv
import io as _io
import json
import os
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import (FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor,
                                wait)
from concurrent.futures.process import BrokenProcessPool
from html import unescape
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
import telecharger_pv as tp                                  # noqa: E402
import collecter_consultations as cc                         # noqa: E402

DONNEES = Path(os.environ.get("DONNEES") or ICI.parent)
RACINE = ICI / "rattrapage"
ANNONCES = RACINE / "annonces.csv"
MARQUE = RACINE / "derniere_page.txt"
AVIS, PDF, PV, REJETS = RACINE / "avis", RACINE / "pdf", RACINE / "pv", RACINE / "rejets"
PIECES = RACINE / "pieces"            # les pièces qui ne sont pas des PDF nus
MOISSON = DONNEES / "moisson"         # où ranger_pv.py classe la récolte une fois lue

URL_RECHERCHE = tp.BASE + "?page=entreprise.EntrepriseAdvancedSearch&AvisExtraitPV"
URL_ANNONCE = tp.BASE + "?page=entreprise.EntrepriseDetailConsultation&refConsultation={ref}&orgAcronyme={org}"
URL_FICHIER = (tp.BASE + "?page=entreprise.EntrepriseDownloadAvisJAL"
                         "&refConsultation={ref}&orgAcronyme={org}&idAvis={avis}")
TYPE_EXTRAIT_PV, CATEGORIE_TRAVAUX = "5", "1"
PAUSE = 0.7

# Le lien du fichier joint, dans la page de l'annonce. Le titre porte la taille : « - 716,79 Ko ».
LIEN_FICHIER = re.compile(
    r"""<a[^>]*?title\s*=\s*["']([^"']*)["'][^>]*?href\s*=\s*["'][^"']*?"""
    r"""EntrepriseDownloadAvisJAL[^"']*?idAvis=(\d+)[^"']*["'][^>]*>(.*?)</a>""", re.I | re.S)


def dire(*mots) -> None:
    print(f"[{time.strftime('%d/%m/%Y %H:%M:%S')}]", *mots, flush=True)


def propre(t: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", t or ""))).strip()


def ecrire_json(chemin: Path, donnees) -> None:
    """Écriture atomique : une coupure laisse un .tmp ignoré, jamais un fichier à moitié écrit."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    provisoire = chemin.with_suffix(chemin.suffix + ".tmp")
    provisoire.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(provisoire, chemin)


def ecrire_binaire(chemin: Path, donnees: bytes) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    provisoire = chemin.with_suffix(chemin.suffix + ".tmp")
    provisoire.write_bytes(donnees)
    os.replace(provisoire, chemin)


def nettoyer_tmp() -> None:
    n = 0
    for dossier in (AVIS, PDF, PV, REJETS):
        for f in dossier.glob("*.tmp") if dossier.is_dir() else []:
            f.unlink(missing_ok=True)
            n += 1
    if n:
        dire(f"{n} fichier(s) interrompu(s) effacé(s) : ils seront repris")


# ------------------------------------------------------- 1. la liste des annonces

def criteres(depuis: str, jusqua: str) -> dict:
    p = cc.PFX
    return {p + "annonceType": TYPE_EXTRAIT_PV, p + "categorie": CATEGORIE_TRAVAUX,
            p + "dateMiseEnLigneCalculeStart": depuis, p + "dateMiseEnLigneCalculeEnd": jusqua}


def liste(depuis: str, jusqua: str) -> None:
    RACINE.mkdir(parents=True, exist_ok=True)
    client = tp.Client()
    html = tp.postback(client, client.html(URL_RECHERCHE), cc.PFX + "lancerRecherche",
                       criteres(depuis, jusqua),
                       button=(cc.PFX + "lancerRecherche", "Lancer la recherche"))
    html = tp.postback(client, html, cc.RES + "listePageSizeTop",
                       {cc.RES + "listePageSizeTop": "500", cc.RES + "listePageSizeBottom": "500"})
    total, pages = tp.pagination(html)
    dire(f"{total} annonces d'extrait de PV, {pages} page(s) de 500")

    connues, depart = set(), 1
    if ANNONCES.exists() and MARQUE.exists():
        with open(ANNONCES, encoding="utf-8-sig", newline="") as f:
            connues = {(l["refConsultation"], l["orgAcronyme"]) for l in csv.DictReader(f, delimiter=";")}
        depart = int(MARQUE.read_text().strip() or 1) + 1
        dire(f"reprise : {len(connues)} annonces déjà enregistrées, on repart de la page {depart}")

    sortie = open(ANNONCES, "a" if connues else "w", encoding="utf-8-sig", newline="")
    ecrire = csv.writer(sortie, delimiter=";")
    if not connues:
        ecrire.writerow(["refConsultation", "orgAcronyme", "texte"])
    try:
        for n in range(1, pages + 1):
            if n > 1:
                html = tp.postback(client, html, cc.RES + "PagerTop$ctl2")
                time.sleep(PAUSE)
            if n < depart:
                continue
            neuves = 0
            for l in cc.lignes(html):
                if (l["ref"], l["org"]) in connues:
                    continue
                connues.add((l["ref"], l["org"]))
                ecrire.writerow([l["ref"], l["org"], propre(l["texte"])[:300]])
                neuves += 1
            sortie.flush()
            MARQUE.write_text(str(n))
            dire(f"  page {n}/{pages} : +{neuves} (total {len(connues)})")
    finally:
        sortie.close()
    dire(f"-> {ANNONCES} ({len(connues)} annonces)")


def annonces_listees() -> list[dict]:
    if not ANNONCES.exists():
        sys.exit(f"{ANNONCES} introuvable — lance d'abord : python rattraper_pv.py --liste")
    with open(ANNONCES, encoding="utf-8-sig", newline="") as f:
        return [{"ref": l["refConsultation"], "org": l["orgAcronyme"]}
                for l in csv.DictReader(f, delimiter=";") if l.get("refConsultation")]


def deja_lues_en_html() -> set:
    """Les consultations dont on lit déjà le PV en HTML : inutile d'aller chercher leur PDF."""
    out = set()
    dossier = DONNEES / "extraits"
    for f in dossier.glob("*.json") if dossier.is_dir() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("present") and (d.get("soumissionnaires") or []):
            out.add(str(d.get("refConsultation_annonce")))
    return out


def a_traiter(limite: int = 0) -> list[dict]:
    lues = deja_lues_en_html()
    reste = [a for a in annonces_listees() if a["ref"] not in lues]
    dire(f"{len(reste)} annonces sans PV lisible en HTML (sur {len(annonces_listees())})")
    return reste[:limite] if limite else reste


# --------------------------------------------------- 2. où est le fichier joint

def lire_annonce(client, a: dict) -> dict:
    html = client.html(URL_ANNONCE.format(**{"ref": a["ref"], "org": a["org"]}))
    fichiers = []
    for titre, avis, texte in LIEN_FICHIER.findall(html):
        fichiers.append({"idAvis": avis, "titre": propre(titre), "libelle": propre(texte)})
    return {"ref": a["ref"], "org": a["org"], "fichiers": fichiers,
            "type_annonce": propre((re.search(r'_annonce">(.*?)</span>', html) or [None, ""])[1])
            if re.search(r'_annonce">(.*?)</span>', html) else "",
            "vu_le": time.strftime("%Y-%m-%d")}


def avis(limite: int, fils: int) -> None:
    AVIS.mkdir(parents=True, exist_ok=True)
    reste = [a for a in a_traiter(limite) if not (AVIS / f"{a['ref']}.json").exists()]
    dire(f"{len(reste)} annonce(s) à ouvrir")
    if not reste:
        return
    propre_fil = threading.local()

    def client():
        if not hasattr(propre_fil, "c"):
            propre_fil.c = tp.Client()
        return propre_fil.c

    def traiter(a):
        try:
            d = lire_annonce(client(), a)
        except Exception as e:                               # noqa: BLE001
            time.sleep(PAUSE)
            return a, None, type(e).__name__
        time.sleep(PAUSE)
        return a, d, None

    avec, sans, echecs = 0, 0, 0
    with ThreadPoolExecutor(max_workers=max(1, fils)) as ex:
        for i, (a, d, err) in enumerate(ex.map(traiter, reste), 1):
            if err:
                echecs += 1
                continue
            ecrire_json(AVIS / f"{a['ref']}.json", d)
            if d["fichiers"]:
                avec += 1
            else:
                sans += 1
            if i % 50 == 0:
                dire(f"  {i}/{len(reste)} — {avec} avec fichier, {sans} sans")
    dire(f"{avec} annonce(s) avec un fichier joint, {sans} sans"
         + (f", {echecs} échec(s) — repris au prochain passage" if echecs else ""))


# --------------------------------------------------------- 3. télécharger les PDF

# La référence du marché dans le texte d'une annonce : « … Travaux 07/10/2026 115/FLSHM/2026 - … ».
# C'est la seule clé commune avec nos extraits HTML, car le portail numérote les annonces d'extrait
# de PV dans un espace d'identifiants distinct de celui des consultations.
REFERENCE_MARCHE = re.compile(r"\d{2}/\d{2}/\d{4}\s+(.{2,40}?)\s+-\s+\.\.\.")


def cle_reference(texte: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (texte or "").lower())


def references_connues() -> set:
    """Les références de marché dont on lit déjà le PV en HTML."""
    out = set()
    dossier = DONNEES / "extraits"
    for f in dossier.glob("*.json") if dossier.is_dir() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("present") and (d.get("soumissionnaires") or []) and d.get("reference"):
            out.add(cle_reference(d["reference"]))
    return out


def references_des_annonces() -> dict:
    """{refConsultation de l'annonce: clé de la référence du marché}."""
    out = {}
    if not ANNONCES.exists():
        return out
    with open(ANNONCES, encoding="utf-8-sig", newline="") as f:
        for l in csv.DictReader(f, delimiter=";"):
            m = REFERENCE_MARCHE.search(unescape(l.get("texte") or ""))
            out[l["refConsultation"]] = cle_reference(m.group(1)) if m else ""
    return out


def pdf_dans_archive(contenu: bytes) -> bytes | None:
    """Le PDF contenu dans une archive ZIP, s'il y en a un. Sinon None.

    Plusieurs acheteurs joignent « Cadre des résultats et Extrait du PV ….zip » au lieu du PDF.
    On prend le plus gros PDF de l'archive : le procès-verbal est plus volumineux que la page
    de garde ou le cadre de résultats qui l'accompagnent parfois.
    """
    import io as _io
    import zipfile
    if contenu[:2] != b"PK":
        return None
    try:
        with zipfile.ZipFile(_io.BytesIO(contenu)) as z:
            pdfs = [n for n in z.namelist() if n.lower().endswith(".pdf")]
            if not pdfs:
                return None
            choisi = max(pdfs, key=lambda n: z.getinfo(n).file_size)
            donnees = z.read(choisi)
            return donnees if donnees[:5].startswith(b"%PDF") else None
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return None


def pdf(limite: int, fils: int) -> None:
    PDF.mkdir(parents=True, exist_ok=True)
    fiches = []
    for f in sorted(AVIS.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("fichiers") and not (PDF / f"{d['ref']}.pdf").exists():
            fiches.append(d)

    # Les marchés qu'on n'a pas encore d'abord. Un tiers des annonces concerne des marchés dont
    # on lit déjà le PV en HTML : leur PDF sert à vérifier la qualité de l'OCR, pas à enrichir la
    # base. Mieux vaut donc tenir les nouveaux en premier — si la nuit ne suffit pas, c'est la
    # partie utile qui sera faite.
    connues, des_annonces = references_connues(), references_des_annonces()
    inedits = [f for f in fiches if des_annonces.get(f["ref"], "") not in connues]
    temoins = [f for f in fiches if des_annonces.get(f["ref"], "") in connues]
    dire(f"{len(inedits)} marché(s) inédit(s), {len(temoins)} déjà connu(s) en HTML "
         f"(ceux-là serviront à mesurer la justesse de l'OCR)")
    fiches = inedits + temoins
    if limite:
        fiches = fiches[:limite]
    dire(f"{len(fiches)} fichier(s) à télécharger")
    if not fiches:
        return
    propre_fil = threading.local()

    def client():
        if not hasattr(propre_fil, "c"):
            propre_fil.c = tp.Client()
        return propre_fil.c

    def traiter(d):
        # Le PV est le dernier fichier joint quand il y en a plusieurs : les avis de publicité
        # sont déposés d'abord, le procès-verbal ensuite.
        choisi = d["fichiers"][-1]
        url = URL_FICHIER.format(ref=d["ref"], org=d["org"], avis=choisi["idAvis"])
        try:
            with client().open(url) as r:
                contenu = r.read()
                nom = r.headers.get("Content-Disposition", "")
        except Exception as e:                               # noqa: BLE001
            time.sleep(PAUSE)
            return d, None, None, type(e).__name__
        time.sleep(PAUSE)
        return d, contenu, nom, None

    bons, pas_pdf, echecs, octets, nommes_pv = 0, 0, 0, 0, 0
    with ThreadPoolExecutor(max_workers=max(1, fils)) as ex:
        for i, (d, contenu, nom, err) in enumerate(ex.map(traiter, fiches), 1):
            if err:
                echecs += 1
                continue
            if contenu and not contenu[:5].startswith(b"%PDF"):
                # Une partie des acheteurs dépose une archive « Cadre des résultats et Extrait
                # du PV.zip » plutôt que le PDF nu. Le procès-verbal est dedans.
                contenu = pdf_dans_archive(contenu)
            if not contenu:
                pas_pdf += 1
                ecrire_json(REJETS / f"{d['ref']}.json",
                            {**d, "probleme": "ni PDF ni archive contenant un PDF",
                             "nom_serveur": nom})
                continue
            ecrire_binaire(PDF / f"{d['ref']}.pdf", contenu)
            # Le libellé du lien ne dit pas si c'est le PV (« Avis complémentaire en ligne »),
            # mais le serveur, lui, nomme le fichier « Extrait_du_PV_... ». On le garde pour
            # pouvoir vérifier après coup qu'on a bien téléchargé le bon document.
            nom_fichier = ""
            if nom:
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', nom, re.I)
                nom_fichier = unescape(m.group(1)).strip() if m else ""
            ecrire_json(AVIS / f"{d['ref']}.json", {**d, "nom_serveur": nom_fichier,
                                                    "octets": len(contenu)})
            bons += 1
            octets += len(contenu)
            if nom_fichier and re.search(r"pv|proc[eè]s", nom_fichier, re.I):
                nommes_pv += 1
            if i % 25 == 0:
                dire(f"  {i}/{len(fiches)} — {bons} PDF, {octets/1e6:.0f} Mo")
    dire(f"{bons} PDF téléchargés ({octets/1e6:.0f} Mo), {pas_pdf} fichier(s) d'un autre type"
         + (f", {echecs} échec(s)" if echecs else ""))
    if bons:
        dire(f"{nommes_pv}/{bons} portent « PV » dans le nom donné par le serveur "
             f"({100*nommes_pv/bons:.0f} %) — c'est le contrôle que le bon fichier a été pris")


# ----------------------------------------- 3bis. estimation, qualification, classe

def fiches(fils: int) -> int:
    """Lit la fiche de consultation de chaque annonce : l'estimation du maître d'ouvrage.

    Le procès-verbal ne porte jamais l'estimation — elle est sur la fiche de la consultation,
    et c'est elle qui manque pour calculer un prix de référence. Sans cette étape, les marchés
    rattrapés auraient leurs concurrents et leurs montants, mais aucun classement possible.

    La fiche apporte aussi la caution, les qualifications exigées et la classe. On verse les
    annonces dans la liste des consultations — les deux familles d'identifiants ne se croisent
    jamais, donc rien ne se mélange — puis on laisse collecter_consultations faire son travail,
    avec sa reprise habituelle.
    """
    liste = DONNEES / "consultations" / "consultations.csv"
    if not ANNONCES.exists():
        sys.exit(f"{ANNONCES} introuvable — lance d'abord : python rattraper_pv.py --liste")

    with open(ANNONCES, encoding="utf-8-sig", newline="") as f:
        annonces = list(csv.DictReader(f, delimiter=";"))
    connues = set()
    if liste.exists():
        with open(liste, encoding="utf-8-sig", newline="") as f:
            connues = {l["refConsultation"] for l in csv.DictReader(f, delimiter=";")}
    neuves = [a for a in annonces if a["refConsultation"] not in connues]
    if neuves:
        liste.parent.mkdir(parents=True, exist_ok=True)
        with open(liste, "a" if connues else "w", encoding="utf-8-sig", newline="") as f:
            ecrire = csv.writer(f, delimiter=";")
            if not connues:
                ecrire.writerow(["refConsultation", "orgAcronyme", "texte"])
            for a in neuves:
                ecrire.writerow([a["refConsultation"], a["orgAcronyme"], a.get("texte", "")])
    dire(f"{len(neuves)} annonce(s) ajoutée(s) à {liste} ({len(connues)} y étaient déjà)")

    cc.configurer(str(DONNEES), None, None, None)
    cc.fiches(max(1, fils))
    return len(neuves)


# ------------------------------------------------------------- 4. OCR et analyse

# Les modèles de langue de Tesseract. Sans « fra », Tesseract ne refuse pas : il rend une page
# vide, document après document, sans un mot d'explication. C'est ce qui a fait croire que
# 29 000 scans étaient illisibles alors qu'ils ne l'étaient pas.
TESSDATA = ("tessdata", "../tessdata", "../../tessdata", "../../archive/tessdata",
            r"C:\pv\tessdata", r"C:\pv\archive\tessdata")


# ------------------------------------------- les pièces jointes qui ne sont pas des PDF


def _trouver_soffice() -> str:
    """LibreOffice, seul outil fiable pour les .doc d'avant 2007. Vide s'il n'est pas installé."""
    import shutil
    for c in (os.environ.get("SOFFICE") or "", shutil.which("soffice") or "",
              r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if c and Path(c).exists():
            return c
    return ""


def _trouver_7z() -> str:
    import shutil
    for c in (os.environ.get("SEPTZIP") or "", shutil.which("7z") or "",
              r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if c and Path(c).exists():
            return c
    return ""


EXTENSIONS = {"pdf": ".pdf", "docx": ".docx", "doc": ".doc", "rtf": ".rtf", "xls": ".xls",
              "zip": ".zip", "rar": ".rar", "7z": ".7z", "image": ".img", "html": ".html",
              "inconnu": ".bin"}

# Dans une archive, le document qui nous intéresse, par ordre de préférence. Un PDF vaut mieux
# qu'un .doc (pas de conversion à faire), et un .doc mieux qu'un tableur : le procès-verbal est
# un texte, le « cadre des résultats » qui l'accompagne est une grille.
PREFERENCES = (".pdf", ".docx", ".doc", ".rtf", ".tif", ".tiff", ".png", ".jpg", ".jpeg",
               ".jpe", ".gif", ".xls", ".xlsx")


def genre(contenu: bytes, nom: str = "") -> str:
    """Ce qu'est la pièce, d'après ses premiers octets.

    Le nom du fichier ne sert qu'à départager un .doc d'un .xls, qui partagent le même en-tête.
    Pour tout le reste on ne s'y fie pas : le portail sert des « .pdf » qui sont des pages web.
    """
    import zipfile
    if contenu[:5].startswith(b"%PDF"):
        return "pdf"
    if contenu[:2] == b"PK":
        try:
            with zipfile.ZipFile(_io.BytesIO(contenu)) as z:
                noms = z.namelist()
        except (zipfile.BadZipFile, OSError):
            return "inconnu"
        if any(n.startswith("word/") for n in noms):
            return "docx"
        if any(n.startswith("xl/") for n in noms):
            return "xls"
        return "zip"
    if contenu[:4] == b"\xd0\xcf\x11\xe0":
        return "xls" if Path(nom).suffix.lower() in (".xls", ".xlt") else "doc"
    if contenu[:5] == b"{\\rtf":
        return "rtf"
    if contenu[:4] == b"Rar!":
        return "rar"
    if contenu[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if (contenu[:3] == b"\xff\xd8\xff" or contenu[:4] == b"\x89PNG"
            or contenu[:4] in (b"II*\x00", b"MM\x00*") or contenu[:4] == b"GIF8"
            or contenu[8:12] == b"WEBP"):
        return "image"
    if re.match(rb"\s*(<!doctype|<html|<\?xml|<%)", contenu[:40], re.I):
        return "html"
    return "inconnu"


def meilleur_de_archive(contenu: bytes) -> tuple:
    """Le membre le plus prometteur d'une archive ZIP, et son nom. (None, '') si rien."""
    import zipfile
    try:
        with zipfile.ZipFile(_io.BytesIO(contenu)) as z:
            membres = [n for n in z.namelist() if not n.endswith("/")]
            for ext in PREFERENCES:
                candidats = [n for n in membres if n.lower().endswith(ext)]
                if candidats:
                    choisi = max(candidats, key=lambda n: z.getinfo(n).file_size)
                    return z.read(choisi), choisi
            if membres:
                choisi = max(membres, key=lambda n: z.getinfo(n).file_size)
                return z.read(choisi), choisi
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError):
        pass
    return None, ""


def meilleur_par_7z(septzip: str, archive: Path) -> tuple:
    """Même chose pour un .rar ou un .7z, que Python ne sait pas ouvrir seul."""
    import shutil
    import subprocess
    import tempfile
    dossier = Path(tempfile.mkdtemp(prefix="pv7z_"))
    try:
        subprocess.run([septzip, "x", "-y", f"-o{dossier}", str(archive)],
                       capture_output=True, timeout=180)
        fichiers = [f for f in dossier.rglob("*") if f.is_file()]
        for ext in PREFERENCES:
            candidats = [f for f in fichiers if f.suffix.lower() == ext]
            if candidats:
                choisi = max(candidats, key=lambda f: f.stat().st_size)
                return choisi.read_bytes(), choisi.name
        if fichiers:
            choisi = max(fichiers, key=lambda f: f.stat().st_size)
            return choisi.read_bytes(), choisi.name
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        shutil.rmtree(dossier, ignore_errors=True)
    return None, ""


def texte_de_docx(contenu: bytes) -> list:
    """Le texte d'un .docx, lu directement dans le document — exact, sans OCR.

    Une ligne de tableau devient une ligne de texte et une cellule un espacement : c'est
    exactement la forme que l'analyseur de PV attend (« SOCIETE X SARL   54 000,00 »).
    """
    import zipfile
    try:
        with zipfile.ZipFile(_io.BytesIO(contenu)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return []
    xml = re.sub(r"<w:tab[^>]*/>", "   ", xml)
    xml = re.sub(r"<w:br[^>]*/?>", "\n", xml)
    xml = re.sub(r"</w:tc>", "   ", xml)
    xml = re.sub(r"</w:(p|tr)>", "\n", xml)
    texte = unescape(re.sub(r"<[^>]+>", "", xml))
    lignes = [re.sub(r"[ \t]{2,}", "   ", l).strip() for l in texte.splitlines()]
    propre = "\n".join(l for l in lignes if l)
    return [propre] if len(propre) >= 40 else []


def pdf_de_images(contenu: bytes) -> bytes | None:
    """Une image — ou toutes les pages d'un TIF — assemblées en un PDF que l'OCR saura lire."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        image = Image.open(_io.BytesIO(contenu))
        pages = []
        while len(pages) < 12:
            pages.append(image.convert("RGB").copy())
            try:
                image.seek(image.tell() + 1)
            except EOFError:
                break
        if not pages:
            return None
        # On ramène chaque page à une taille raisonnable AVANT de l'enfermer dans le PDF.
        # Sinon un scan à 600 dpi produit une page de 25 x 35 pouces que l'OCR rendra ensuite
        # à 300 dpi, soit 78 millions de pixels : de quoi épuiser la mémoire de la machine.
        COTE = 3500
        ajustees = []
        for page in pages:
            if max(page.width, page.height) > COTE:
                f = COTE / max(page.width, page.height)
                page = page.resize((max(1, int(page.width * f)),
                                    max(1, int(page.height * f))), Image.LANCZOS)
            ajustees.append(page)
        tampon = _io.BytesIO()
        ajustees[0].save(tampon, "PDF", save_all=True, append_images=ajustees[1:],
                         resolution=300)
        return tampon.getvalue()
    except Exception:                                        # noqa: BLE001
        return None


def convertir_par_soffice(soffice: str, fichiers: list, sortie: Path) -> int:
    """Convertit en PDF un lot de documents bureautiques. Rend le nombre de PDF obtenus."""
    import subprocess
    if not fichiers:
        return 0
    sortie.mkdir(parents=True, exist_ok=True)
    profil = (RACINE / "_profil_soffice").resolve().as_uri()
    try:
        subprocess.run([soffice, "--headless", "--norestore", f"-env:UserInstallation={profil}",
                        "--convert-to", "pdf", "--outdir", str(sortie)]
                       + [str(f) for f in fichiers],
                       capture_output=True, timeout=120 + 30 * len(fichiers))
    except (subprocess.TimeoutExpired, OSError):
        return 0
    return sum(1 for f in fichiers if (sortie / f"{f.stem}.pdf").exists())


def pieces(limite: int, fils: int) -> None:
    """Reprend les pièces jointes qui ne sont pas des PDF nus : archives, Word, images.

    Le premier téléchargement n'acceptait que « %PDF » et jetait le reste. Or ce reste est du
    procès-verbal lui aussi — « Extrait de PV 3 BP 2026..doc », « Extrait PV AOO N° 25-2026
    001.tif », « Cadre des résultats et Extrait du PV ….zip ». Mesuré sur un échantillon de 50 :
    38 % d'archives, 36 % de .doc, 12 % de .docx, 10 % d'images. Soit 13 % de la récolte.

    Chaque pièce est ramenée à une forme que la suite de la chaîne sait lire :

        PDF dans une archive    l'archive est dépliée, le PDF part dans rattrapage\\pdf
        .docx                   le texte est lu dans le document même : exact, sans OCR
        .doc .rtf .xls          converti en PDF par LibreOffice, puis OCR
        .jpg .png .tif .gif     assemblées en PDF (toutes les pages d'un TIF), puis OCR
        .rar .7z                dépliées si 7-Zip est installé
        page HTML               ce n'est pas un document : le portail n'a rien servi

    Relançable : ce qui est déjà téléchargé ne l'est pas deux fois, ce qui est déjà converti non
    plus. Si LibreOffice manque, les .doc sont conservés et une relance après son installation
    les convertira sans rien redemander au portail.
    """
    PDF.mkdir(parents=True, exist_ok=True)
    PIECES.mkdir(parents=True, exist_ok=True)
    try:
        import pv_parser as analyseur
    except ImportError:
        analyseur = None

    # Les refus du téléchargement : ils portent ref, org et idAvis, donc tout ce qu'il faut pour
    # redemander le fichier. On écarte les refus de lecture, qui ont déjà leur PDF.
    attendus = []
    for f in sorted(REJETS.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "soumissionnaires" in d or not d.get("fichiers"):
            continue
        if (PDF / f"{d['ref']}.pdf").exists() or (PV / f"{d['ref']}.json").exists():
            continue
        attendus.append(d)
    if limite:
        attendus = attendus[:limite]

    soffice, septzip = _trouver_soffice(), _trouver_7z()
    dire(f"{len(attendus)} pièce(s) d'un autre type à reprendre")
    dire(f"  LibreOffice : {soffice or 'ABSENT — les .doc seront conservés sans être lus'}")
    dire(f"  7-Zip       : {septzip or 'absent — les .rar seront conservés sans être dépliés'}")
    if not attendus:
        return

    propre_fil = threading.local()

    def client():
        if not hasattr(propre_fil, "c"):
            propre_fil.c = tp.Client()
        return propre_fil.c

    def telecharger(d):
        """(fiche, contenu, erreur). Une pièce déjà sur le disque n'est pas redemandée."""
        deja = next(iter(PIECES.glob(f"{d['ref']}.*")), None)
        if deja is not None and deja.is_file():
            try:
                return d, deja.read_bytes(), None
            except OSError:
                pass
        url = URL_FICHIER.format(ref=d["ref"], org=d["org"], avis=d["fichiers"][-1]["idAvis"])
        try:
            with client().open(url) as r:
                contenu = r.read()
        except Exception as e:                               # noqa: BLE001
            time.sleep(PAUSE)
            return d, None, type(e).__name__
        time.sleep(PAUSE)
        return d, contenu, None

    comptes: dict = {}
    a_convertir: list = []
    lus_directement = pdf_obtenus = echecs = 0

    def noter(cle):
        comptes[cle] = comptes.get(cle, 0) + 1

    with ThreadPoolExecutor(max_workers=max(1, fils)) as ex:
        for i, (d, contenu, err) in enumerate(ex.map(telecharger, attendus), 1):
            ref = d["ref"]
            if err or not contenu:
                echecs += 1
                noter("téléchargement impossible")
                continue
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)',
                          d.get("nom_serveur") or "", re.I)
            nom = unescape(m.group(1)).strip() if m else ""

            g = genre(contenu, nom)
            if g in ("zip", "rar", "7z"):                    # une archive : on prend son contenu
                if g == "zip":
                    interieur, dedans = meilleur_de_archive(contenu)
                elif septzip:
                    chemin = PIECES / f"{ref}{EXTENSIONS[g]}"
                    ecrire_binaire(chemin, contenu)
                    interieur, dedans = meilleur_par_7z(septzip, chemin)
                else:
                    interieur, dedans = None, ""
                if interieur:
                    noter(f"{g} déplié")
                    contenu, nom, g = interieur, dedans, genre(interieur, dedans)
                else:
                    ecrire_binaire(PIECES / f"{ref}{EXTENSIONS[g]}", contenu)
                    noter(f"{g} non dépliable (gardé)")
                    continue

            if g == "pdf":
                ecrire_binaire(PDF / f"{ref}.pdf", contenu)
                pdf_obtenus += 1
                noter("PDF récupéré")
            elif g == "docx":
                pages = texte_de_docx(contenu) if analyseur else []
                if pages:
                    resultat = analyseur.parse(pages)
                    donnees, alertes, notes = (resultat if isinstance(resultat, tuple)
                                               and len(resultat) == 3 else (resultat, [], []))
                    e = en_forme_extrait(ref, d.get("org", ""), donnees,
                                         list(alertes), list(notes))
                    e["source"] = "extrait_docx"
                    qualite, _ = classer(e)
                    e["qualite"] = qualite
                    if qualite == "insuffisant":
                        ecrire_binaire(PIECES / f"{ref}.docx", contenu)
                        a_convertir.append(PIECES / f"{ref}.docx")
                        noter("docx sans rien d'exploitable -> à convertir pour relecture")
                    else:
                        # On garde le document même quand il a été lu : c'est la pièce d'origine,
                        # celle qu'on montrera si un chiffre du site est contesté.
                        ecrire_binaire(PIECES / f"{ref}.docx", contenu)
                        ecrire_json(PV / f"{ref}.json", completer_attribution(e))
                        lus_directement += 1
                        noter(f"docx lu directement ({qualite})")
                else:
                    ecrire_binaire(PIECES / f"{ref}.docx", contenu)
                    a_convertir.append(PIECES / f"{ref}.docx")
                    noter("docx sans couche texte -> à convertir")
            elif g == "image":
                en_pdf = pdf_de_images(contenu)
                if en_pdf:
                    ecrire_binaire(PDF / f"{ref}.pdf", en_pdf)
                    pdf_obtenus += 1
                    noter("image assemblée en PDF")
                else:
                    ecrire_binaire(PIECES / f"{ref}.img", contenu)
                    noter("image illisible (gardée)")
            elif g in ("doc", "rtf", "xls"):
                chemin = PIECES / f"{ref}{EXTENSIONS[g]}"
                ecrire_binaire(chemin, contenu)
                if soffice:
                    a_convertir.append(chemin)
                    noter(f"{g} -> à convertir")
                else:
                    noter(f"{g} gardé (LibreOffice absent)")
            elif g == "html":
                # On garde la page : sans elle on la redemanderait à chaque relance pour rien.
                ecrire_binaire(PIECES / f"{ref}.html", contenu)
                noter("le portail a servi une page web, pas un document")
            else:
                ecrire_binaire(PIECES / f"{ref}{EXTENSIONS[g]}", contenu)
                noter("type non reconnu (gardé)")

            if i % 100 == 0:
                dire(f"  {i}/{len(attendus)} — {pdf_obtenus} PDF, {lus_directement} lus"
                     f" directement, {len(a_convertir)} à convertir")

    # Les documents bureautiques, par lots : LibreOffice démarre lentement et convertit vite.
    if a_convertir and soffice:
        dire(f"conversion de {len(a_convertir)} document(s) par LibreOffice")
        sortie, convertis = PIECES / "_pdf", 0
        for debut in range(0, len(a_convertir), 40):
            lot = a_convertir[debut:debut + 40]
            convertir_par_soffice(soffice, lot, sortie)
            for f in lot:
                produit = sortie / f"{f.stem}.pdf"
                if produit.exists():
                    ecrire_binaire(PDF / f"{f.stem}.pdf", produit.read_bytes())
                    produit.unlink(missing_ok=True)
                    pdf_obtenus += 1
                    convertis += 1
            dire(f"  {min(debut + 40, len(a_convertir))}/{len(a_convertir)}"
                 f" — {convertis} converti(s) en PDF")
        noter(f"convertis en PDF par LibreOffice : {convertis}")

    dire("")
    for cle, n in sorted(comptes.items(), key=lambda x: -x[1]):
        dire(f"  {n:>5}  {cle}")
    dire("")
    dire(f"{pdf_obtenus} nouveau(x) PDF dans rattrapage\\pdf — à lire ensuite avec « --ocr »")
    dire(f"{lus_directement} document(s) Word lus directement, sans OCR")
    if echecs:
        dire(f"{echecs} téléchargement(s) en échec — relance « --pieces » pour les reprendre")
    if a_convertir and not soffice:
        dire("\nPour les .doc, installe LibreOffice puis relance « --pieces » :\n"
             "   winget install --id TheDocumentFoundation.LibreOffice\n"
             "Rien ne sera retéléchargé : les pièces sont déjà dans rattrapage\\pieces.")


def _langues_disponibles() -> list:
    try:
        import pytesseract
        return list(pytesseract.get_languages(config=""))
    except Exception:                                        # noqa: BLE001
        return []


def charger_ocr():
    try:
        import pv_ocr, pv_parser                             # noqa: E402
    except ImportError as e:
        sys.exit(f"{e}\n\npv_ocr.py et pv_parser.py sont dans C:\\pv\\archive : copie-les ici.")

    if "fra" not in _langues_disponibles():
        # Le dossier des modèles a pu être déplacé : on le cherche et on l'indique à Tesseract.
        for candidat in TESSDATA:
            dossier = (ICI / candidat).resolve() if not Path(candidat).is_absolute() else Path(candidat)
            if (dossier / "fra.traineddata").exists():
                os.environ["TESSDATA_PREFIX"] = str(dossier)
                dire(f"modèles de langue trouvés dans {dossier}")
                break
    langues = _langues_disponibles()
    if "fra" not in langues:
        sys.exit(
            "\nARRÊT : Tesseract ne trouve pas le modèle de langue française.\n\n"
            f"Langues vues : {', '.join(langues) or 'aucune'}\n"
            "Sans « fra », Tesseract rend des pages vides sans se plaindre, et tous les\n"
            "documents scannés paraissent illisibles.\n\n"
            "Pour réparer, copie les trois fichiers de C:\\pv\\archive\\tessdata vers le\n"
            "dossier tessdata de ton installation Tesseract :\n"
            "   copy C:\\pv\\archive\\tessdata\\*.traineddata "
            "\"%LOCALAPPDATA%\\Programs\\Tesseract-OCR\\tessdata\\\"\n")
    dire(f"Tesseract prêt — langues : {', '.join(sorted(langues))}")
    return pv_ocr, pv_parser


def en_forme_extrait(ref: str, org: str, d: dict, alertes: list, notes: list) -> dict:
    """Le même dictionnaire que extrait_pv.lire(), pour que construire_base.py n'ait rien à apprendre."""
    return {
        "source": "extrait_pdf_ocr", "present": True,
        "refConsultation_pv": None, "refConsultation_annonce": ref, "orgAcronyme": org,
        "reference_pv": d.get("numero_ao"), "nouveau": True,
        "reference": d.get("numero_ao"), "objet": d.get("objet"),
        "acheteur": d.get("maitre_ouvrage"), "procedure": d.get("procedure"),
        "categorie": "Travaux", "date_limite_plis": d.get("date_ouverture_plis"),
        "lieu_ouverture": d.get("lieu_ouverture_plis"), "allotissement": None,
        "estimation": None, "caution_provisoire": None,
        "date_achevement": d.get("date_achevement_travaux_commission"),
        "justification": d.get("justification"), "infructueux": bool(d.get("infructueux")),
        "attributaire": d.get("attributaire"), "montant_attribue": d.get("montant_attribue"),
        "attributaires_par_lot": d.get("attributaires_par_lot") or [],
        "soumissionnaires": d.get("soumissionnaires") or [],
        "nombre_soumissionnaires": d.get("nombre_soumissionnaires") or 0,
        "alertes_ocr": alertes, "notes_ocr": notes,
    }


def classer(e: dict) -> tuple[str, str]:
    """Ce que ce document apporte vraiment. Trois formes valent la peine d'être gardées.

    Le portail range sous « Annonce d'extrait de PV » trois documents différents, et ne les
    distingue pas :

      complet      le procès-verbal entier : les concurrents et leurs montants. C'est le seul
                   qui permette une moyenne, donc un prix de référence et un classement.
      attribution  une page de « Résultat de l'appel d'offres » : le gagnant et son montant,
                   sans les offres concurrentes. Dit qui a gagné quoi, et pour combien.
      participants un PV dont les offres financières n'ont pas été ouvertes — séance
                   interrompue, discordance relevée, marché annulé. Dit qui a soumissionné.

    Rejeter les deux dernières reviendrait à jeter les deux tiers de ce que le portail publie.
    """
    soum = e.get("soumissionnaires") or []
    chiffres = [c for c in soum if c.get("montant_acte_engagement")]
    perdants = [c for c in chiffres if c.get("statut") != "attributaire"]
    if e.get("infructueux") and not soum:
        return "infructueux", ""
    if len(chiffres) >= 2 and perdants:
        return "complet", ""
    if e.get("attributaire") and e.get("montant_attribue"):
        return "attribution", ""
    if chiffres:
        return "attribution", ""
    if soum:
        return "participants", ""
    return "insuffisant", "aucun concurrent ni attributaire lu"


def completer_attribution(e: dict) -> dict:
    """Un « Résultat d'appel d'offres » n'a pas de tableau de concurrents : l'attributaire en
    tient lieu, pour que la suite de la chaîne le traite comme n'importe quel marché attribué."""
    if e.get("soumissionnaires") or not e.get("attributaire"):
        return e
    e["soumissionnaires"] = [{"nom": e["attributaire"], "statut": "attributaire",
                              "montant_acte_engagement": e.get("montant_attribue"),
                              "montant_apres_verification": e.get("montant_attribue"),
                              "lots": []}]
    e["nombre_soumissionnaires"] = 1
    return e


# Tesseract découpe la page selon le mode qu'on lui donne, et le bon mode dépend de la mise en
# page du scan. « 4 » suppose une colonne de texte, « 6 » un bloc uniforme : sur les PV à tableaux
# bordurés, l'un réussit souvent là où l'autre rend une bouillie. Plutôt que de parier, on relit
# avec l'autre mode quand la première lecture ne donne rien d'exploitable.
MODES = ("--psm 4", "--psm 6", "--psm 3")

# Combien de documents on confie à un processus avant de le remplacer par un neuf. La panne
# observée arrivait vers le 160ᵉ document d'un même processus ; 60 laisse une marge confortable.
# Le coût du remplacement (démarrer 10 interpréteurs) est de quelques secondes pour une demi-heure
# de lecture : négligeable au regard d'une campagne perdue.
PAR_PROCESSUS = 60
# Le signal d'alarme : autant d'échecs techniques parmi les ALARME_SUR derniers résultats. On
# mesure sur une fenêtre et non « d'affilée », car les résultats de plusieurs processus
# s'entremêlent et une réussite isolée suffirait à remettre un compteur d'affilée à zéro.
ALARME_SUR, ALARME_SEUIL = 40, 30
# Si plus aucune lecture n'aboutit pendant ce délai, un processus est figé. Un document légitime
# demande au pire trois passes de 180 s, soit 9 minutes : 15 minutes ne peut pas être normal.
BLOCAGE = 900


def _fermer_pool(ex, brutalement: bool) -> None:
    """Rend le pool. « brutalement » quand un de ses processus ne répond plus.

    Sortir d'un « with ProcessPoolExecutor » appelle shutdown(wait=True), qui attendrait
    indéfiniment un processus figé — c'est ainsi qu'une campagne se bloque pour de bon. On
    termine donc les processus avant de rendre le pool.
    """
    if brutalement:
        for processus in list(getattr(ex, "_processes", {}).values()):
            try:
                processus.terminate()
            except Exception:                                # noqa: BLE001
                pass
    try:
        ex.shutdown(wait=not brutalement, cancel_futures=True)
    except Exception:                                        # noqa: BLE001
        pass

_MOTEUR = None                  # pv_ocr, chargé une seule fois par processus de lecture
_ANALYSEUR = None               # pv_parser, idem


def _ouvrier(tessdata: str) -> None:
    """Préparation d'un processus de lecture.

    pdfium n'est pas prévu pour être appelé depuis plusieurs fils d'exécution d'un même processus.
    Mesuré sur cette machine : au bout de ~160 documents lus par 10 fils, son état interne est
    perdu et TOUS les suivants sont refusés avec « PDFium: Data format error » — alors que les
    fichiers sont intacts (vérifié : %PDF-1.4, %%EOF en place, trois pages lues sans peine par
    ailleurs). Un processus par lecteur supprime la cause : chacun a sa bibliothèque et sa
    mémoire, et ce que l'un abîme ne touche pas les autres.
    """
    global _MOTEUR, _ANALYSEUR
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata
    import pv_ocr
    import pv_parser
    _MOTEUR, _ANALYSEUR = pv_ocr, pv_parser


def _lire_un(chemin: str) -> tuple:
    """Lit un document et rend des données simples, transmissibles d'un processus à l'autre.

    Rend (chemin, données du PV ou None, alertes, notes, erreur ou None). C'est le chemin et non
    la référence, parce que le nom du fichier change selon qu'il est rangé ou non : la référence
    est recalculée par l'appelant.
    """
    f = Path(chemin)
    ref = reference_de(f)
    try:
        donnees = f.read_bytes()
    except OSError as e:
        return chemin, None, [], [], f"{type(e).__name__}: {e}"

    repli, erreur = None, None
    for mode in MODES:
        try:
            _MOTEUR.TESS_CONFIG = mode
            pages, _ = _MOTEUR.read_pages(donnees, max_pages=12)
            resultat = _ANALYSEUR.parse(pages)
        except Exception as e:                               # noqa: BLE001
            erreur = erreur or f"{type(e).__name__}: {e}"
            continue
        d, alertes, notes = (resultat if isinstance(resultat, tuple) and len(resultat) == 3
                             else (resultat, [], []))
        if classer(en_forme_extrait(ref, "", d, [], []))[0] != "insuffisant":
            return chemin, d, list(alertes), list(notes), None
        repli = (d, list(alertes), list(notes))              # lu, mais rien d'exploitable
    if repli is not None:
        return (chemin, *repli, None)
    return chemin, None, [], [], erreur or "aucune lecture"


def reference_de(chemin: Path) -> str:
    """La référence d'annonce, quelle que soit la convention de nommage du fichier.

        671672.pdf                      tel que le téléchargement l'écrit
        115-FLSHM-2026__671672.pdf      tel que ranger_pv.py le renomme
    """
    m = re.search(r"__(\d{5,10})$", chemin.stem)
    return m.group(1) if m else chemin.stem


def documents_sur_disque() -> list:
    """Tous les documents lisibles, avant comme après le rangement.

    Une fois ranger_pv.py passé, « rattrapage\\pdf » est vide et tout est sous
    « moisson\\pdf\\<verdict>\\ ». Sans regarder là aussi, « --ocr --reprendre » ne trouverait
    plus rien à relire — et c'est précisément là que dorment les documents à reprendre.
    """
    trouves = list(PDF.glob("*.pdf"))
    rangee = MOISSON / "pdf"
    if rangee.is_dir():
        trouves += [f for f in rangee.glob("*/*.pdf") if f.is_file()]
    return sorted(trouves, key=lambda f: reference_de(f))


def _a_lire(reprendre: bool) -> list:
    """Les documents qui restent à lire : ceux dont on n'a encore ni fiche ni rejet.

    « --reprendre » a déjà effacé, juste avant, les refus qui méritent une seconde lecture. Les
    refus qui subsistent sont donc exactement ceux qu'il ne faut pas relire — un document sur
    lequel la lecture se fige, par exemple, figerait de nouveau.
    """
    del reprendre                                            # le tri a été fait en amont
    return [f for f in documents_sur_disque()
            if not (PV / f"{reference_de(f)}.json").exists()
            and not (REJETS / f"{reference_de(f)}.json").exists()]


def ocr(limite: int, fils: int = 4, reprendre: bool = False) -> None:
    charger_ocr()                                            # vérifie Tesseract et « fra » d'abord
    tessdata = os.environ.get("TESSDATA_PREFIX", "")
    PV.mkdir(parents=True, exist_ok=True)
    REJETS.mkdir(parents=True, exist_ok=True)

    if reprendre:
        # Tout rejet dont le PDF est encore là repasse en lecture : les refus de contenu comme
        # les pannes techniques. (La version précédente ne reprenait que les refus de contenu,
        # ce qui aurait condamné définitivement les 29 190 fichiers victimes du pdfium cassé.)
        efface = 0
        presents = {reference_de(f) for f in documents_sur_disque()}
        for r in REJETS.glob("*.json"):
            if r.stem not in presents:
                continue
            try:
                if "se fige" in (json.loads(r.read_text(encoding="utf-8")).get("probleme") or ""):
                    continue                                 # il figerait de nouveau
            except (OSError, ValueError):
                pass
            r.unlink(missing_ok=True)
            efface += 1
        dire(f"{efface} lecture(s) ratée(s) remise(s) en file")

    fichiers = _a_lire(reprendre)
    if limite:
        fichiers = fichiers[:limite]
    dire(f"{len(fichiers)} PDF à lire — {fils} processus de lecture")
    if not fichiers:
        return

    orgs = {a["ref"]: a["org"] for a in annonces_listees()}
    pris = rejetes = faits = techniques = 0
    formes: dict[str, int] = {}
    fenetre: deque = deque(maxlen=ALARME_SUR)
    figes: dict = {}                    # documents sur lesquels la lecture s'est figée
    derniere_erreur = ""
    t0 = time.time()
    restants = list(fichiers)
    tranche_taille = max(20, PAR_PROCESSUS * max(1, fils))

    def enregistrer(ref, d, alertes, notes, erreur) -> None:
        nonlocal pris, rejetes, techniques, derniere_erreur
        fenetre.append(bool(erreur))
        if erreur:
            ecrire_json(REJETS / f"{ref}.json", {"ref": ref, "probleme": erreur})
            echecs_tranche.append(ref)
            rejetes += 1
            techniques += 1
            derniere_erreur = erreur
            return
        e = en_forme_extrait(ref, orgs.get(ref, ""), d, list(alertes), list(notes))
        qualite, pourquoi = classer(e)
        e["qualite"] = qualite
        if qualite == "insuffisant":
            ecrire_json(REJETS / f"{ref}.json", {**e, "probleme": pourquoi})
            rejetes += 1
        else:
            ecrire_json(PV / f"{ref}.json", completer_attribution(e))
            pris += 1
            formes[qualite] = formes.get(qualite, 0) + 1

    def non_traites(lot) -> list:
        return [f for f in lot if not (PV / f"{reference_de(f)}.json").exists()
                and not (REJETS / f"{reference_de(f)}.json").exists()]

    arret, secours, pris_a_lalarme = False, 0, -1
    echecs_tranche: list = []
    while restants and not arret:
        tranche, restants = restants[:tranche_taille], restants[tranche_taille:]
        fenetre.clear()                                      # processus neufs : on repart à zéro
        echecs_tranche = []
        rendre, ex = False, ProcessPoolExecutor(max_workers=max(1, fils),
                                                initializer=_ouvrier, initargs=(tessdata,))
        try:
            lances = {ex.submit(_lire_un, str(f)): f for f in tranche}
            pendantes = set(lances)
            while pendantes and not rendre and not arret:
                # On attend qu'une lecture quelconque aboutisse. Si plus rien n'aboutit pendant
                # BLOCAGE, un processus est figé — Tesseract peut rester suspendu quand la
                # mémoire a manqué dans le fil qui lit sa sortie. On ne peut pas l'attendre :
                # on le remplace.
                finies, pendantes = wait(pendantes, timeout=BLOCAGE,
                                         return_when=FIRST_COMPLETED)
                if not finies:
                    dire(f"  aucune lecture n'aboutit depuis {BLOCAGE // 60} min — un processus"
                         f" est figé ; on le remplace et on reprend les documents en attente")
                    for en_attente in (lances[fu] for fu in pendantes):
                        cle = reference_de(en_attente)
                        figes[cle] = figes.get(cle, 0) + 1
                    rendre = True
                    break
                for future in finies:
                    try:
                        chemin, d, alertes, notes, erreur = future.result()
                    except Exception as e:                   # noqa: BLE001
                        dire(f"  lecture perdue ({type(e).__name__}) — document repris plus tard")
                        continue
                    ref = reference_de(Path(chemin))
                    faits += 1
                    enregistrer(ref, d, alertes, notes, erreur)
                    if faits % 20 == 0:
                        reste = (time.time() - t0) / faits * (len(fichiers) - faits) / 60
                        dire(f"  {faits}/{len(fichiers)} — {pris} retenus, {rejetes} rejetés"
                             f" (reste ~{reste:.0f} min)")
                    if len(fenetre) == ALARME_SUR and sum(fenetre) >= ALARME_SEUIL:
                        # Une rafale d'échecs techniques : le plus probable est que la
                        # bibliothèque de lecture s'est perdue dans ces processus-ci. On les
                        # remplace et on redonne leur chance aux documents non traités —
                        # y compris ceux que la rafale vient de marquer, dont le refus ne
                        # prouve rien : on efface donc ces refus-là avant de les remettre en file.
                        sterile = (pris == pris_a_lalarme)
                        pris_a_lalarme, secours = pris, secours + 1
                        for perdu in echecs_tranche:
                            (REJETS / f"{perdu}.json").unlink(missing_ok=True)
                        rejetes -= len(echecs_tranche)
                        techniques -= len(echecs_tranche)
                        faits -= len(echecs_tranche)
                        echecs_tranche = []
                        rendre = True
                        if secours >= 3 and sterile:
                            arret = True
                            dire(f"  {secours}ᵉ rafale d'échecs, et le renouvellement des"
                                 f" processus n'y change rien : on arrête")
                        else:
                            dire("  rafale d'échecs techniques — processus de lecture"
                                 " renouvelés, on reprend")
                        break
        except BrokenProcessPool:
            # Un processus est mort (mémoire, bibliothèque native). Les documents de la tranche
            # qui n'ont pas abouti repartent avec un pool neuf ; rien n'est perdu.
            dire("  un processus de lecture est mort — documents repris avec un pool neuf")
            rendre = True
        except KeyboardInterrupt:
            dire("  interrompu — ce qui est lu est conservé, relance pour reprendre")
            arret = rendre = True
        finally:
            _fermer_pool(ex, brutalement=rendre)
        if rendre and not arret:
            a_refaire, ecartes = [], 0
            for f in non_traites(tranche):
                if figes.get(reference_de(f), 0) >= 2:
                    # Deux blocages sur le même document : ce n'est pas la faute des processus.
                    # On l'écarte, sinon la campagne le relancerait indéfiniment.
                    ecrire_json(REJETS / f"{reference_de(f)}.json",
                                {"ref": reference_de(f),
                                 "probleme": "la lecture se fige sur ce document"})
                    rejetes += 1
                    ecartes += 1
                else:
                    a_refaire.append(f)
            restants = a_refaire + restants
            dire(f"  {len(a_refaire)} document(s) remis en file"
                 + (f", {ecartes} écarté(s) pour blocage répété" if ecartes else ""))

    detail = ", ".join(f"{n} {k}" for k, n in sorted(formes.items(), key=lambda x: -x[1]))
    dire(f"{pris} documents retenus ({detail or 'aucun'}), {rejetes} sans rien d'exploitable")
    if arret and secours:
        dire(f"\nARRÊT : les échecs ne viennent pas des documents.\n"
             f"  dernière erreur : {derniere_erreur}\n"
             f"  {len(restants)} document(s) attendent encore.\n"
             "  Corrige la cause, puis relance avec --reprendre : les refus techniques\n"
             "  de cette campagne repasseront tous en lecture.")
    elif restants:
        dire(f"{len(restants)} document(s) non traités — relance la même commande pour continuer")
    if not arret and techniques:
        dire(f"{techniques} refus pour cause technique (et non de contenu) : « --reprendre »"
             f" les remettra en lecture.")


# ------------------------------------------------------------------- rapport

def rapport() -> None:
    def compte(d, motif):
        return len(list(d.glob(motif))) if d.is_dir() else 0

    n_annonces = len(annonces_listees()) if ANNONCES.exists() else 0
    n_avis, n_pdf = compte(AVIS, "*.json"), compte(PDF, "*.pdf")
    n_pv, n_rejets = compte(PV, "*.json"), compte(REJETS, "*.json")
    avec_fichier = 0
    for f in AVIS.glob("*.json") if AVIS.is_dir() else []:
        try:
            avec_fichier += bool(json.loads(f.read_text(encoding="utf-8")).get("fichiers"))
        except (OSError, ValueError):
            pass
    pct = lambda a, b: f"{100*a/b:>5.0f} %" if b else "      "
    dire("=== OÙ EN EST LE RATTRAPAGE ===")
    print(f"  annonces d'extrait de PV listées      {n_annonces:>7}")
    print(f"  annonces ouvertes                     {n_avis:>7} {pct(n_avis, n_annonces)}")
    print(f"    dont avec un fichier joint          {avec_fichier:>7} {pct(avec_fichier, n_avis)}")
    print(f"  PDF téléchargés                       {n_pdf:>7} {pct(n_pdf, avec_fichier)}")
    print(f"  PV analysés et exploitables           {n_pv:>7} {pct(n_pv, n_pdf)}")
    print(f"  rejetés (à regarder dans rejets\\)     {n_rejets:>7}")

    if n_pv:
        formes, avec_alertes = {}, 0
        for f in PV.glob("*.json"):
            try:
                e = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            formes[e.get("qualite", "?")] = formes.get(e.get("qualite", "?"), 0) + 1
            avec_alertes += bool(e.get("alertes_ocr"))
        libelles = {
            "complet": "PV entier : concurrents et montants",
            "attribution": "résultat : le gagnant et son montant",
            "participants": "PV sans ouverture financière : qui a soumissionné",
            "infructueux": "déclaré infructueux",
        }
        print(f"\n  ce que les {n_pv} documents retenus apportent :")
        for k, n in sorted(formes.items(), key=lambda x: -x[1]):
            print(f"    {libelles.get(k, k):<44}{n:>7} {pct(n, n_pv)}")
        print(f"    l'analyseur signale un doute                {avec_alertes:>7} {pct(avec_alertes, n_pv)}")
        print("\n  « PV entier » est le seul qui permette un prix de référence et un classement.")
        print("  Les autres enrichissent quand même le site : marché, objet, acheteur, gagnant.")


def installer() -> None:
    """Verse les PV acceptés dans ..\\extraits, d'où construire_base.py les lira sans rien changer."""
    cible = DONNEES / "extraits"
    cible.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(PV.glob("*.json")):
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        destination = cible / f"c{e['refConsultation_annonce']}.json"
        ecrire_json(destination, e)
        n += 1
    dire(f"{n} PV versés dans {cible}")
    dire("Reconstruis ensuite la base :  python construire_base.py --base travaux")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--liste", action="store_true")
    ap.add_argument("--avis", action="store_true")
    ap.add_argument("--pdf", action="store_true")
    ap.add_argument("--fiches", action="store_true")
    ap.add_argument("--pieces", action="store_true",
                    help="reprend les pièces jointes qui ne sont pas des PDF")
    ap.add_argument("--ocr", action="store_true")
    ap.add_argument("--rapport", action="store_true")
    ap.add_argument("--installer", action="store_true")
    ap.add_argument("--essai", type=int, default=0, metavar="N",
                    help="les quatre étapes sur N annonces seulement, puis le rapport")
    ap.add_argument("--limite", type=int, default=0, metavar="N")
    ap.add_argument("--fils", type=int, default=3, metavar="N",
                    help="requêtes simultanées vers le portail (défaut 3)")
    ap.add_argument("--reprendre", action="store_true",
                    help="refaire les lectures qui avaient échoué (après avoir réparé Tesseract)")
    ap.add_argument("--coeurs", type=int, default=0, metavar="N",
                    help="documents lus en parallèle par l'OCR (défaut : tous les cœurs sauf un)")
    ap.add_argument("--depuis", default="01/01/2022", metavar="JJ/MM/AAAA")
    ap.add_argument("--jusqua", default="31/12/2027", metavar="JJ/MM/AAAA")
    a = ap.parse_args()

    RACINE.mkdir(parents=True, exist_ok=True)
    nettoyer_tmp()
    if not a.coeurs:
        a.coeurs = max(1, (os.cpu_count() or 2) - 1)
    limite = a.essai or a.limite

    if a.essai:
        dire(f"=== ESSAI SUR {a.essai} ANNONCES ===")
        if not ANNONCES.exists():
            liste(a.depuis, a.jusqua)
        avis(limite, a.fils)
        pdf(limite, a.fils)
        ocr(limite, a.coeurs, a.reprendre)
        rapport()
        return

    fait = False
    for drapeau, action in ((a.liste, lambda: liste(a.depuis, a.jusqua)),
                            (a.avis, lambda: avis(limite, a.fils)),
                            (a.pdf, lambda: pdf(limite, a.fils)),
                            (a.pieces, lambda: pieces(limite, a.fils)),
                            (a.fiches, lambda: fiches(a.fils)),
                            (a.ocr, lambda: ocr(limite, a.coeurs, a.reprendre)),
                            (a.installer, installer),
                            (a.rapport, rapport)):
        if drapeau:
            action()
            fait = True
    if not fait:
        rapport()


if __name__ == "__main__":
    # « spawn » : chaque processus de lecture démarre d'un interpréteur neuf. C'est le seul mode
    # sous Windows, et on l'impose aussi ailleurs — dupliquer un processus qui a déjà chargé
    # pdfium, OpenCV et Tesseract (« fork ») laisse ces bibliothèques natives dans un état
    # qu'elles n'ont pas prévu.
    import multiprocessing

    multiprocessing.freeze_support()
    try:
        multiprocessing.set_start_method("spawn")
    except RuntimeError:
        pass
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu. Relance la même commande : rien de ce qui est fait n'est refait.")
    except Exception as e:                                   # noqa: BLE001
        print(f"\nARRÊT : {type(e).__name__}: {e}")
        print("Relance la même commande : la reprise est automatique.")
        sys.exit(1)
