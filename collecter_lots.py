"""L'estimation et la caution de chaque lot, dans la fenêtre « Détail des lots » du portail.

Un marché alloti n'a pas d'estimation globale : le portail la donne lot par lot, et seulement
dans une fenêtre qui s'ouvre depuis la page de la consultation. La page principale affiche
« Estimation (en Dhs TTC) : » suivi de rien, et c'est pourquoi aucun marché alloti du site n'a
de prix de référence — 1 073 sur 1 073.

Tout le reste est déjà là : chaque offre porte son numéro de lot, et les gagnants par lot aussi.
Il ne manque que ces deux chiffres pour que le prix de référence et le classement se calculent
lot par lot, comme la commission les calcule.

    cd pv_travaux\\site
    python collecter_lots.py --essai 5      cinq marchés, pour voir ce que ça donne
    python collecter_lots.py                tous les marchés allotis
    python collecter_lots.py --reprendre    redemande aussi ceux qui avaient échoué

Relançable : ce qui est déjà lu n'est pas redemandé. Écrit dans « ..\\consultations\\lots\\ ».
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from html import unescape
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
import telecharger_pv as tp                                  # noqa: E402

DONNEES = Path(os.environ.get("DONNEES") or ICI.parent)
EXTRAITS = DONNEES / "extraits"
EXTRAITS_OCR = DONNEES / "extraits_ocr"
SORTIE = DONNEES / "consultations" / "lots"
PAUSE = 0.7

# Le portail écrit « orgAccronyme » avec deux c sur cette adresse-là, et « orgAcronyme » partout
# ailleurs. Une coquille de leur côté, mais il faut la respecter sinon la fenêtre revient vide.
POPUP = (tp.BASE + "?page=commun.PopUpDetailLots"
                   "&orgAccronyme={org}&refConsultation={ref}&lang=fr")

NOMBRE = re.compile(r"(\d+)")
# L'en-tête d'un lot : « <span>Lot</span> 1 : ». C'est le seul repère fiable pour découper la
# page — les commentaires HTML annoncent tous « Debut Lot 1 », y compris devant le lot 2.
ENTETE_LOT = re.compile(r"<span>\s*Lot\s*</span>\s*(\d+)\s*:", re.I)
MONTANT = re.compile(r"(\d[\d\s  .]*,\d{2}|\d[\d\s  ]*\d)")


def dire(*mots) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] " + " ".join(str(m) for m in mots), flush=True)


def propre(texte: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", texte or ""))).strip()


def nombre(texte: str):
    """« 1 332 120,00 » ou « 25 000,00 DH » -> 1332120.0. None si rien de chiffré."""
    m = MONTANT.search(texte or "")
    if not m:
        return None
    brut = re.sub(r"[\s  .]", "", m.group(1)).replace(",", ".")
    try:
        valeur = float(brut)
    except ValueError:
        return None
    return valeur if 0 < valeur < 1e11 else None


def par_identifiant(bloc: str, suffixe: str) -> str:
    """Le contenu de la balise dont l'identifiant finit par ce suffixe."""
    m = re.search(r'id="[^"]*' + re.escape(suffixe) + r'"[^>]*>(.*?)</span>', bloc, re.S | re.I)
    return m.group(1) if m else ""


def qualifications_du_bloc(bloc: str) -> list:
    """Les qualifications d'un lot : secteur / domaine / qualification / classe."""
    brut = par_identifiant(bloc, "_qualification")
    out = []
    for item in re.findall(r"<li>(.*?)</li>", brut, re.S | re.I):
        texte = propre(item)
        if not texte:
            continue
        morceaux = [p.strip() for p in texte.split("/")]
        classe = ""
        m = re.search(r"classe\s*([0-9A-Za-z]+)", texte, re.I)
        if m:
            classe = m.group(1)
        out.append({"secteur": morceaux[0] if morceaux else "",
                    "domaine": morceaux[1] if len(morceaux) > 1 else "",
                    "qualification": morceaux[2] if len(morceaux) > 2 else "",
                    "classe": classe, "brut": texte})
    return out


