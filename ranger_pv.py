"""Range la récolte de procès-verbaux : un dossier par type de fichier, un sous-dossier par verdict.

Jusqu'ici tout tombait à plat : « rattrapage\\pdf\\671672.pdf » à côté de 29 492 autres, les
verdicts éparpillés dans deux dossiers de JSON, et les pièces qui n'étaient pas des PDF jetées
sans trace de ce qu'elles étaient. Impossible de voir d'un coup d'œil ce qui est exploitable,
ce qui ne l'est pas, et pourquoi.

Ce programme ne télécharge rien, ne lit aucun PV, ne touche pas au réseau. Il regarde ce qui
est déjà sur le disque, reconnaît chaque fichier à ses premiers octets (et non à son nom, qui
ment souvent), va chercher le verdict de lecture déjà produit, et range :

    moisson\\
      pdf\\     complet\\       le procès-verbal entier : concurrents + montants
                attribution\\   le gagnant et son montant, sans les offres concurrentes
                participants\\  les concurrents, offres financières non ouvertes
                infructueux\\   déclaré sans suite
                illisible\\     la lecture n'a rien rendu  -> à regarder toi-même
                non_lu\\        pas encore passé à la lecture
      zip\\     (les mêmes sous-dossiers)
      word\\    (idem)
      image\\   (idem)
      autre\\   (idem)
      index.csv        une ligne par document : référence du marché, type, verdict, chemin
      a_inspecter.csv  seulement les illisibles, avec le lien du portail

Les fichiers sont renommés avec la référence du marché, pour être reconnaissables sans ouvrir
l'index : « 115-FLSHM-2026__1047184.pdf ».

    cd pv_travaux\\site
    python ranger_pv.py --etat       ne déplace rien, affiche seulement le recensement
    python ranger_pv.py              range pour de bon (déplace, n'efface jamais)
    python ranger_pv.py --copier     range en copiant, si tu veux garder l'ancien dossier

« --etat » ne touche à rien : il est sans danger même pendant que la lecture OCR tourne.
Le rangement, lui, s'exécute une fois la lecture terminée.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import zipfile
from html import unescape
from pathlib import Path

ICI = Path(__file__).resolve().parent
DONNEES = ICI.parent
RATTRAPAGE = ICI / "rattrapage"
MOISSON = DONNEES / "moisson"

# Les dossiers où dorment les documents déjà récupérés, par ordre de préférence.
SOURCES = (RATTRAPAGE / "pdf", RATTRAPAGE / "pieces", DONNEES / "PV")
# Les verdicts de lecture, déjà calculés par rattraper_pv.py.
FICHES = RATTRAPAGE / "pv"
ECHECS = RATTRAPAGE / "rejets"
ANNONCES = RATTRAPAGE / "annonces.csv"

VERDICTS = ("complet", "attribution", "participants", "infructueux", "illisible", "non_lu")
CONTENEURS = ("pdf", "zip", "word", "tableur", "image", "html", "autre")

REFERENCE_MARCHE = re.compile(r"\d{2}/\d{2}/\d{4}\s+(.{2,40}?)\s+-\s+\.\.\.")
INTERDITS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PORTAIL = ("https://www.marchespublics.gov.ma/index.php"
           "?page=entreprise.EntrepriseDetailConsultation&refConsultation={ref}&orgAcronyme={org}")


# ------------------------------------------------------------- reconnaissance

def conteneur(chemin: Path, debut: bytes) -> str:
    """Ce qu'est vraiment le fichier, d'après ses premiers octets.

    L'extension du nom ne suffit pas : le portail sert des « .pdf » qui sont des pages d'erreur
    HTML, et des « .zip » qui sont en réalité des .docx. Les octets, eux, ne mentent pas.
    """
    if debut.startswith(b"%PDF"):
        return "pdf"
    if debut.startswith(b"PK\x03\x04"):
        # Un .docx et un .xlsx sont des archives zip ; ce qu'il y a dedans les distingue.
        try:
            with zipfile.ZipFile(chemin) as z:
                noms = z.namelist()[:40]
        except (OSError, zipfile.BadZipFile):
            return "zip"
        if any(n.startswith("word/") for n in noms):
            return "word"
        if any(n.startswith("xl/") for n in noms):
            return "tableur"
        if any(n.startswith("ppt/") for n in noms):
            return "autre"
        return "zip"
    if debut.startswith(b"\xd0\xcf\x11\xe0"):
        # Un document Office d'avant 2007 : .doc, .xls. Le nom tranche quand il est là.
        return "tableur" if chemin.suffix.lower() in (".xls", ".xlt") else "word"
    if debut.startswith(b"{\\rtf"):
        return "word"
    if debut.startswith(b"Rar!") or debut.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "zip"
    if (debut.startswith(b"\xff\xd8\xff") or debut.startswith(b"\x89PNG")
            or debut[:4] in (b"II*\x00", b"MM\x00*") or debut.startswith(b"GIF8")
            or debut[8:12] == b"WEBP"):
        return "image"
    # Le portail renvoie parfois une page web au lieu du fichier : session expirée, pièce
    # retirée. Ce n'est pas un document d'un type exotique, c'est l'absence de document.
    if re.match(rb"\s*(<!doctype|<html|<\?xml|<%)", debut, re.I):
        return "html"
    return "autre"


def extension(conten: str, nom_serveur: str, actuelle: str) -> str:
    """L'extension à donner au fichier rangé : celle du serveur si elle est cohérente."""
    du_serveur = Path(nom_serveur).suffix.lower() if nom_serveur else ""
    attendues = {"pdf": (".pdf",), "zip": (".zip", ".rar", ".7z"),
                 "word": (".doc", ".docx", ".rtf"), "tableur": (".xls", ".xlsx"),
                 "image": (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".webp"),
                 "html": (".html", ".htm")}
    for candidate in (du_serveur, actuelle.lower()):
        if candidate and candidate in attendues.get(conten, ()):
            return candidate
    defaut = {"pdf": ".pdf", "zip": ".zip", "word": ".doc", "tableur": ".xls",
              "image": ".jpg", "html": ".html", "autre": ".bin"}
    return defaut[conten]


