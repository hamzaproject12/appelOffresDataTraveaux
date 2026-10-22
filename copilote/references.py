"""Bases simples : les références que toute méthode du copilote devra battre.

Règle de CLAUDE.md : « Une méthode n'est gardée que si elle bat une base simple au backtest. »
La plus simple qui soit : soumissionner partout au même taux fixe. On insère une offre fictive
à ce taux dans chaque unité et on compte combien de fois la commission la désigne.
"""
from __future__ import annotations

from .donnees import CLE_FICTIVE, TRANCHES, Unite, rejouer_attribution, tranche_de

# 0,80 à 1,05 par pas de 0,0025 : 101 taux. Calculés en entiers pour éviter la dérive flottante.
GRILLE = [round((3200 + 10 * k) / 4000, 4) for k in range(101)]


def unites_eligibles(unites: list[Unite]) -> list[Unite]:
    """Unités attribuées dont le prix de référence se calcule sans notre offre.

    On écarte les marchés infructueux ou sans attributaire connu : personne n'y a gagné, donc
    « gagner » n'y a pas de sens. Sans estimation, l'offre fictive n'a pas de montant.
    """
    return [u for u in unites
            if u.cle_gagnant and u.estimation and rejouer_attribution(u).reference is not None]


def taux_de_victoire(unites: list[Unite], grille: list[float] = GRILLE) -> dict:
    """{taux: {"n": …, "victoires": …, "taux_victoire": …}} sur les unités éligibles."""
    eligibles = unites_eligibles(unites)
    resultat = {}
    for r in grille:
        v = sum(rejouer_attribution(u, r).cle_gagnant == CLE_FICTIVE for u in eligibles)
        resultat[r] = {"n": len(eligibles), "victoires": v,
                       "taux_victoire": v / len(eligibles) if eligibles else None}
    return resultat


def par_tranche(unites: list[Unite], taux: list[float]) -> dict:
    """{tranche: {"n": …, taux: taux_de_victoire}} — la même mesure, par nombre d'offres reçues."""
    eligibles = unites_eligibles(unites)
    sortie = {}
    for nom, _, _ in TRANCHES:
        sous = [u for u in eligibles if tranche_de(rejouer_attribution(u).nb_offres) == nom]
        ligne = {"n": len(sous)}
        for r in taux:
            v = sum(rejouer_attribution(u, r).cle_gagnant == CLE_FICTIVE for u in sous)
            ligne[r] = v / len(sous) if sous else None
        sortie[nom] = ligne
    return sortie


def meilleur_par_tranche(unites: list[Unite], grille: list[float] = GRILLE) -> dict:
    """{tranche: {"n": …, "taux": meilleur taux fixe, "taux_victoire": …}} sur toute la grille.

    À égalité, le taux le plus bas l'emporte (même règle que `meilleur`). `taux` vaut None
    quand la tranche n'a aucune unité éligible.
    """
    sortie = {}
    for nom, ligne in par_tranche(unites, grille).items():
        candidats = [(r, ligne[r]) for r in grille if ligne[r] is not None]
        if candidats:
            r, v = max(candidats, key=lambda t: (t[1], -t[0]))
            sortie[nom] = {"n": ligne["n"], "taux": r, "taux_victoire": v}
        else:
            sortie[nom] = {"n": ligne["n"], "taux": None, "taux_victoire": None}
    return sortie


def meilleur(resultat: dict) -> tuple[float, dict] | None:
    candidats = [(r, x) for r, x in resultat.items() if x["taux_victoire"] is not None]
    return max(candidats, key=lambda t: (t[1]["taux_victoire"], -t[0])) if candidats else None
