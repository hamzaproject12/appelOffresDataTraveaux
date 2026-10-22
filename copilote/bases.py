"""Les quatre connexions du copilote.

    historique()  pv_travaux.db   lecture seule — remplacée à chaque déploiement, rien ne s'y écrit
    ao_ouverts()  ao_ouverts.db   lecture seule — c'est serveur.py qui l'alimente
    calcul()      copilote_calcul.db     lecture-écriture, recalculable
    retours()     copilote_retours.db    lecture-écriture, à conserver

Chaque appel ouvre une connexion neuve : à l'appelant de la fermer.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from construire_base import PREFIXE_ORG, cle_plate

from . import config

SCHEMA_META = "CREATE TABLE IF NOT EXISTS meta (cle TEXT PRIMARY KEY, valeur TEXT)"

# Les chemins des deux bases lues peuvent être fixés par serveur.py (routes.creer_routeur) ;
# à défaut, on retombe sur la même règle que lui (config.py), ce qui sert aux scripts en ligne
# de commande.
_sources: dict[str, Path] = {}


def definir_sources(historique: Path | str, ao: Path | str) -> None:
    _sources["historique"] = Path(historique)
    _sources["ao"] = Path(ao)


def chemin_historique() -> Path:
    return _sources.get("historique") or config.chemin_historique()


def chemin_ao() -> Path:
    return _sources.get("ao") or config.chemin_ao()


def cle_acheteur(nom: str | None) -> str:
    """Même clé que serveur.cle_acheteur : les deux traitements de construire_base, importés."""
    return cle_plate(PREFIXE_ORG.sub("", (nom or "")).strip())


def _lecture_seule(chemin: Path) -> sqlite3.Connection:
    chemin = Path(chemin)
    if not chemin.exists():
        # En mode=ro SQLite ne crée rien : sans ce test l'erreur serait un « unable to open ».
        raise FileNotFoundError(f"Base introuvable : {chemin}")
    db = sqlite3.connect(f"{chemin.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 4000")
    db.execute("PRAGMA query_only = ON")                 # deuxième verrou, au cas où mode=ro sauterait
    return db


def _lecture_ecriture(chemin: Path) -> sqlite3.Connection:
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(chemin, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 4000")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute(SCHEMA_META)
    db.commit()
    return db


def historique() -> sqlite3.Connection:
    db = _lecture_seule(chemin_historique())
    db.create_function("cle_acheteur", 1, cle_acheteur, deterministic=True)
    return db


def ao_ouverts() -> sqlite3.Connection:
    return _lecture_seule(chemin_ao())


def calcul() -> sqlite3.Connection:
    return _lecture_ecriture(config.COPILOTE_CALCUL)


def retours() -> sqlite3.Connection:
    return _lecture_ecriture(config.COPILOTE_RETOURS)
