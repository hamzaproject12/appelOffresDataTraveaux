"""Éligibilité d'une entreprise à un AO : qualifications et classes, comparées par CODE (étape D).

Ordre des classes (vérifié sur pv_travaux.db, estimation médiane des marchés par classe) :

    Équipement et Agriculture   S > 1 > 2 > 3 > 4 > 5     (plus le chiffre est petit, plus le marché est gros :
                                                           S 63 M DH, 1 26 M, 2 14 M, 3 6 M, 4 2 M, 5 1 M)
    Habitat                     6 > 5 > 4 > 3 > 2 > 1     (ordre INVERSE : la classe 6 est la plus haute)

Seuls les codes à lettre (B.1, O.2, X.1…) de l'Équipement sont comparés. Les codes à chiffre (« 4.1.Travaux de séguia »,
« 1.1 Terrassements généraux ») appartiennent à l'Agriculture ou à l'Habitat et ne se distinguent pas par le texte :
ils sont « non reconnus » et l'AO reste « à étudier ».

Règles (décision de Hamza, 04/11/2026) :
  * classe insuffisante, même d'un cran : « non éligible seul » ;
  * qualification absente : « non éligible » ;
  * plusieurs qualifications listées : elles sont TOUTES exigées ; si le texte montre une alternative (« ou »),
    l'AO est « à étudier » ;
  * un partenaire déclaré rend éligible seulement s'il est en GROUPEMENT et si l'un de ses membres a la
    qualification et la classe. Un sous-traitant ou un fournisseur ne change que le coût.

Cette logique existe en deux copies : ici et dans statique/decision.js (tests/cas_decision.json).
"""
from __future__ import annotations

import re

RANG_S = 0
CLASSES = ("S", "1", "2", "3", "4", "5")
_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z])\s*[.\-]\s*(\d{1,2})(?!\d)")
_ALTERNATIVE = re.compile(r"(?<![A-Za-zÀ-ÿ])ou(?![A-Za-zÀ-ÿ])", re.IGNORECASE)


def rang_classe(classe) -> int | None:
    """« S » → 0 ; « 3 », 3, « Classe 3 » → 3 ; None si illisible ou hors de l'échelle S, 1 à 5."""
    t = str(classe if classe is not None else "").strip().upper()
    if t in ("S", "CLASSE S"):
        return RANG_S
    m = re.search(r"(\d+)", t)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 5 else None


def suffit(nous, exige) -> bool:
    """Notre classe suffit-elle ? Un rang plus petit est une classe plus haute : S (0) couvre tout."""
    return nous is not None and exige is not None and nous <= exige


def codes_de(libelle: str | None) -> list[str]:
    """« B.1- Travaux de terrassements » → [« B.1 »] ; « B.1 ou B.3 » → [« B.1 », « B.3 »]. Les codes à chiffre
    (« 4.1.Travaux… ») ne sont pas reconnus : liste vide."""
    return [f"{m.group(1).upper()}.{int(m.group(2))}" for m in _CODE.finditer(libelle or "")]


def exigences_de(lignes) -> list[dict]:
    """Les lignes (libellé, classe) d'un AO → les exigences comparables.

    Chaque exigence : {code, classe (texte), rang, alternative, brut}. `code` vaut None pour une qualification
    qu'on ne sait pas lire (elle devient « à étudier »)."""
    sortie = []
    for libelle, classe in lignes:
        codes = codes_de(libelle)
        alternative = bool(_ALTERNATIVE.search(libelle or "")) and len(codes) > 1
        rang = rang_classe(classe)
        txt = None if classe is None else str(classe).strip()
        if not codes:
            sortie.append({"code": None, "classe": txt, "rang": rang, "alternative": False, "brut": libelle})
            continue
        for c in codes:
            sortie.append({"code": c, "classe": txt, "rang": rang, "alternative": alternative, "brut": libelle})
    return sortie


def verifier(exigences: list[dict], nos_quals: dict[str, str], partenaires: list[dict] | None = None) -> dict:
    """Notre éligibilité. `nos_quals` : {code: classe en texte}. `partenaires` : ceux du scénario (un seul pour un
    groupement) ; les autres types n'entrent pas ici.

    Retour : {statut: eligible | a_etudier | non_eligible, lignes: […], manque: n}. Chaque ligne : l'exigence, notre
    classe, `etat` (ok / classe / absent / inconnu / alternative), `couvert_par` (nom du partenaire) et
    `apporteraient` (les noms des partenaires en groupement qui couvriraient la ligne)."""
    partenaires = partenaires or []
    lignes, incertain, manque = [], False, 0
    for e in exigences:
        l = {**e, "nous": None, "etat": None, "couvert_par": None, "apporteraient": []}
        if e["code"] is None or e["rang"] is None:
            l["etat"] = "inconnu"
            incertain = True
        else:
            n = nos_quals.get(e["code"])
            nr = rang_classe(n) if n is not None else None
            l["nous"] = n
            if n is None:
                l["etat"] = "absent"
            elif nr is None or not suffit(nr, e["rang"]):
                l["etat"] = "classe"
            else:
                l["etat"] = "ok"
            if l["etat"] in ("absent", "classe"):
                for p in partenaires:
                    if _couvre(p, e):
                        l["couvert_par"] = p["nom"]
                        break
        if e.get("alternative"):
            incertain = True
            if l["etat"] in ("absent", "classe") and not l["couvert_par"]:
                l["etat"] = "alternative"           # une alternative non remplie n'est pas un refus : à étudier
        if l["etat"] in ("absent", "classe") and not l["couvert_par"]:
            manque += 1
        lignes.append(l)
    if manque:
        statut = "non_eligible"
    elif incertain:
        statut = "a_etudier"
    else:
        statut = "eligible"
    return {"statut": statut, "lignes": lignes, "manque": manque}


def _couvre(partenaire: dict, exigence: dict) -> bool:
    if partenaire.get("type") != "groupement":
        return False
    for q in partenaire.get("qualifs") or []:
        code = q.get("code")
        if code == exigence["code"] and suffit(rang_classe(q.get("classe")), exigence["rang"]):
            return True
    return False


def apports(exigences: list[dict], nos_quals: dict[str, str], partenaires: list[dict]) -> list[dict]:
    """Pour chaque exigence non remplie seul : les partenaires déclarés en groupement qui la couvriraient."""
    seul = verifier(exigences, nos_quals)
    sortie = []
    for l in seul["lignes"]:
        if l["etat"] in ("absent", "classe"):
            sortie.append({"code": l["code"], "classe": l["classe"],
                           "partenaires": [p["nom"] for p in partenaires if _couvre(p, l)]})
    return sortie
