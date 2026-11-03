"""Ce qui s'est réellement passé pour un AO rejoué, pour le bloc « Ce qui s'est réellement passé » de la
page d'analyse et pour les mesures (mesures_probables.py).

C'est le SEUL module, avec rejouer_ao.py qui l'écrit, à lire la vérité d'un rejeu. Le moteur (probables,
groupes_ao, analyse, similarite…) ne l'importe jamais : tests/test_rejeu.py et tests/test_probables.py le
vérifient. La comparaison entre ce qui était prévu et ce qui est arrivé se fait dans le navigateur, à partir
des deux réponses, ou dans mesures_probables.py.
"""
from __future__ import annotations

import sqlite3

from construire_base import ADMIS, offres_du_marche

from . import rejeu
from .rejouer_ao import lire_verite


def pour_lot(calc: sqlite3.Connection, ref_annonce: str, lot: str | None) -> dict | None:
    """La vérité d'un marché ou d'un lot (lot None pour un marché ordinaire) ; None si ce n'est pas un rejeu
    ou si elle est inconnue."""
    if not rejeu.est_un_rejeu(ref_annonce):
        return None
    voulu = "" if lot is None else str(lot)
    v = next((x for x in lire_verite(calc, ref_annonce) if str(x["lot"]) == voulu), None)
    if v is None:
        return None
    vus, concurrents = set(), []
    for k in v["concurrents"]:
        if not k["cle"] or k["cle"] in vus:
            continue
        vus.add(k["cle"])
        concurrents.append({"cle": k["cle"], "nom": k["nom"], "statut": k["statut"], "taux": k["taux"],
                            "rang": k["rang"]})
    return {
        "ref_annonce": ref_annonce, "lot": v["lot"] or None, "reference_reelle": v["reference_reelle"],
        "ref_reel": v["ref_reel"],
        "date_ouverture": v["date_ouverture"], "estimation": v["estimation"],
        "prix_reference": v["prix_reference"], "moyenne_offres": v["moyenne_offres"], "nb_offres": v["nb_offres"],
        "gagnant": {"cle": v["cle_gagnant"], "nom": v["nom_gagnant"], "taux": v["taux_gagnant"]},
        "nb_concurrents": len(concurrents), "concurrents": concurrents,
        "retenues": retenues(v["concurrents"], v["estimation"]),
    }


def retenues(concurrents: list[dict], estimation: float | None) -> list[float]:
    """Les taux des offres qui entrent dans la moyenne et le classement (règle de construire_base : les admis
    si le PV les distingue, sinon toutes). La page y insère notre offre au point recommandé."""
    if not estimation:
        return []
    cs = [{"statut": c.get("statut"), "montant_verifie": c.get("montant")} for c in concurrents]
    _, sur_admis = offres_du_marche(cs)
    return [c["montant_verifie"] / estimation for c in cs
            if c["montant_verifie"] and (c["statut"] in ADMIS or not sur_admis)]