def lire_lots(html: str) -> list:
    """Les lots d'une fenêtre « Détail des lots ».

    Un bloc par lot, découpé sur l'en-tête « Lot N : ». On y lit l'intitulé, la catégorie,
    l'estimation, la caution et les qualifications — tout ce que le portail publie par lot.
    """
    entetes = list(ENTETE_LOT.finditer(html))
    lots = []
    for i, m in enumerate(entetes):
        debut = m.end()
        fin = entetes[i + 1].start() if i + 1 < len(entetes) else len(html)
        bloc = html[debut:fin]

        # L'intitulé est le premier « content-bloc » qui suit l'en-tête.
        intitule = ""
        t = re.search(r'class="content-bloc bloc-600"[^>]*>(.*?)</div>', bloc, re.S | re.I)
        if t:
            intitule = propre(t.group(1))

        # L'estimation : la valeur affichée, doublée d'un champ caché qui porte la même chose.
        # On prend l'affichage et on se rabat sur le champ caché s'il est vide.
        estimation = nombre(propre(par_identifiant(bloc, "_labelReferentielZoneText")))
        if estimation is None:
            c = re.search(r'_oldValue"[^>]*value="([^"]*)"', bloc, re.I)
            estimation = nombre(c.group(1)) if c else None

        lots.append({
            "lot": m.group(1),
            "intitule": intitule,
            "categorie": propre(re.search(r"<span>Cat[ée]gorie</span>\s*:\s*</div>\s*"
                                          r'<div class="content-bloc bloc-600">(.*?)</div>',
                                          bloc, re.S | re.I).group(1))
            if re.search(r"<span>Cat[ée]gorie</span>", bloc, re.I) else "",
            "estimation": estimation,
            "caution_provisoire": nombre(propre(par_identifiant(bloc, "_cautionProvisoire"))),
            "qualifications": qualifications_du_bloc(bloc),
            "agrements": propre(par_identifiant(bloc, "_agrements")).strip(" -") or None,
        })
    return lots


