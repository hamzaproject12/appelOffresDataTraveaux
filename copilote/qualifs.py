"""Qualifications et classes, comparées par CODE.

L'annonce donne {"libelle": "B.1", "classe": "3"} ; l'historique donne « B.1- Travaux de
terrassements routiers courants » et « 3 ». On ne compare jamais les libellés : seulement le code
(« B.1 »), puis la classe.
"""
from __future__ import annotations

import re

_CODE = re.compile(r"^\W*([A-Za-z])\s*[.\-]?\s*(\d{1,2})?(?!\w)")
_CLASSE = re.compile(r"(\d+)")


def code_de(libelle: str | None) -> str | None:
    """« B.1- Travaux… » → « B.1 » ; « b1 » → « B.1 » ; « B » → « B » ; None si rien ne ressemble à un code."""
    m = _CODE.match(libelle or "")
    if not m:
        return None
    lettre, numero = m.group(1).upper(), m.group(2)
    return f"{lettre}.{int(numero)}" if numero else lettre


def classe_de(classe: str | int | None) -> int | None:
    """« 3 », « Classe 3 », 3 → 3 ; None si illisible."""
    m = _CLASSE.search(str(classe if classe is not None else ""))
    return int(m.group(1)) if m else None


def domaine_de(code: str | None) -> str | None:
    """« B.1 » → « B »."""
    return code[0] if code else None


def proximite_classe(cible: int | None, autre: int | None) -> float:
    """1 si la même classe, puis 0,6 / 0,3 pour un et deux crans d'écart ; 0,5 si l'une est inconnue."""
    if cible is None or autre is None:
        return 0.5
    return {0: 1.0, 1: 0.6, 2: 0.3}.get(abs(cible - autre), 0.0)


def paires(lignes) -> dict[str, int | None]:
    """[(libellé ou code, classe), …] → {code: classe}. Le code vu deux fois garde la plus forte classe."""
    out: dict[str, int | None] = {}
    for libelle, classe in lignes:
        code = code_de(libelle)
        if not code:
            continue
        c = classe_de(classe)
        if code not in out or (c is not None and (out[code] is None or c > out[code])):
            out[code] = c
    return out
