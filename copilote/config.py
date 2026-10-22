"""Chemins et réglages du copilote.

Même convention que serveur.py : un fichier du volume /data sur le pod, à côté du script en local
(et sous Windows). Les variables d'environnement l'emportent toujours.
"""
from __future__ import annotations

import os
from pathlib import Path

ICI = Path(__file__).resolve().parent.parent          # le dossier du site
RAPPORTS = ICI / "rapports"


def _volume() -> Path:
    return Path("/data") if os.name != "nt" and Path("/data").is_dir() else ICI


def _chemin(variable: str, nom: str) -> Path:
    return Path(os.environ.get(variable) or _volume() / nom)


# Deux bases à part, jamais l'une dans l'autre :
#   calcul  : résultats recalculables, supprimable et reconstruite par `python -m copilote.recalculer`
#   retours : saisies humaines, JAMAIS supprimée ni reconstruite
COPILOTE_CALCUL = _chemin("COPILOTE_CALCUL", "copilote_calcul.db")
COPILOTE_RETOURS = _chemin("COPILOTE_RETOURS", "copilote_retours.db")


def chemin_historique() -> Path:
    """pv_travaux.db : celle que serveur.py sert (BASE), sinon celle posée à côté du script."""
    if os.environ.get("BASE"):
        return Path(os.environ["BASE"])
    for candidat in (ICI / "pv_travaux.db", ICI / "pv.db",
                     _volume() / "pv_travaux.db", _volume() / "pv.db"):
        if candidat.exists():
            return candidat
    return ICI / "pv_travaux.db"


def chemin_ao() -> Path:
    """ao_ouverts.db : même règle que serveur.AO_BASE."""
    return _chemin("AO_OUVERTS", "ao_ouverts.db")


DUREE_SESSION = 12 * 3600          # la même que le cookie visiteur