def marches_allotis(avec_ocr: bool) -> list:
    """Les marchés qui comptent au moins deux lots, d'après les extraits déjà lus.

    Par défaut on ne regarde que les PV saisis sur le portail — ceux de la branche « main ».
    Les PV lus par OCR sont un autre jeu de données, dans « extraits_ocr » : les mêler ici
    ferait collecter des lots pour des marchés qui ne sont pas dans la base qu'on reconstruit.
    """
    out = []
    if not EXTRAITS.is_dir():
        sys.exit(f"{EXTRAITS} est introuvable.")
    dossiers = [EXTRAITS] + ([EXTRAITS_OCR] if avec_ocr and EXTRAITS_OCR.is_dir() else [])
    dire("extraits examinés : " + ", ".join(d.name for d in dossiers)
         + ("" if avec_ocr else "   (sans l'OCR — ajoute --avec-ocr pour l'inclure)"))
    for f in sorted(x for d in dossiers for x in d.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        m = NOMBRE.search(str(d.get("allotissement") or ""))
        if not m or int(m.group(1)) < 2:
            continue
        ref = str(d.get("refConsultation_annonce") or "").strip()
        org = str(d.get("orgAcronyme") or "").strip()
        if ref and org:
            out.append({"ref": ref, "org": org, "reference": d.get("reference"),
                        "annonce": d.get("allotissement")})
    return out


def ecrire_json(chemin: Path, donnees) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    provisoire = chemin.with_suffix(chemin.suffix + ".tmp")
    provisoire.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(provisoire, chemin)


def collecter(limite: int, fils: int, reprendre: bool, avec_ocr: bool) -> None:
    SORTIE.mkdir(parents=True, exist_ok=True)
    for reste in SORTIE.glob("*.tmp"):
        reste.unlink(missing_ok=True)

    tous = marches_allotis(avec_ocr)
    dire(f"{len(tous)} marché(s) allotis repérés")

    def a_relire(m) -> bool:
        fiche = SORTIE / f"{m['ref']}.json"
        if not fiche.exists():
            return True
        if not reprendre:
            return False
        try:
            return not (json.loads(fiche.read_text(encoding="utf-8")) or {}).get("lots")
        except (OSError, ValueError):
            return True

    restants = [m for m in tous if a_relire(m)]
    dire(f"{len(tous) - len(restants)} déjà lus, {len(restants)} restent à demander")
    a_faire = restants[:limite] if limite else restants
    if limite and len(a_faire) < len(restants):
        dire(f"(essai limité à {len(a_faire)} ; enlève --essai pour faire les {len(restants)})")
    if not a_faire:
        return

    propre_fil = threading.local()

    def client():
        if not hasattr(propre_fil, "c"):
            propre_fil.c = tp.Client()
        return propre_fil.c

    def traiter(m):
        try:
            html = client().html(POPUP.format(ref=m["ref"], org=m["org"]))
        except Exception as e:                               # noqa: BLE001
            time.sleep(PAUSE)
            return m, None, f"{type(e).__name__}: {e}"
        time.sleep(PAUSE)
        return m, html, None

    lus = vides = echecs = 0
    avec_estimation = sans_estimation = 0
    exemples = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, fils)) as ex:
        for i, (m, html, erreur) in enumerate(ex.map(traiter, a_faire), 1):
            if erreur:
                echecs += 1
                ecrire_json(SORTIE / f"{m['ref']}.json", {**m, "lots": [], "probleme": erreur})
                continue
            lots = lire_lots(html)
            ecrire_json(SORTIE / f"{m['ref']}.json", {**m, "lots": lots})
            if not lots:
                vides += 1
            else:
                lus += 1
                for l in lots:
                    if l["estimation"] is not None:
                        avec_estimation += 1
                    else:
                        sans_estimation += 1
                if len(exemples) < 3:
                    exemples.append((m.get("reference"), lots))
            if i % 50 == 0:
                reste = (time.time() - t0) / i * (len(a_faire) - i) / 60
                dire(f"  {i}/{len(a_faire)} — {lus} marchés lus, {avec_estimation} lots chiffrés"
                     f" (reste ~{reste:.0f} min)")

    dire("")
    dire(f"{lus} marché(s) lus, {vides} sans lot reconnu, {echecs} échec(s)")
    dire(f"{avec_estimation} lot(s) avec une estimation, {sans_estimation} sans")
    if avec_estimation + sans_estimation:
        dire(f"soit {100 * avec_estimation / (avec_estimation + sans_estimation):.0f} %"
             f" des lots qui pourront avoir un prix de référence")
    for reference, lots in exemples:
        dire("")
        dire(f"  {reference} — {len(lots)} lots :")
        for l in lots:
            dire(f"    lot {l['lot']:<3} estimation {l['estimation']}"
                 f"  caution {l['caution_provisoire']}  {l['intitule'][:52]}")
    dire("")
    dire(f"-> {SORTIE}")
    dire("Reconstruis ensuite la base :  python construire_base.py --base travaux")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--essai", type=int, default=0, metavar="N",
                    help="n'en demander que N, pour voir le résultat avant de tout lancer")
    ap.add_argument("--limite", type=int, default=0, metavar="N")
    ap.add_argument("--fils", type=int, default=3, metavar="N", help="demandes en parallèle")
    ap.add_argument("--avec-ocr", action="store_true",
                    help="inclure aussi les marchés lus par OCR (dossier extraits_ocr)")
    ap.add_argument("--reprendre", action="store_true",
                    help="redemander aussi les marchés dont la lecture n'avait rien donné")
    a = ap.parse_args()
    collecter(a.essai or a.limite, a.fils, a.reprendre, a.avec_ocr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu. Relance la même commande : rien de ce qui est lu n'est redemandé.")
    except Exception as e:                                   # noqa: BLE001
        print(f"\nARRÊT : {type(e).__name__}: {e}")
        sys.exit(1)
