"""Ce que le MOTEUR sait d'un AO rejoué (ref_annonce « REJEU-… ») : sa date de coupure, rien d'autre.

Un AO rejoué est un marché déjà attribué, renvoyé à l'API comme s'il venait de paraître. Pour que le
test ait un sens, le moteur ne doit voir que ce qu'il aurait su la veille de l'ouverture des plis :
tout ce qui est daté de ce jour-là ou après (le marché lui-même compris) lui est interdit, qu'il
s'agisse de l'historique, des groupes ou des profils.

Ce module est le seul passage du moteur : la date de coupure vient d'ici, et un AO « REJEU- » sans
date de coupure est une ERREUR, jamais « tout l'historique ». Les résultats réels sont rangés ailleurs
(rejouer_ao.py) ; aucun module du moteur n'a à les lire, et tests/test_rejeu.py le vérifie.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import groupes
from .donnees import Unite, charger_unites

PREFIXE = "REJEU-"

SCHEMA_AO = """
CREATE TABLE IF NOT EXISTS rejeu_ao (
  ref_annonce TEXT PRIMARY KEY,
  date_coupure TEXT NOT NULL,          -- AAAA-MM-JJ : dernier jour que le moteur a le droit de voir
  annonce TEXT NOT NULL,               -- l'annonce telle qu'elle est envoyée (sans date limite : posée à l'envoi)
  cree_le TEXT NOT NULL, envoye_le TEXT);
"""


class RejeuSansCoupure(LookupError):
    """Un AO « REJEU- » dont la date de coupure est inconnue : on s'arrête plutôt que de tout montrer."""


def est_un_rejeu(ref_annonce: str | None) -> bool:
    return str(ref_annonce or "").startswith(PREFIXE)


def veille(date_ouverture: str) -> str:
    """AAAAMMJJ → AAAA-MM-JJ du jour d'avant. La coupure est stricte : le jour d'ouverture est exclu."""
    return (datetime.strptime(date_ouverture, "%Y%m%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def jusqua_pour(ref_annonce: str | None, calc: sqlite3.Connection) -> str | None:
    """Le dernier jour visible (AAAA-MM-JJ, inclus) pour cet AO ; None pour un vrai AO (pas de coupure).

    `calc` est copilote_calcul.db. Compatible avec le paramètre `jusqua` de groupes.charger et de
    recalculer, qui gardent les marchés datés jusqu'à ce jour inclus.
    """
    if not est_un_rejeu(ref_annonce):
        return None
    try:
        ligne = calc.execute("SELECT date_coupure FROM rejeu_ao WHERE ref_annonce = ?", (ref_annonce,)).fetchone()
    except sqlite3.OperationalError:
        ligne = None
    if ligne is None:
        raise RejeuSansCoupure(f"{ref_annonce} : date de coupure inconnue (lancer python -m copilote.rejouer_ao).")
    return ligne[0]


def calcul_groupes_pour(ref_annonce: str | None, calc: sqlite3.Connection) -> str:
    """Identifiant du calcul de groupes à lire : « courant » pour un vrai AO, « jusqua-… » pour un rejeu."""
    jusqua = jusqua_pour(ref_annonce, calc)
    return groupes.identifiant(jusqua)


def unites_pour(db: sqlite3.Connection, ref_annonce: str | None, calc: sqlite3.Connection,
                fusions: dict[str, str] | None = None) -> list[Unite]:
    """Les unités (marchés ou lots) que le moteur peut lire pour cet AO.

    Vrai AO : tout l'historique. Rejeu : seulement les unités à date connue, non douteuse, antérieures
    à l'ouverture réelle — même règle que groupes.charger(jusqua=…).
    """
    unites = charger_unites(db, fusions=fusions)
    jusqua = jusqua_pour(ref_annonce, calc)
    if jusqua is None:
        return unites
    limite = jusqua.replace("-", "")
    return [u for u in unites if u.annee and u.date and u.date <= limite]
