"""Chargement des marchés et des offres en taux, et rejeu de l'attribution.

Une « unité » est ce que la commission juge d'un seul bloc : un marché ordinaire, ou UN LOT d'un
marché alloti (les offres d'un lot ne concourent qu'entre elles, avec l'estimation de ce lot).
Seuls les marchés `principale = 1` sont lus ; un marché alloti n'apparaît que par ses lots.

Tous les calculs se font en taux : r = montant / estimation.

La règle d'attribution n'est PAS recopiée ici : prix_de_reference, classement_commission,
offres_utilisables (via prix_de_reference), offres_du_marche et dans_la_fourchette sont importées
de construire_base.py. Si la règle y change, le copilote la suit.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from construire_base import (ADMIS, classement_commission, dans_la_fourchette, offre_de,
                             offres_du_marche, prix_de_reference)

from . import fusions as _fusions

CLE_FICTIVE = "__offre_fictive__"

# Tranches du nombre d'offres retenues dans la moyenne. Une unité à une seule offre est gagnée par
# n'importe qui : les chiffres globaux n'ont de sens qu'à côté de ceux des tranches.
TRANCHES = [("1-2", 1, 2), ("3-4", 3, 4), ("5-7", 5, 7), ("8+", 8, 10 ** 9)]


def tranche_de(nb_offres: int) -> str:
    return next(nom for nom, bas, haut in TRANCHES if bas <= max(nb_offres, 1) <= haut)


@dataclass
class Unite:
    ref: str
    lot: str | None                     # None pour un marché ordinaire
    acheteur: str | None
    date: str                           # tri_date AAAAMMJJ, '' si inconnue
    annee: str                          # '' quand la date est douteuse ou absente
    estimation: float | None
    cle_gagnant: str | None             # attributaire réel, None si non attribué / inconnu
    prix_reference_stocke: float | None
    # Au format qu'attend construire_base : nom, cle, statut, montant_verifie, montant_acte, lots.
    # « taux » s'y ajoute (None sans estimation ou sans montant).
    concurrents: list[dict] = field(default_factory=list)

    @property
    def identifiant(self) -> str:
        return self.ref if self.lot is None else f"{self.ref}#{self.lot}"


@dataclass
class Attribution:
    moyenne: float | None
    nb_offres: int
    reference: float | None
    rangs: dict[int, int]               # indice du concurrent -> rang (1 = désigné par la commission)
    indice_gagnant: int | None
    cle_gagnant: str | None
    nom_gagnant: str | None


def _concurrent(row: sqlite3.Row, estimation: float | None, fusions: dict[str, str]) -> dict:
    c = {"nom": row["nom"], "cle": _fusions.appliquer(row["cle"], fusions),
         "statut": row["statut"], "lots": row["lots"],
         "montant_verifie": row["montant_verifie"], "montant_acte": row["montant_acte"]}
    montant = offre_de(c)
    c["taux"] = montant / estimation if montant and estimation else None
    return c


def _annee(tri_date: str | None, douteuse) -> str:
    return "" if douteuse or not tri_date else tri_date[:4]


def charger_unites(db: sqlite3.Connection, annee: str | None = None,
                   fusions: dict[str, str] | None = None,
                   refs: list[str] | None = None) -> list[Unite]:
    """Toutes les unités jugeables, éventuellement d'une seule année.

    `db` est pv_travaux.db en lecture seule (bases.historique()). `annee` filtre sur l'année de
    la date du marché ; les dates douteuses (annee = '') ne sont retenues que si on ne filtre pas.
    `fusions` ({cle_fusionner: cle_garder}) vaut par défaut copilote/fusions_validees.csv ; {} n'en
    applique aucune. `refs` limite la lecture à ces marchés (la fiche d'un groupe en a quelques dizaines).
    """
    if fusions is None:
        fusions = _fusions.charger()
    # Le filtre s'écrit dans les trois requêtes : lire tout l'historique pour en garder 150 marchés
    # coûterait une seconde à chaque ouverture de fiche.
    filtre = f" AND m.ref IN ({','.join('?' * len(refs))})" if refs is not None else ""
    args = tuple(refs) if refs is not None else ()
    if refs is not None and not refs:
        return []
    par_ref: dict[str, list[sqlite3.Row]] = {}
    for row in db.execute(
            "SELECT c.ref, c.nom, c.cle, c.statut, c.lots, c.montant_verifie, c.montant_acte"
            " FROM concurrents c JOIN marches m ON m.ref = c.ref WHERE m.principale = 1"
            + filtre + " ORDER BY c.id", args):
        par_ref.setdefault(row["ref"], []).append(row)

    lots_de: dict[str, list[sqlite3.Row]] = {}
    for row in db.execute(
            "SELECT l.ref, l.lot, l.estimation, l.cle_attributaire, l.prix_reference"
            " FROM lots l JOIN marches m ON m.ref = l.ref WHERE m.principale = 1"
            + filtre + " ORDER BY l.ref, l.rowid", args):
        lots_de.setdefault(row["ref"], []).append(row)

    unites: list[Unite] = []
    for m in db.execute(
            "SELECT ref, acheteur, tri_date, date_douteuse, estimation, cle_attributaire,"
            " prix_reference, nb_lots FROM marches m WHERE principale = 1"
            + filtre + " ORDER BY ref", args):
        a = _annee(m["tri_date"], m["date_douteuse"])
        if annee is not None and a != annee:
            continue
        lignes = par_ref.get(m["ref"], [])
        base = dict(ref=m["ref"], acheteur=m["acheteur"], date=m["tri_date"] or "", annee=a)
        if m["nb_lots"]:
            # Alloti : chaque lot se lit seul, avec ses offres (c.lots = « 1 » ou « 1,2 ») et son
            # estimation. Les concurrents sans numéro de lot ne concourent nulle part.
            for lot in lots_de.get(m["ref"], []):
                numero = str(lot["lot"])
                du_lot = [r for r in lignes
                          if numero in [x.strip() for x in (r["lots"] or "").split(",") if x.strip()]]
                unites.append(Unite(
                    lot=numero, estimation=lot["estimation"] or None,
                    cle_gagnant=_fusions.appliquer(lot["cle_attributaire"] or None, fusions),
                    prix_reference_stocke=lot["prix_reference"],
                    concurrents=[_concurrent(r, lot["estimation"] or None, fusions) for r in du_lot],
                    **base))
        else:
            unites.append(Unite(
                lot=None, estimation=m["estimation"] or None,
                cle_gagnant=_fusions.appliquer(m["cle_attributaire"] or None, fusions),
                prix_reference_stocke=m["prix_reference"],
                concurrents=[_concurrent(r, m["estimation"] or None, fusions) for r in lignes],
                **base))
    return unites


def rejouer_attribution(unite: Unite, taux_ajoute: float | None = None) -> Attribution:
    """Rejoue l'ouverture des plis de l'unité, avec la règle de construire_base.py.

    `taux_ajoute` insère une offre fictive à ce taux (montant = taux × estimation), admise, comme
    le ferait notre propre soumission : elle entre dans la moyenne et dans le classement. Sans
    estimation, aucun prix de référence n'existe et l'offre fictive est ignorée.
    """
    concurrents = unite.concurrents
    if taux_ajoute is not None and unite.estimation:
        montant = taux_ajoute * unite.estimation
        concurrents = concurrents + [{
            "nom": "OFFRE FICTIVE", "cle": CLE_FICTIVE, "statut": "admis", "lots": "",
            "montant_verifie": montant, "montant_acte": montant,
            "taux": taux_ajoute}]

    offres, sur_admis = offres_du_marche(concurrents)
    offres = dans_la_fourchette(offres, unite.estimation)
    moyenne, nb_offres, reference, _, _ = prix_de_reference(unite.estimation, offres)
    candidats = [(i, offre_de(c)) for i, c in enumerate(concurrents)
                 if offre_de(c) and (c.get("statut") in ADMIS or not sur_admis)]
    rangs = classement_commission(candidats, reference)
    premier = next((i for i, r in rangs.items() if r == 1), None)
    gagnant = concurrents[premier] if premier is not None else None
    return Attribution(moyenne, nb_offres, reference, rangs, premier,
                       gagnant["cle"] if gagnant else None, gagnant["nom"] if gagnant else None)


def concordance(unites: list[Unite]) -> dict:
    """Part des unités où la règle rejouée désigne le vrai gagnant, globalement et par année.

    Comparables = unités attribuées dont le prix de référence se calcule. Les autres sont
    comptées par motif d'exclusion, pour ne pas laisser croire que « n » est l'ensemble.
    """
    def vide() -> dict:
        return {"unites": 0, "non_attribuees": 0, "sans_reference": 0, "comparables": 0,
                "concordants": 0, "reference_differente": 0}
    total, par_annee = vide(), {}
    par_tranche = {nom: {"comparables": 0, "concordants": 0} for nom, _, _ in TRANCHES}
    for u in unites:
        blocs = (total, par_annee.setdefault(u.annee or "date douteuse", vide()))
        for b in blocs:
            b["unites"] += 1
        if not u.cle_gagnant:
            for b in blocs:
                b["non_attribuees"] += 1
            continue
        a = rejouer_attribution(u)
        if a.reference is None:
            for b in blocs:
                b["sans_reference"] += 1
            continue
        # Garde-fou : le rejeu doit retrouver le prix de référence que la base a stocké.
        differente = (u.prix_reference_stocke is None
                      or abs(a.reference - u.prix_reference_stocke) > 0.01)
        t = par_tranche[tranche_de(a.nb_offres)]
        for b in (*blocs, t):
            b["comparables"] += 1
            b["concordants"] += int(a.cle_gagnant == u.cle_gagnant)
        for b in blocs:
            b["reference_differente"] += int(differente)
    for b in (total, *par_annee.values(), *par_tranche.values()):
        b["taux"] = b["concordants"] / b["comparables"] if b["comparables"] else None
    return {"global": total, "par_annee": dict(sorted(par_annee.items())), "par_tranche": par_tranche}