# ------------------------------------------------------------- ce qu'on sait déjà

def references_des_annonces() -> dict[str, tuple[str, str]]:
    """refAnnonce -> (référence du marché, acronyme de l'acheteur), lu dans annonces.csv."""
    out: dict[str, tuple[str, str]] = {}
    if not ANNONCES.exists():
        return out
    with ANNONCES.open(encoding="utf-8-sig", newline="") as f:
        csv.field_size_limit(10 ** 7)
        for l in csv.DictReader(f, delimiter=";"):
            m = REFERENCE_MARCHE.search(unescape(l.get("texte") or ""))
            out[l["refConsultation"]] = (m.group(1).strip() if m else "",
                                         l.get("orgAcronyme") or "")
    return out


def verdicts() -> tuple[dict[str, str], dict[str, str]]:
    """refAnnonce -> verdict de lecture, et refAnnonce -> raison quand la lecture a échoué."""
    quoi: dict[str, str] = {}
    pourquoi: dict[str, str] = {}
    for f in (FICHES.glob("*.json") if FICHES.is_dir() else []):
        try:
            quoi[f.stem] = json.loads(f.read_text(encoding="utf-8")).get("qualite") or "complet"
        except (OSError, ValueError):
            quoi[f.stem] = "complet"
    for f in (ECHECS.glob("*.json") if ECHECS.is_dir() else []):
        if f.stem in quoi:
            # Une lecture a abouti depuis : le refus est périmé. C'est le cas des 990 documents
            # Word, refusés au téléchargement parce qu'ils n'étaient pas des PDF, puis lus
            # directement dans le document. Les ranger sous « illisible » reviendrait à mettre
            # au rebut les lectures les plus sûres de toute la récolte.
            continue
        quoi[f.stem] = "illisible"
        try:
            pourquoi[f.stem] = str(json.loads(f.read_text(encoding="utf-8")).get("probleme") or "")
        except (OSError, ValueError):
            pourquoi[f.stem] = ""
    return quoi, pourquoi


def nom_serveurs() -> dict[str, str]:
    """refAnnonce -> nom du fichier tel que le serveur l'a nommé (il dit souvent « Extrait PV »)."""
    out: dict[str, str] = {}
    for dossier in (RATTRAPAGE / "avis", ECHECS):
        for f in (dossier.glob("*.json") if dossier.is_dir() else []):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            brut = d.get("nom_serveur") or ""
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', brut, re.I)
            nom = unescape(m.group(1)).strip() if m else (brut if "." in brut else "")
            if nom:
                out.setdefault(f.stem, nom)
    return out


