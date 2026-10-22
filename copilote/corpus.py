"""Le corpus des marchés comparables : un tableau de colonnes (numpy) prêt à être noté.

Une ligne = une « unité » (un marché, ou un lot d'un marché alloti, voir donnees.py). On ne garde que
les unités qui peuvent servir de référence à un nouvel AO : date connue et non douteuse, estimation,
et un prix de référence qui se calcule (règle de construire_base.py, importée via donnees.py).

Les lignes sont rangées par date croissante : « les marchés antérieurs au jour d » est donc un
préfixe du tableau (`avant(jour)`), ce qui rend le backtest strict et rapide.

Textes : l'objet du marché ; pour un lot, l'intitulé du lot suivi de l'objet. Qualifications : celles
du marché (la base ne les range pas lot par lot).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from . import acheteurs as _ach
from . import nature, qualifs, regions
from .bases import cle_acheteur
from .donnees import Unite, charger_unites, rejouer_attribution

# Classes de qualification : 0 = le code est absent, 1 à 9 = la classe, 10 = code présent, classe inconnue.
CLASSE_INCONNUE = 10


def texte_index(texte: str | None) -> str:
    """Le texte tel qu'il entre dans l'index : à plat, sans les nombres seuls (pk, tranches, lots)."""
    return " ".join(m for m in nature.plat(texte).split() if not m.isdigit())


def jour(tri_date: str | None) -> int | None:
    """AAAAMMJJ → numéro de jour (ordinal). None si illisible."""
    try:
        return date(int(tri_date[:4]), int(tri_date[4:6]), int(tri_date[6:8])).toordinal()
    except (TypeError, ValueError):
        return None


@dataclass
class Fiche:
    """Ce qui décrit une unité en dehors de ses offres."""
    texte: str                                  # objet (+ intitulé du lot), tel quel
    quals: dict[str, int | None] = field(default_factory=dict)


def charger_fiches(db: sqlite3.Connection, unites: list[Unite]) -> dict[str, Fiche]:
    """Objet et qualifications de chaque unité, par identifiant."""
    objets = {r["ref"]: r["objet"] or "" for r in db.execute(
        "SELECT ref, objet FROM marches WHERE principale = 1")}
    intitules = {(r["ref"], str(r["lot"])): r["intitule"] or "" for r in db.execute(
        "SELECT l.ref, l.lot, l.intitule FROM lots l JOIN marches m ON m.ref = l.ref WHERE m.principale = 1")}
    brutes: dict[str, list] = {}
    for r in db.execute("SELECT q.ref, q.qualification, q.classe FROM qualifications q"
                        " JOIN marches m ON m.ref = q.ref WHERE m.principale = 1"):
        brutes.setdefault(r["ref"], []).append((r["qualification"], r["classe"]))
    quals = {ref: qualifs.paires(lignes) for ref, lignes in brutes.items()}
    fiches = {}
    for u in unites:
        objet = objets.get(u.ref, "")
        texte = f"{intitules.get((u.ref, u.lot), '')} {objet}".strip() if u.lot is not None else objet
        fiches[u.identifiant] = Fiche(texte, dict(quals.get(u.ref, {})))
    return fiches


@dataclass
class Corpus:
    unites: list[Unite]
    fiches: list[Fiche]
    texte: list[str]                            # texte d'index (à plat)
    famille: list[str | None]
    nature: list[str | None]
    jour: np.ndarray                            # ordinal du jour
    estimation: np.ndarray
    ref_taux: np.ndarray                        # prix de référence / estimation
    nb_offres: np.ndarray                       # offres retenues dans la moyenne
    nb_concurrents: np.ndarray                  # concurrents listés
    acheteur_cle: list[str]
    acheteur_type: list[str]
    region: list[str | None]
    codes: list[str]                            # tous les codes de qualification vus
    Q: np.ndarray                               # (unités × codes), voir CLASSE_INCONNUE
    exclus: dict[str, int] = field(default_factory=dict)
    index_de: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.unites)

    def avant(self, jour_limite: int) -> int:
        """Nombre de lignes datées STRICTEMENT avant ce jour : le préfixe que le backtest peut lire."""
        return int(np.searchsorted(self.jour, jour_limite, side="left"))

    def signature(self) -> str:
        """Empreinte des textes et de leur ordre : l'index enregistré ne vaut que pour ce corpus."""
        import hashlib
        h = hashlib.sha1()
        for u, t in zip(self.unites, self.texte):
            h.update(f"{u.identifiant}\t{t}\n".encode())
        return h.hexdigest()

    def gagnant(self, i: int) -> dict:
        """Le gagnant de l'unité i : l'attributaire réel, sinon celui que la règle désigne."""
        u = self.unites[i]
        cle, source = u.cle_gagnant, "attribution"
        if not cle:
            cle, source = rejouer_attribution(u).cle_gagnant, "regle"
        if not cle:
            return {"cle": None, "nom": None, "taux": None, "source": None}
        c = next((c for c in u.concurrents if c["cle"] == cle), None)
        return {"cle": cle, "nom": c["nom"] if c else None, "taux": c["taux"] if c else None,
                "source": source}


def construire(db: sqlite3.Connection, fusions: dict[str, str] | None = None,
               unites: list[Unite] | None = None) -> Corpus:
    unites = unites if unites is not None else charger_unites(db, fusions=fusions)
    fiches = charger_fiches(db, unites)
    exclus = {"sans_date": 0, "sans_estimation": 0, "sans_reference": 0}
    lignes = []
    for u in unites:
        j = jour(u.date) if u.annee else None
        if j is None:
            exclus["sans_date"] += 1
            continue
        if not u.estimation:
            exclus["sans_estimation"] += 1
            continue
        a = rejouer_attribution(u)
        if a.reference is None:
            exclus["sans_reference"] += 1
            continue
        lignes.append((j, u.identifiant, u, a))
    lignes.sort(key=lambda x: (x[0], x[1]))

    codes = sorted({c for _, _, u, _ in lignes for c in fiches[u.identifiant].quals})
    position = {c: k for k, c in enumerate(codes)}
    n = len(lignes)
    Q = np.zeros((n, len(codes)), dtype=np.int8)
    corpus = Corpus(
        unites=[u for _, _, u, _ in lignes], fiches=[fiches[u.identifiant] for _, _, u, _ in lignes],
        texte=[], famille=[], nature=[], jour=np.array([j for j, *_ in lignes], dtype=np.int32),
        estimation=np.array([u.estimation for _, _, u, _ in lignes], dtype=np.float64),
        ref_taux=np.array([a.reference / u.estimation for _, _, u, a in lignes], dtype=np.float64),
        nb_offres=np.array([a.nb_offres for *_, a in lignes], dtype=np.int32),
        nb_concurrents=np.array([len(u.concurrents) for _, _, u, _ in lignes], dtype=np.int32),
        acheteur_cle=[], acheteur_type=[], region=[], codes=codes, Q=Q, exclus=exclus)
    for i, (_, _, u, _) in enumerate(lignes):
        f = fiches[u.identifiant]
        corpus.texte.append(texte_index(f.texte))
        fam, nat = nature.classer(f.texte)
        corpus.famille.append(fam)
        corpus.nature.append(nat)
        corpus.acheteur_cle.append(cle_acheteur(u.acheteur))
        corpus.acheteur_type.append(_ach.type_acheteur(u.acheteur))
        corpus.region.append(regions.region_de(u.acheteur))
        for code, classe in f.quals.items():
            Q[i, position[code]] = classe if classe and 1 <= classe <= 9 else CLASSE_INCONNUE
        corpus.index_de[u.identifiant] = i
    return corpus
