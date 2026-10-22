"""Une petite base d'historique de poche, avec juste les colonnes que lit le copilote."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE marches (ref TEXT PRIMARY KEY, acheteur TEXT, tri_date TEXT, date_douteuse INTEGER,
  estimation REAL, cle_attributaire TEXT, prix_reference REAL, nb_lots INTEGER, principale INTEGER,
  infructueux INTEGER);
CREATE TABLE concurrents (id INTEGER PRIMARY KEY, ref TEXT, nom TEXT, cle TEXT, montant_acte REAL,
  montant_verifie REAL, statut TEXT, lots TEXT);
CREATE TABLE lots (ref TEXT, lot TEXT, estimation REAL, cle_attributaire TEXT, prix_reference REAL,
  PRIMARY KEY (ref, lot));
"""


def creer(chemin: Path) -> sqlite3.Connection:
    db = sqlite3.connect(chemin)
    db.executescript(SCHEMA)
    return db


def marche(db, ref, estimation, offres, gagnant=None, date="20260105", principale=1, douteuse=0,
           nb_lots=0, prix_reference=None, acheteur="COMMUNE D'ESSAI"):
    """offres : liste de (cle, montant) ou (cle, montant, statut) ou (cle, montant, statut, lots)."""
    db.execute("INSERT INTO marches VALUES (?,?,?,?,?,?,?,?,?,0)",
               (ref, acheteur, date, douteuse, estimation, gagnant, prix_reference, nb_lots, principale))
    for o in offres:
        cle, montant, *reste = o
        statut = reste[0] if reste else "admis"
        lots = reste[1] if len(reste) > 1 else ""
        db.execute("INSERT INTO concurrents (ref, nom, cle, montant_acte, montant_verifie, statut, lots)"
                   " VALUES (?,?,?,?,?,?,?)", (ref, cle.upper(), cle, montant, montant, statut, lots))


def lot(db, ref, numero, estimation, gagnant=None, prix_reference=None):
    db.execute("INSERT INTO lots VALUES (?,?,?,?,?)", (ref, numero, estimation, gagnant, prix_reference))