# ------------------------------------------------------------- le rangement

def reference_du_fichier(chemin: Path) -> str:
    """La référence d'annonce, quelle que soit la convention de nommage du dossier d'origine.

    Trois conventions cohabitent sur le disque et désignent la même chose :
        671672.pdf                              le rattrapage Travaux
        1000003_g3h__EXTRAIT DE PV.pdf          la récolte Services
        115-FLSHM-2026__671672.pdf              ce que ce programme écrit
    Dans les deux premières l'annonce est le nombre qui ouvre le nom ; dans la troisième c'est
    celui qui le ferme. On lit donc la fin d'abord, puis le début.
    """
    m = re.search(r"__(\d{5,10})$", chemin.stem)
    if m:
        return m.group(1)
    m = re.match(r"(\d{5,10})", chemin.stem)
    return m.group(1) if m else chemin.stem


def nom_range(ref: str, marche: str, ext: str) -> str:
    base = INTERDITS.sub("-", marche).strip(" .-") if marche else ""
    base = re.sub(r"\s+", " ", base)[:60]
    return f"{base}__{ref}{ext}" if base else f"sans-reference__{ref}{ext}"


def documents() -> list[Path]:
    """Tous les documents du disque, où qu'ils soient, un seul par annonce.

    L'arborescence déjà rangée fait partie des sources : c'est ce qui permet de relancer ce
    programme après une nouvelle lecture OCR, pour que les fichiers passent de « non_lu » au
    verdict qu'ils viennent d'obtenir. Un document rangé qui n'a pas changé de verdict ne bouge
    pas : l'opération est donc sans risque, autant de fois qu'on veut.
    """
    vus: dict[str, Path] = {}
    for source in SOURCES:
        if not source.is_dir():
            continue
        for f in sorted(source.iterdir()):
            if f.is_file():
                vus.setdefault(reference_du_fichier(f), f)
    for conten in CONTENEURS:                                # puis ce qui est déjà rangé
        for verdict in VERDICTS:
            dossier = MOISSON / conten / verdict
            if not dossier.is_dir():
                continue
            for f in sorted(dossier.iterdir()):
                if f.is_file():
                    vus.setdefault(reference_du_fichier(f), f)
    return list(vus.values())


