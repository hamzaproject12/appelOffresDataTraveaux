"""Fusions de sociétés validées à la main : deux clés qui désignent la même entreprise.

Le fichier copilote/fusions_validees.csv (séparateur `;`, colonnes cle_garder;cle_fusionner;raison)
est la SEULE source de fusion : rien n'est fusionné automatiquement (voir doublons.py, qui ne fait
que proposer des paires). Au chargement, cle_fusionner est remplacée par cle_garder partout :
offres, attributaire, avis.

Une chaîne a → b → c est suivie jusqu'au bout ; un cycle est refusé.
"""
from __future__ import annotations

import csv
from pathlib import Path

FICHIER = Path(__file__).resolve().parent / "fusions_validees.csv"


def charger(chemin: Path | str | None = None) -> dict[str, str]:
    """{cle_fusionner: cle_garder}, chaînes résolues. Fichier absent : aucune fusion."""
    chemin = Path(chemin) if chemin else FICHIER
    if not chemin.exists():
        return {}
    brut: dict[str, str] = {}
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        for ligne in csv.DictReader(f, delimiter=";"):
            garder = (ligne.get("cle_garder") or "").strip()
            fusionner = (ligne.get("cle_fusionner") or "").strip()
            if garder and fusionner and garder != fusionner:
                brut[fusionner] = garder
    resolu: dict[str, str] = {}
    for depart in brut:
        cle, vues = depart, {depart}
        while cle in brut:
            cle = brut[cle]
            if cle in vues:
                raise ValueError(f"fusions_validees.csv : cycle autour de « {depart} »")
            vues.add(cle)
        resolu[depart] = cle
    return resolu


def appliquer(cle: str | None, fusions: dict[str, str]) -> str | None:
    return fusions.get(cle, cle) if cle else cle
