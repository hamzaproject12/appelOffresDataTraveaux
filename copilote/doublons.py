"""Doublons de sociétés : deux orthographes d'une même entreprise ?

Vérification rapide, RAPPORT SEULEMENT — rien n'est fusionné. Une paire est signalée quand :
  1. les clés de société sont très proches (rapidfuzz, ratio ≥ SEUIL) ;
  2. les deux ne figurent JAMAIS sur un même marché : une entreprise ne se fait pas concurrence
     à elle-même, alors que deux homonymes réels s'y croisent tôt ou tard.
La clé est celle de construire_base.cle_nom (formes juridiques et espaces retirés).

    python -m copilote.doublons            écrit rapports/copilote_doublons.csv
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from collections import Counter, defaultdict

import numpy as np
from rapidfuzz import fuzz, process

from . import bases, config

SEUIL = 90                  # ratio rapidfuzz minimal entre deux clés, sur 100
LONGUEUR_MIN = 6            # une clé plus courte ressemble trop à n'importe quelle autre
PAQUET = 1000               # lignes de la matrice de comparaison calculées à la fois


def charger_societes(db: sqlite3.Connection) -> dict[str, dict]:
    """{cle: {"nom": nom le plus fréquent, "refs": {marchés où elle figure}}} — marchés principaux."""
    noms: dict[str, Counter] = defaultdict(Counter)
    refs: dict[str, set] = defaultdict(set)
    for r in db.execute("SELECT c.cle, c.nom, c.ref FROM concurrents c JOIN marches m ON m.ref = c.ref"
                        " WHERE m.principale = 1 AND c.cle <> ''"):
        noms[r["cle"]][r["nom"]] += 1
        refs[r["cle"]].add(r["ref"])
    return {cle: {"nom": noms[cle].most_common(1)[0][0], "refs": refs[cle]} for cle in refs}


def trouver(societes: dict[str, dict], seuil: int = SEUIL) -> list[dict]:
    """Les paires proches qui ne se rencontrent jamais, de la plus ressemblante à la moins."""
    cles = sorted(c for c in societes if len(c) >= LONGUEUR_MIN)
    paires = []
    for debut in range(0, len(cles), PAQUET):
        bloc = cles[debut:debut + PAQUET]
        scores = process.cdist(bloc, cles, scorer=fuzz.ratio, score_cutoff=seuil,
                               dtype=np.uint8, workers=-1)
        for i, j in zip(*np.nonzero(scores)):
            a, b = bloc[i], cles[j]
            if a < b and not (societes[a]["refs"] & societes[b]["refs"]):
                paires.append({
                    "score": int(scores[i, j]),
                    "cle_a": a, "nom_a": societes[a]["nom"], "marches_a": len(societes[a]["refs"]),
                    "cle_b": b, "nom_b": societes[b]["nom"], "marches_b": len(societes[b]["refs"]),
                })
    paires.sort(key=lambda p: (-p["score"], -(p["marches_a"] + p["marches_b"]), p["cle_a"]))
    return paires


def ecrire_csv(paires: list[dict], chemin) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    champs = ["score", "cle_a", "nom_a", "marches_a", "cle_b", "nom_b", "marches_b"]
    with open(chemin, "w", newline="", encoding="utf-8-sig") as f:       # utf-8-sig : Excel
        w = csv.DictWriter(f, fieldnames=champs, delimiter=";")
        w.writeheader()
        w.writerows(paires)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")        # la console Windows est en cp1252
    db = bases.historique()
    try:
        societes = charger_societes(db)
    finally:
        db.close()
    paires = trouver(societes)
    sortie = config.RAPPORTS / "copilote_doublons.csv"
    ecrire_csv(paires, sortie)
    print(f"{len(societes)} sociétés, {len(paires)} paires proches jamais présentes ensemble "
          f"(ratio ≥ {SEUIL}) -> {sortie}")
    for p in paires[:25]:
        print(f"{p['score']:3d}  {p['nom_a']} ({p['marches_a']})  ~  {p['nom_b']} ({p['marches_b']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