def ranger(etat_seulement: bool, copier: bool) -> None:
    t0 = time.time()
    refs = references_des_annonces()
    quoi, pourquoi = verdicts()
    noms = nom_serveurs()

    presents = documents()
    if not presents:
        print("Aucun document trouvé. Dossiers cherchés :")
        for s in SOURCES:
            print(f"  {s}  {'(absent)' if not s.is_dir() else ''}")
        return

    print(f"{len(presents)} document(s) sur le disque"
          + (" — recensement seulement, rien ne sera déplacé" if etat_seulement else ""))

    grille: dict[tuple[str, str], int] = {}
    octets: dict[str, int] = {}
    lignes: list[dict] = []
    a_inspecter: list[dict] = []
    deplaces = erreurs = 0

    for i, f in enumerate(presents, 1):
        try:
            with f.open("rb") as fh:
                debut = fh.read(16)
            taille = f.stat().st_size
        except OSError as e:
            erreurs += 1
            print(f"  illisible sur le disque : {f.name} ({type(e).__name__})")
            continue

        ref = reference_du_fichier(f)
        conten = conteneur(f, debut)
        verdict = quoi.get(ref, "non_lu")
        marche, org = refs.get(ref, ("", ""))
        nom_serveur = noms.get(ref, "")

        grille[(conten, verdict)] = grille.get((conten, verdict), 0) + 1
        octets[conten] = octets.get(conten, 0) + taille

        cible = MOISSON / conten / verdict / nom_range(ref, marche, extension(conten, nom_serveur, f.suffix))
        lignes.append({"reference_marche": marche, "ref_annonce": ref, "acheteur": org,
                       "type_fichier": conten, "verdict": verdict,
                       "probleme": pourquoi.get(ref, ""), "octets": taille,
                       "nom_serveur": nom_serveur,
                       "chemin": str(cible.relative_to(DONNEES)) if not etat_seulement else str(f)})
        if verdict in ("illisible", "non_lu") or conten != "pdf":
            a_inspecter.append({"reference_marche": marche, "ref_annonce": ref,
                                "type_fichier": conten, "verdict": verdict,
                                "probleme": pourquoi.get(ref, ""),
                                "fichier": cible.name, "nom_serveur": nom_serveur,
                                "lien_portail": PORTAIL.format(ref=ref, org=org)})

        if not etat_seulement:
            try:
                cible.parent.mkdir(parents=True, exist_ok=True)
                if cible.exists() and cible.resolve() == f.resolve():
                    pass                                     # déjà à sa place
                elif copier:
                    shutil.copy2(f, cible)
                    deplaces += 1
                else:
                    os.replace(f, cible) if f.drive == cible.drive else shutil.move(str(f), str(cible))
                    deplaces += 1
            except OSError as e:
                erreurs += 1
                print(f"  échec du rangement de {f.name} : {type(e).__name__}: {e}")

        if i % 2000 == 0:
            print(f"  {i}/{len(presents)}…")

    # ------------------------------------------------------------- le tableau
    print(f"\n{'':<10}" + "".join(f"{v:>14}" for v in VERDICTS) + f"{'total':>10}{'poids':>10}")
    for c in CONTENEURS:
        total = sum(grille.get((c, v), 0) for v in VERDICTS)
        if not total:
            continue
        print(f"{c:<10}" + "".join(f"{grille.get((c, v), 0) or '':>14}" for v in VERDICTS)
              + f"{total:>10}{octets.get(c, 0) / 1e9:>9.1f}G")
    tot = {v: sum(grille.get((c, v), 0) for c in CONTENEURS) for v in VERDICTS}
    print(f"{'TOTAL':<10}" + "".join(f"{tot[v] or '':>14}" for v in VERDICTS)
          + f"{sum(tot.values()):>10}{sum(octets.values()) / 1e9:>9.1f}G")

    exploitables = tot["complet"] + tot["attribution"] + tot["participants"]
    print(f"\n  exploitables (un gagnant ou des concurrents nommés) : {exploitables}")
    print(f"  dont un prix de référence est possible (complet)     : {tot['complet']}")
    print(f"  à regarder toi-même (illisible)                      : {tot['illisible']}")
    print(f"  pas encore passés à la lecture                       : {tot['non_lu']}")

    # ------------------------------------------------------------- les index
    MOISSON.mkdir(parents=True, exist_ok=True)
    champs = ["reference_marche", "ref_annonce", "acheteur", "type_fichier", "verdict",
              "probleme", "octets", "nom_serveur", "chemin"]
    index = MOISSON / ("index_etat.csv" if etat_seulement else "index.csv")
    with index.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=champs, delimiter=";")
        w.writeheader()
        w.writerows(sorted(lignes, key=lambda l: (l["type_fichier"], l["verdict"],
                                                  l["reference_marche"])))
    print(f"\n-> {index}  ({len(lignes)} lignes)")

    if a_inspecter:
        champs2 = ["reference_marche", "ref_annonce", "type_fichier", "verdict", "probleme",
                   "fichier", "nom_serveur", "lien_portail"]
        chemin2 = MOISSON / ("a_inspecter_etat.csv" if etat_seulement else "a_inspecter.csv")
        with chemin2.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=champs2, delimiter=";")
            w.writeheader()
            w.writerows(a_inspecter)
        print(f"-> {chemin2}  ({len(a_inspecter)} cas à regarder, avec le lien du portail)")

    if not etat_seulement:
        for provisoire in ("index_etat.csv", "a_inspecter_etat.csv"):
            (MOISSON / provisoire).unlink(missing_ok=True)   # le recensement est périmé
        print(f"\n{deplaces} fichier(s) {'copiés' if copier else 'déplacés'}"
              + (f", {erreurs} échec(s)" if erreurs else ""))
        for source in SOURCES:
            if source.is_dir() and not any(source.iterdir()):
                print(f"  {source.name}\\ est vide, tu peux le supprimer")
    print(f"({time.time() - t0:.0f} s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--etat", action="store_true",
                    help="recenser sans rien déplacer (sans danger pendant la lecture OCR)")
    ap.add_argument("--copier", action="store_true",
                    help="copier au lieu de déplacer (deux fois la place disque)")
    a = ap.parse_args()
    ranger(a.etat, a.copier)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu — les fichiers déjà rangés le restent, relance pour finir.")
    except Exception as e:                                   # noqa: BLE001
        print(f"ARRÊT : {type(e).__name__}: {e}")
