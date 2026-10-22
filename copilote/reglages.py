"""Lecture de copilote/reglages.json et des poids passés en paramètre."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

FICHIER = Path(__file__).with_name("reglages.json")
CRITERES = ("objet_nature", "acheteur", "qualification", "montant", "region", "recence")
FILTRES_QUALIFICATION = ("code", "domaine", "aucun")


class ReglageInvalide(ValueError):
    pass


def charger(chemin: Path | str | None = None) -> dict:
    with open(chemin or FICHIER, encoding="utf-8") as f:
        return json.load(f)


def poids_depuis_texte(texte: str | None, base: dict[str, float]) -> dict[str, float]:
    """« acheteur:50,objet_nature:30 » → les poids de `base`, ces deux critères remplacés.

    Un critère inconnu, une valeur illisible ou négative, ou des poids tous nuls lèvent ReglageInvalide :
    mieux vaut refuser que noter avec autre chose que ce qui a été demandé.
    """
    poids = dict(base)
    for morceau in (texte or "").split(","):
        if not morceau.strip():
            continue
        nom, sep, valeur = morceau.partition(":")
        nom = nom.strip()
        if not sep or nom not in CRITERES:
            raise ReglageInvalide(f"critère inconnu ou mal écrit : « {morceau.strip()} » (attendus : {', '.join(CRITERES)})")
        try:
            v = float(valeur.replace(",", "."))
        except ValueError:
            raise ReglageInvalide(f"poids illisible pour {nom} : « {valeur.strip()} »") from None
        if not math.isfinite(v) or v < 0:
            raise ReglageInvalide(f"poids négatif ou non fini pour {nom}")
        poids[nom] = v
    if sum(poids.values()) <= 0:
        raise ReglageInvalide("la somme des poids doit être positive")
    return poids


def avec_poids(reglages: dict, poids: dict[str, float]) -> dict:
    r = copy.deepcopy(reglages)
    r["poids"] = {c: float(poids[c]) for c in CRITERES}
    return r


# Jeux de poids comparés au backtest (étape 4). « defaut » est celui de reglages.json.
JEUX = {
    "defaut": None,
    "objet_dominant": {"objet_nature": 60, "acheteur": 15, "qualification": 10, "montant": 10, "region": 2, "recence": 3},
    "acheteur_dominant": {"objet_nature": 25, "acheteur": 50, "qualification": 10, "montant": 5, "region": 5, "recence": 5},
    "montant_recence": {"objet_nature": 35, "acheteur": 20, "qualification": 10, "montant": 20, "region": 5, "recence": 10},
    "objet_seul": {"objet_nature": 100, "acheteur": 0, "qualification": 0, "montant": 0, "region": 0, "recence": 0},
}
