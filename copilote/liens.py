"""Liens de présence commune entre deux sociétés : observé, attendu par hasard, excès, niveau.

Pour chaque paire (A, B) qui se croise sur au moins `min_communs` marchés :
    observé  = marchés où A et B ont toutes deux déposé une offre (écartées comprises) ;
    attendu  = Σ sur les acheteurs de  marchés(A, acheteur) × marchés(B, acheteur) / marchés(acheteur),
               c'est-à-dire ce que donnerait le hasard si chacune gardait ses acheteurs et son activité ;
    excès    = observé / attendu ;
    p        = probabilité de Poisson d'observer au moins autant avec cet attendu.
Le niveau (fort, moyen, faible) dépend des seuils de reglages.json, section « groupes ».
Les parts disent dans quel sens va la présence : part_a = part des marchés de A où B est présente.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.stats import poisson

from . import reglages as _reglages
from .bases import cle_acheteur

NIVEAUX = ("fort", "moyen", "faible")              # du plus exigeant au moins exigeant
RANG = {"fort": 3, "moyen": 2, "faible": 1, None: 0}
NOM_RANG = {3: "fort", 2: "moyen", 1: "faible", 0: None}


def reglages_groupes(r: dict | None = None) -> dict:
    """La section « groupes » de reglages.json."""
    return (r if r is not None else _reglages.charger())["groupes"]


@dataclass
class Liens:
    """Toutes les paires examinées, en colonnes (i < j : indices dans Donnees.societes)."""
    i: np.ndarray
    j: np.ndarray
    communs: np.ndarray
    attendu: np.ndarray
    exces: np.ndarray
    p: np.ndarray
    part_i: np.ndarray
    part_j: np.ndarray
    rang: np.ndarray            # 3 fort, 2 moyen, 1 faible, 0 aucun
    mutuel: np.ndarray          # présence mutuelle habituelle (seconde voie, voir niveau_de)

    def __len__(self) -> int:
        return len(self.i)


def matrice_acheteurs(d) -> sparse.csr_matrix:
    """Marchés × acheteurs (clé cle_acheteur), 1 par marché."""
    cles = [cle_acheteur(m.acheteur) for m in d.marches]
    rang = {c: k for k, c in enumerate(sorted(set(cles)))}
    return sparse.csr_matrix((np.ones(len(cles)), (np.arange(len(cles)), [rang[c] for c in cles])),
                             shape=(len(cles), len(rang)))


def attendus(d, i: np.ndarray, j: np.ndarray, M: sparse.csr_matrix | None = None) -> np.ndarray:
    """Attendu par hasard de chaque paire (i[k], j[k])."""
    M = matrice_acheteurs(d) if M is None else M
    A = (d.X @ M).tocsr()                                       # sociétés × acheteurs : marchés
    taille = np.asarray(M.sum(axis=0)).ravel()                  # marchés par acheteur
    Aw = (A @ sparse.diags(1.0 / np.maximum(taille, 1))).tocsr()
    sortie = np.empty(len(i))
    pas = 50_000                                                # la mémoire reste bornée
    for k in range(0, len(i), pas):
        sortie[k:k + pas] = np.asarray(Aw[i[k:k + pas]].multiply(A[j[k:k + pas]]).sum(axis=1)).ravel()
    return sortie


def mutuel_de(communs, part_a, part_b, g: dict) -> np.ndarray:
    """Présence mutuelle habituelle : l'ancienne règle (assez de marchés communs, et chacune présente
    dans au moins `part` des marchés de l'autre). Deux sociétés qui se suivent ainsi chez un même
    acheteur ont un excès proche de 1 — l'acheteur explique leur rencontre — mais elles se suivent."""
    m = g["mutuel"]
    return (np.asarray(communs) >= m["communs"]) & (np.minimum(part_a, part_b) >= m["part"])


def niveau_de(communs, exces, p, seuils: dict, mutuel=None, niveau_mutuel: str = "moyen") -> np.ndarray:
    """Rang du niveau (3, 2, 1, 0) de chaque paire selon les seuils {niveau: {communs, exces, p}} ;
    une présence mutuelle habituelle vaut au moins `niveau_mutuel`."""
    communs, exces, p = np.asarray(communs), np.asarray(exces), np.asarray(p)
    rang = np.zeros(len(communs), dtype=np.int8)
    for nom in reversed(NIVEAUX):                               # le plus exigeant écrit en dernier
        s = seuils[nom]
        ok = (communs >= s["communs"]) & (exces >= s["exces"]) & (p < s["p"])
        rang[ok] = RANG[nom]
    if mutuel is not None:
        rang = np.maximum(rang, np.where(mutuel, RANG[niveau_mutuel], 0)).astype(np.int8)
    return rang


def calculer(d, reglages: dict | None = None, min_communs: int | None = None) -> Liens:
    """Les paires qui se croisent au moins `min_communs` fois (par défaut : le minimum du niveau faible)."""
    g = reglages_groupes(reglages)
    seuils = g["niveaux"]
    if min_communs is None:
        min_communs = min(s["communs"] for s in seuils.values())
    n = np.asarray(d.X.sum(axis=1)).ravel()
    C = sparse.triu(d.X @ d.X.T, k=1).tocoo()
    garde = C.data >= min_communs
    i, j, c = C.row[garde].astype(np.int64), C.col[garde].astype(np.int64), C.data[garde].astype(np.int64)
    ordre = np.lexsort((j, i))
    i, j, c = i[ordre], j[ordre], c[ordre]
    att = attendus(d, i, j)
    exces = c / np.maximum(att, 1e-9)
    p = poisson.sf(c - 1, att)
    pi, pj = c / n[i], c / n[j]
    mutuel = mutuel_de(c, pi, pj, g)
    return Liens(i, j, c, att, exces, p, pi, pj,
                 niveau_de(c, exces, p, seuils, mutuel, g["mutuel"]["niveau"]), mutuel)


def paire_isolee(d, a: int, b: int) -> dict:
    """Les mêmes mesures pour une seule paire, même sous le minimum de marchés communs (avis)."""
    xa, xb = d.X[a], d.X[b]
    c = int(xa.multiply(xb).sum())
    att = float(attendus(d, np.array([a]), np.array([b]))[0])
    na, nb = int(xa.sum()), int(xb.sum())
    return {"communs": c, "attendu": att, "exces": c / att if att > 0 else 0.0,
            "p": float(poisson.sf(c - 1, att)) if c else 1.0,
            "part_a": c / na if na else 0.0, "part_b": c / nb if nb else 0.0}
