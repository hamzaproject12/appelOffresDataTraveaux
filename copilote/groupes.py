"""Groupes de présence commune : des sociétés qui soumissionnent souvent ensemble.

Ce sont des faits statistiques de présence, pas des accusations. Un groupe peut réunir des
partenaires habituels, des sociétés d'un même dirigeant, des membres d'un groupement, ou des
concurrents qui visent les mêmes acheteurs. Les avis humains (table avis_groupes de
copilote_retours.db) tranchent.

Étapes :
  1. matrice creuse sociétés × marchés (une colonne par MARCHÉ, pas par lot : un marché en 8 lots
     ne compte qu'une présence commune), puis X·Xᵀ = marchés communs de toutes les paires ;
  2. présence mutuelle : paire croisée ≥ MIN_COMMUNS fois, retenue si la part des marchés de A où
     B est présent ET la part des marchés de B où A est présent dépassent le seuil ;
     avis « liées : oui » ⇒ retenue d'office, « non » ⇒ exclue ;
  3. graphe des paires retenues ; un GROUPE est une clique maximale d'au moins 3 sociétés
     (toutes les paires du groupe sont mutuellement présentes) présente à au moins 3 membres sur
     ≥ MIN_COMMUNS marchés. Les cliques qui se chevauchent forment une FAMILLE (Louvain, puis
     découpée pour que toutes ses cliques aient un membre en commun) : c'est l'unité affichée.
     Membres permanents = présents dans toutes les cliques de la famille ; occasionnels = dans
     certaines seulement. L'identifiant d'une famille est l'empreinte de ses permanents ;
  4. confirmation : contextes (acheteurs, domaines, régions), présence hors profil, qui gagne,
     position des offres, comparaison au hasard ;
  5. score de 0 à 100, chef de file probable, marchés de preuve.

`jusqua` (AAAA-MM-JJ) ne garde que les marchés datés jusqu'à ce jour inclus (dates douteuses
exclues), y compris pour les profils, le hasard et les avis : aucune fuite pour le backtest.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import networkx as nx
import numpy as np
from scipy import sparse

from . import fusions as _fusions
from .donnees import Unite, charger_unites
from .regions import region_de

SEUIL = 0.50                # part minimale de présence mutuelle, des deux côtés
SEUIL_FAIBLE = 0.30         # entre ce seuil et SEUIL : lien faible, rangé mais non affiché (étape 5)
MIN_COMMUNS = 5             # marchés communs minimum pour examiner une paire ou garder un groupe
MIN_PRESENTS = 3            # membres présents sur un marché pour qu'il serve de preuve
TIRAGES = 200               # tirages de la comparaison au hasard
GRAINE = 0
PART_HABITUELLE = 0.10      # un domaine est habituel s'il fait ≥ 10 % des marchés hors groupe
MIN_PROFIL = 5              # marchés hors groupe nécessaires pour juger d'un profil
CHEF_DE_FILE = 0.60         # part des victoires du groupe au-delà de laquelle un membre mène
MIN_VICTOIRES = 3           # en dessous, on ne dit rien de la répartition des victoires

# Poids du score (total 100). Voir score() pour chaque composante.
POIDS = {"force": 20, "volume": 15, "hasard": 30, "contextes": 15, "hors_profil": 5, "gains": 15}

SCHEMA_AVIS = """
CREATE TABLE IF NOT EXISTS avis_groupes (
  id INTEGER PRIMARY KEY,
  cle_a TEXT NOT NULL, cle_b TEXT NOT NULL,          -- cle_a < cle_b
  reponse TEXT NOT NULL CHECK (reponse IN ('oui', 'non', 'nsp')),
  commentaire TEXT, auteur TEXT NOT NULL, cree_le TEXT NOT NULL,
  groupe TEXT);                                      -- signature du groupe affiché, pour mémoire
CREATE INDEX IF NOT EXISTS i_avis_paire ON avis_groupes(cle_a, cle_b);
"""

SCHEMA_CALCUL = """
CREATE TABLE IF NOT EXISTS groupes_calculs (
  calcul TEXT PRIMARY KEY, jusqua TEXT, seuil REAL, cree_le TEXT, marches INTEGER,
  societes INTEGER, paires_examinees INTEGER, paires_retenues INTEGER, groupes INTEGER,
  fusions INTEGER, avis INTEGER, details TEXT);
CREATE TABLE IF NOT EXISTS groupes_paires (
  calcul TEXT, cle_a TEXT, cle_b TEXT, communs INTEGER, part_a REAL, part_b REAL,
  retenue INTEGER, origine TEXT, PRIMARY KEY (calcul, cle_a, cle_b));
CREATE TABLE IF NOT EXISTS groupes (
  calcul TEXT, id INTEGER, signature TEXT, taille INTEGER, famille INTEGER, score INTEGER,
  chef_de_file TEXT, structure TEXT, marches_complet INTEGER, marches_3 INTEGER,
  acheteurs INTEGER, regions INTEGER, domaines TEXT, part_hors_profil REAL, part_gains REAL,
  attendu_hasard REAL, rapport_hasard REAL, p_hasard REAL, premier TEXT, dernier TEXT,
  details TEXT, PRIMARY KEY (calcul, id));
CREATE TABLE IF NOT EXISTS groupe_membres (
  calcul TEXT, groupe INTEGER, cle TEXT, nom TEXT, marches INTEGER, presences_groupe INTEGER,
  gains_groupe INTEGER, taux_avec REAL, n_avec INTEGER, taux_sans REAL, n_sans INTEGER,
  hors_profil INTEGER, PRIMARY KEY (calcul, groupe, cle));
CREATE TABLE IF NOT EXISTS groupe_preuves (
  calcul TEXT, groupe INTEGER, ref TEXT, date TEXT, acheteur TEXT, presents INTEGER,
  complet INTEGER, gagnants TEXT, PRIMARY KEY (calcul, groupe, ref));
CREATE INDEX IF NOT EXISTS i_gm_cle ON groupe_membres(calcul, cle);
CREATE TABLE IF NOT EXISTS familles (
  calcul TEXT, id TEXT, numero INTEGER, score INTEGER, taille INTEGER, cliques INTEGER,
  meilleure_clique INTEGER, permanents TEXT, occasionnels TEXT, PRIMARY KEY (calcul, id));
CREATE TABLE IF NOT EXISTS famille_cliques (
  calcul TEXT, famille TEXT, groupe INTEGER, PRIMARY KEY (calcul, groupe));
CREATE TABLE IF NOT EXISTS familles_anciennes (   -- jamais vidée par un recalcul : sert aux liens déjà envoyés
  id TEXT PRIMARY KEY, permanents TEXT, occasionnels TEXT, vue_le TEXT);
CREATE TABLE IF NOT EXISTS liens_faibles (
  calcul TEXT, cle_a TEXT, cle_b TEXT, communs INTEGER, part_a REAL, part_b REAL,
  PRIMARY KEY (calcul, cle_a, cle_b));
"""


# --------------------------------------------------------------------------- données

@dataclass
class Marche:
    ref: str
    acheteur: str
    date: str                       # AAAAMMJJ, '' si inconnue
    region: str | None
    domaines: frozenset[str]        # lettres de qualification (B, A, J…)
    unites: list[Unite] = field(default_factory=list)


@dataclass
class Donnees:
    marches: list[Marche]
    societes: list[str]             # clé de chaque ligne de X
    noms: dict[str, str]
    X: sparse.csr_matrix            # sociétés × marchés, 1 = au moins une offre déposée
    jusqua: str | None
    nb_fusions: int

    @property
    def index(self) -> dict[str, int]:
        return {c: i for i, c in enumerate(self.societes)}


_LETTRE_QUALIF = re.compile(r"^\s*([A-Z])\s*\.\s*\d")
_LETTRE_DOMAINE = re.compile(r"^\s*([A-Z])\s*-")


def lettre_de(qualification: str | None, domaine: str | None = None) -> str | None:
    """« B.1- Travaux de terrassements… » → « B » ; sinon la lettre du domaine « B- Travaux… »."""
    for texte, motif in ((qualification, _LETTRE_QUALIF), (domaine, _LETTRE_DOMAINE)):
        m = motif.match(texte or "")
        if m:
            return m.group(1)
    return None


def _limite(jusqua: str | None) -> str | None:
    if not jusqua:
        return None
    return datetime.strptime(jusqua, "%Y-%m-%d").strftime("%Y%m%d")


def charger(db: sqlite3.Connection, jusqua: str | None = None,
            fusions: dict[str, str] | None = None) -> Donnees:
    """Marchés et présences, depuis pv_travaux.db (lecture seule), fusions validées appliquées."""
    if fusions is None:
        fusions = _fusions.charger()
    limite = _limite(jusqua)
    unites = charger_unites(db, fusions=fusions)
    if limite:
        # Backtest : une date douteuse ou absente pourrait être postérieure, on l'écarte.
        unites = [u for u in unites if u.annee and u.date and u.date <= limite]

    lettres: dict[str, set[str]] = defaultdict(set)
    try:
        for ref, qualif, domaine in db.execute("SELECT ref, qualification, domaine FROM qualifications"):
            lettre = lettre_de(qualif, domaine)
            if lettre:
                lettres[ref].add(lettre)
    except sqlite3.OperationalError:            # base de test sans table qualifications
        pass

    par_ref: dict[str, Marche] = {}
    noms: dict[str, Counter] = defaultdict(Counter)
    presences: dict[str, set[str]] = {}
    for u in unites:
        m = par_ref.get(u.ref)
        if m is None:
            m = par_ref[u.ref] = Marche(u.ref, u.acheteur or "", u.date, region_de(u.acheteur),
                                        frozenset(lettres.get(u.ref, ())))
            presences[u.ref] = set()
        m.unites.append(u)
        for c in u.concurrents:
            if c["cle"]:
                presences[u.ref].add(c["cle"])
                noms[c["cle"]][c["nom"]] += 1

    marches = [m for m in par_ref.values() if presences[m.ref]]
    societes = sorted(noms)
    idx = {c: i for i, c in enumerate(societes)}
    lignes, colonnes = [], []
    for j, m in enumerate(marches):
        for c in presences[m.ref]:
            lignes.append(idx[c])
            colonnes.append(j)
    X = sparse.csr_matrix((np.ones(len(lignes), dtype=np.int32), (lignes, colonnes)),
                          shape=(len(societes), len(marches)))
    return Donnees(marches, societes, {c: n.most_common(1)[0][0] for c, n in noms.items()},
                   X, jusqua, len(fusions))


# --------------------------------------------------------------------------- avis

def preparer_retours(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA_AVIS)
    db.commit()


def paire(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def lire_avis(db: sqlite3.Connection, jusqua: str | None = None,
              fusions: dict[str, str] | None = None) -> dict[tuple[str, str], str]:
    """{(cle_a, cle_b): 'oui' | 'non'} — pour chaque paire, le dernier avis tranché l'emporte.

    « Je ne sais pas » ne tranche rien. Avec `jusqua`, seuls les avis donnés ce jour-là ou avant
    comptent (backtest).
    """
    if fusions is None:
        fusions = _fusions.charger()
    preparer_retours(db)
    sql = "SELECT cle_a, cle_b, reponse FROM avis_groupes WHERE reponse IN ('oui', 'non')"
    args: tuple = ()
    if jusqua:
        sql += " AND substr(cree_le, 1, 10) <= ?"
        args = (jusqua,)
    avis: dict[tuple[str, str], str] = {}
    for a, b, reponse in db.execute(sql + " ORDER BY cree_le, id", args):
        a, b = _fusions.appliquer(a, fusions), _fusions.appliquer(b, fusions)
        if a != b:
            avis[paire(a, b)] = reponse
    return avis


# --------------------------------------------------------------------------- paires

@dataclass
class Paire:
    a: str
    b: str
    communs: int
    part_a: float           # part des marchés de A où B est présent
    part_b: float
    retenue: bool
    origine: str            # seuil | sous le seuil | avis oui | avis non


def paires(d: Donnees, seuil: float = SEUIL, avis: dict | None = None) -> list[Paire]:
    """Toutes les paires croisées ≥ MIN_COMMUNS fois, plus celles qui ont un avis tranché."""
    avis = avis or {}
    n = np.asarray(d.X.sum(axis=1)).ravel()
    C = sparse.triu(d.X @ d.X.T, k=1).tocoo()
    garde = C.data >= MIN_COMMUNS
    resultat: dict[tuple[str, str], Paire] = {}
    for i, j, c in zip(C.row[garde], C.col[garde], C.data[garde]):
        a, b = d.societes[i], d.societes[j]
        pa, pb = c / n[i], c / n[j]
        ok = bool(pa >= seuil and pb >= seuil)
        resultat[(a, b)] = Paire(a, b, int(c), float(pa), float(pb), ok,
                                 "seuil" if ok else "sous le seuil")
    idx = d.index
    for (a, b), reponse in avis.items():
        p = resultat.get((a, b))
        if p is None:
            if a not in idx or b not in idx:
                continue                         # société absente de la fenêtre
            i, j = idx[a], idx[b]
            c = int(d.X[i].multiply(d.X[j]).sum())
            p = resultat[(a, b)] = Paire(a, b, c, c / n[i] if n[i] else 0.0,
                                         c / n[j] if n[j] else 0.0, False, "")
        p.retenue = reponse == "oui"
        p.origine = f"avis {reponse}"
    return sorted(resultat.values(), key=lambda p: (p.a, p.b))


# --------------------------------------------------------------------------- groupes

@dataclass
class Groupe:
    membres: list[str]
    famille: int | None = None
    mesures: dict = field(default_factory=dict)
    score: int = 0
    composantes: dict = field(default_factory=dict)

    @property
    def signature(self) -> str:
        return "|".join(self.membres)


def cliques(liste: list[Paire]) -> tuple[nx.Graph, list[list[str]], dict[str, int]]:
    G = nx.Graph()
    for p in liste:
        if p.retenue:
            G.add_edge(p.a, p.b, weight=min(p.part_a, p.part_b))
    trouvees = sorted(sorted(c) for c in nx.find_cliques(G) if len(c) >= MIN_PRESENTS)
    famille_de: dict[str, int] = {}
    if G.number_of_edges():
        familles = nx.community.louvain_communities(G, weight="weight", seed=GRAINE)
        for k, f in enumerate(sorted(familles, key=lambda f: (-len(f), sorted(f)))):
            for cle in f:
                famille_de[cle] = k + 1
    return G, trouvees, famille_de


@dataclass
class Famille:
    cliques: list[Groupe]
    permanents: list[str]               # clés présentes dans toutes les cliques
    occasionnels: list[str]             # clés présentes dans certaines cliques seulement
    cle: str = ""                       # empreinte des permanents
    numero: int = 0                     # rang par score, pour l'affichage seulement

    @property
    def membres(self) -> list[str]:
        return sorted(self.permanents + self.occasionnels)

    @property
    def meilleure(self) -> Groupe:
        ordre = sorted(self.cliques, key=lambda g: g.signature)           # à égalité : première signature
        return max(ordre, key=lambda g: (g.score, g.mesures["marches_3"]))

    @property
    def score(self) -> int:
        return self.meilleure.score


def empreinte(permanents: list[str], longueur: int = 8) -> str:
    """Empreinte courte de la liste TRIÉE des membres permanents : stable tant qu'ils ne changent pas."""
    return hashlib.sha256(chr(10).join(sorted(permanents)).encode("utf-8")).hexdigest()[:longueur]


def _decouper(cliques: list[Groupe]) -> list[list[Groupe]]:
    """Découpe un ensemble de cliques pour que chaque morceau ait au moins un membre commun à toutes.

    Louvain peut ranger côte à côte des cliques sans aucun membre en commun : il n'y aurait alors
    ni « permanent » ni empreinte. Les cliques, du meilleur score au moins bon, rejoignent le morceau
    avec lequel elles partagent le plus de membres permanents ; sinon elles en ouvrent un nouveau.
    """
    morceaux: list[tuple[set[str], list[Groupe]]] = []
    for g in sorted(cliques, key=lambda g: (-g.score, g.signature)):
        membres = set(g.membres)
        meilleur_i, meilleur_n = None, 0
        for i, (noyau, _) in enumerate(morceaux):
            n = len(noyau & membres)
            if n > meilleur_n:
                meilleur_i, meilleur_n = i, n
        if meilleur_i is None:
            morceaux.append((membres, [g]))
        else:
            noyau, liste = morceaux[meilleur_i]
            morceaux[meilleur_i] = (noyau & membres, liste + [g])
    return [liste for _, liste in morceaux]


def ranger_familles(groupes: list[Groupe]) -> list[Famille]:
    """Rassemble les cliques en familles ; numérote par score décroissant et pose les empreintes."""
    par_louvain: dict[int | None, list[Groupe]] = defaultdict(list)
    for g in groupes:
        par_louvain[g.famille].append(g)
    familles = []
    for cliques in par_louvain.values():
        for morceau in _decouper(cliques):
            tous = [set(g.membres) for g in morceau]
            permanents = set.intersection(*tous)
            familles.append(Famille(morceau, sorted(permanents), sorted(set.union(*tous) - permanents)))
    familles.sort(key=lambda f: (-f.score, -f.meilleure.mesures["marches_3"], f.permanents))
    pris: set[str] = set()
    for n, f in enumerate(familles, 1):
        f.numero = n
        longueur = 8
        while (cle := empreinte(f.permanents, longueur)) in pris:        # même noyau dans deux familles
            longueur += 2
        pris.add(cle)
        f.cle = cle
        for g in f.cliques:
            g.famille = n
    return familles


def liens_faibles(liste: list[Paire], seuil: float, bas: float = SEUIL_FAIBLE) -> list[Paire]:
    """Paires sous le seuil de présence mutuelle mais au moins à `bas` des deux côtés, sans avis tranché."""
    return [p for p in liste
            if not p.retenue and p.origine == "sous le seuil" and min(p.part_a, p.part_b) >= bas]


class Profils:
    """Ce qu'on sait de chaque société dans la fenêtre : ses marchés, ses domaines, ses unités."""

    def __init__(self, d: Donnees):
        self.d = d
        self.marches_de = [d.X.indices[d.X.indptr[i]:d.X.indptr[i + 1]] for i in range(len(d.societes))]
        self.unites_de: dict[str, list[tuple[int, Unite]]] = defaultdict(list)
        for j, m in enumerate(d.marches):
            for u in m.unites:
                for cle in {c["cle"] for c in u.concurrents if c["cle"]}:
                    self.unites_de[cle].append((j, u))
        self.Xc = d.X.tocsc()

    def habituels(self, i: int, exclus: set[int]) -> set[str] | None:
        """Domaines habituels de la société i, hors des marchés `exclus` ; None si trop peu."""
        compte, total = Counter(), 0
        for j in self.marches_de[i]:
            if j in exclus or not self.d.marches[j].domaines:
                continue
            total += 1
            compte.update(self.d.marches[j].domaines)
        if total < MIN_PROFIL:
            return None
        return {l for l, n in compte.items() if n / total >= PART_HABITUELLE}


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _taux_de(u: Unite, cle: str) -> float | None:
    return next((c["taux"] for c in u.concurrents if c["cle"] == cle and c["taux"]), None)


def mesurer(g: Groupe, pr: Profils, toutes: dict[tuple[str, str], Paire],
            rng: np.random.Generator, tirages: int = TIRAGES) -> None:
    d, idx = pr.d, pr.d.index
    S = [idx[c] for c in g.membres]
    k = len(S)
    presents = np.asarray(d.X[S].sum(axis=0)).ravel()
    preuves = np.flatnonzero(presents >= MIN_PRESENTS)
    ens_preuves = set(preuves.tolist())
    marches = [d.marches[j] for j in preuves]
    dates = sorted(m.date for m in marches if m.date)
    membres = set(g.membres)

    # Contextes
    acheteurs = {m.acheteur for m in marches}
    regions = {m.region for m in marches if m.region}
    domaines = Counter(l for m in marches for l in m.domaines)

    # Présence hors profil : domaines habituels calculés sur les marchés de la société SANS le groupe.
    hors, evaluables = {}, 0
    for c, i in zip(g.membres, S):
        hab = pr.habituels(i, ens_preuves)
        hors[c] = 0
        if hab is None:
            continue
        for j in pr.marches_de[i]:
            if j in ens_preuves and d.marches[j].domaines:
                evaluables += 1
                hors[c] += int(not (d.marches[j].domaines & hab))
    part_hors = sum(hors.values()) / evaluables if evaluables else 0.0

    # Qui gagne, dans les unités (marché ou lot) des marchés de preuve où un membre a soumissionné.
    unites_groupe = [u for m in marches for u in m.unites
                     if u.cle_gagnant and membres & {c["cle"] for c in u.concurrents}]
    gains = Counter(u.cle_gagnant for u in unites_groupe if u.cle_gagnant in membres)
    total_gains = sum(gains.values())
    part_gains = total_gains / len(unites_groupe) if unites_groupe else 0.0
    chef, part_chef = (gains.most_common(1)[0] if gains else (None, 0))
    part_chef = part_chef / total_gains if total_gains else 0.0
    if total_gains < MIN_VICTOIRES:
        structure = "peu de victoires"
    elif part_chef >= CHEF_DE_FILE:
        structure = "chef de file"
    else:
        structure = "rotation"
    entropie = 0.0
    if total_gains and k > 1:
        ps = [n / total_gains for n in gains.values()]
        entropie = -sum(p * math.log(p) for p in ps) / math.log(k)

    # Taux de victoire de chaque membre avec et sans le groupe.
    detail_membres = []
    for c, i in zip(g.membres, S):
        avec = [u for j, u in pr.unites_de[c] if u.cle_gagnant and j in ens_preuves]
        sans = [u for j, u in pr.unites_de[c] if u.cle_gagnant and j not in ens_preuves]
        detail_membres.append({
            "cle": c, "nom": d.noms.get(c, c), "marches": len(pr.marches_de[i]),
            "presences_groupe": int(sum(1 for j in pr.marches_de[i] if j in ens_preuves)),
            "gains_groupe": int(gains.get(c, 0)),
            "n_avec": len(avec), "taux_avec": (sum(u.cle_gagnant == c for u in avec) / len(avec)
                                               if avec else None),
            "n_sans": len(sans), "taux_sans": (sum(u.cle_gagnant == c for u in sans) / len(sans)
                                               if sans else None),
            "hors_profil": hors[c]})

    # Position des offres non gagnantes, en points de taux, quand un membre gagne.
    ecarts = {"membres_gagnant": [], "membres_reference": [], "autres_gagnant": [], "autres_reference": []}
    for u in unites_groupe:
        if u.cle_gagnant not in membres or not u.estimation:
            continue
        t_g = _taux_de(u, u.cle_gagnant)
        t_ref = u.prix_reference_stocke / u.estimation if u.prix_reference_stocke else None
        for c in u.concurrents:
            if not c["taux"] or c["cle"] == u.cle_gagnant or c["statut"] not in ("admis", "attributaire"):
                continue
            qui = "membres" if c["cle"] in membres else "autres"
            if t_g:
                ecarts[f"{qui}_gagnant"].append(c["taux"] - t_g)
            if t_ref:
                ecarts[f"{qui}_reference"].append(c["taux"] - t_ref)
    position = {cle: (float(np.median(v)) if v else None) for cle, v in ecarts.items()}
    position["n_membres"] = len(ecarts["membres_gagnant"])

    # Comparaison au hasard : mêmes acheteurs, même période, sociétés d'activité comparable.
    attendu, p_valeur, rapport = _hasard(S, preuves, acheteurs, dates, pr, rng, tirages)

    force = np.mean([min(toutes[paire(a, b)].part_a, toutes[paire(a, b)].part_b)
                     for x, a in enumerate(g.membres) for b in g.membres[x + 1:]
                     if paire(a, b) in toutes])
    g.mesures = {
        "taille": k, "force": float(force),
        "marches_3": int(len(preuves)), "marches_complet": int((presents[preuves] == k).sum()),
        "acheteurs": len(acheteurs), "regions": len(regions),
        "domaines": "".join(sorted(domaines)), "part_hors_profil": part_hors,
        "hors_profil_evaluables": evaluables,
        "unites": len(unites_groupe), "gains": total_gains, "part_gains": part_gains,
        "chef_de_file": chef, "part_chef": part_chef, "structure": structure,
        "entropie_gains": entropie, "position": position,
        "attendu_hasard": attendu, "p_hasard": p_valeur, "rapport_hasard": rapport,
        "premier": dates[0] if dates else "", "dernier": dates[-1] if dates else "",
        "membres": detail_membres,
        "preuves": [{"ref": m.ref, "date": m.date, "acheteur": m.acheteur,
                     "presents": int(presents[j]), "complet": bool(presents[j] == k),
                     "gagnants": sorted({u.cle_gagnant for u in m.unites if u.cle_gagnant})}
                    for j, m in zip(preuves, marches)],
    }
    score(g)


def _hasard(S, preuves, acheteurs, dates, pr: Profils, rng, tirages):
    """Combien de marchés « à ≥ 3 présents » ferait un ensemble de sociétés tirées au hasard ?

    Univers : les marchés des mêmes acheteurs, dans la même période (premier → dernier marché de
    preuve). Chaque membre est remplacé par une société active dans cet univers, d'activité
    comparable (entre la moitié et le double de ses présences ; à défaut les 10 plus proches) :
    deux grosses sociétés qui fréquentent le même acheteur se croisent forcément, le hasard doit
    en tenir compte.
    Rend (moyenne au hasard, valeur p empirique, rapport (observé+1)/(attendu+1)). Sans société
    comparable dans l'univers, la présence commune s'explique par l'univers lui-même : rapport 1.
    """
    observe = len(preuves)
    tire = tirer_semblables(S, acheteurs, dates, pr, rng, tirages)
    if tire is None:
        return float(observe), 1.0, 1.0
    D, Xu = tire
    comptes = (D @ Xu).toarray()
    hasard = (comptes >= MIN_PRESENTS).sum(axis=1)
    attendu = float(hasard.mean())
    p = float((1 + (hasard >= observe).sum()) / (tirages + 1))
    return attendu, p, (observe + 1) / (attendu + 1)


def tirer_semblables(S, acheteurs, dates, pr: Profils, rng, tirages: int):
    """Tirages d'ensembles semblables au groupe S (voir _hasard).

    Rend (D, Xu) : D (tirages × sociétés) marque les sociétés de chaque tirage, Xu est la matrice
    sociétés × marchés de l'univers ; ou None s'il n'y a pas assez de candidats.
    """
    d = pr.d
    lo, hi = (dates[0], dates[-1]) if dates else ("", "99999999")
    univers = [j for j, m in enumerate(d.marches)
               if m.acheteur in acheteurs and (not m.date or lo <= m.date <= hi)]
    Xu = pr.Xc[:, univers].tocsr()
    activite = np.asarray(Xu.sum(axis=1)).ravel()
    candidats = np.setdiff1d(np.flatnonzero(activite > 0), S)
    if len(candidats) < len(S):
        return None
    tirage = np.empty((tirages, len(S)), dtype=np.int64)
    log_c = np.log(activite[candidats])
    for col, i in enumerate(S):
        a = activite[i]
        pool = candidats[(activite[candidats] >= a / 2) & (activite[candidats] <= 2 * a)]
        if len(pool) < 10:
            pool = candidats[np.argsort(np.abs(log_c - math.log(a)))[:10]]
        tirage[:, col] = rng.choice(pool, size=tirages)
    D = sparse.csr_matrix((np.ones(tirage.size, dtype=np.int32),
                           (np.repeat(np.arange(tirages), len(S)), tirage.ravel())),
                          shape=(tirages, len(d.societes)))
    D.data[:] = 1                                # un même tirage deux fois ne compte qu'une
    return D, Xu


def score(g: Groupe) -> None:
    """Score de 0 à 100 : somme pondérée de composantes entre 0 et 1.

    force       présence mutuelle moyenne des paires du groupe, 30 % → 0, 100 % → 1
    volume      marchés de preuve, échelle log : 1 → 0,18 ; 10 → 0,61 ; 50 et plus → 1
    hasard      rapport au hasard, échelle log : 1 → 0, 10 et plus → 1 ; divisé par 2 si p > 0,05
    contextes   acheteurs (½, 5 et plus → 1), régions (¼, 3 et plus → 1), domaines (¼, 3 et plus → 1)
    hors_profil part des présences hors des domaines habituels, 20 % et plus → 1
    gains       part des unités du groupe gagnées par un membre, 60 % et plus → 1
    """
    m = g.mesures
    c = {
        "force": _clip((m["force"] - 0.30) / 0.70),
        "volume": _clip(math.log1p(m["marches_3"]) / math.log1p(50)),
        "hasard": _clip(math.log(max(m["rapport_hasard"], 1.0)) / math.log(10))
                  * (1.0 if m["p_hasard"] <= 0.05 else 0.5),
        "contextes": 0.5 * _clip((m["acheteurs"] - 1) / 4) + 0.25 * _clip((m["regions"] - 1) / 2)
                     + 0.25 * _clip((len(m["domaines"]) - 1) / 2),
        "hors_profil": _clip(m["part_hors_profil"] / 0.20),
        "gains": _clip(m["part_gains"] / 0.60),
    }
    g.composantes = {k: round(v, 3) for k, v in c.items()}
    g.score = int(round(sum(POIDS[k] * v for k, v in c.items())))


# --------------------------------------------------------------------------- ensemble

@dataclass
class Resultat:
    donnees: Donnees
    seuil: float
    paires: list[Paire]
    groupes: list[Groupe]
    familles: list[Famille]
    avis: dict
    duree: float = 0.0


def detecter(d: Donnees, seuil: float = SEUIL, avis: dict | None = None,
             tirages: int = TIRAGES) -> Resultat:
    debut = datetime.now()
    avis = avis or {}
    liste = paires(d, seuil, avis)
    toutes = {(p.a, p.b): p for p in liste}
    G, trouvees, famille_de = cliques(liste)
    pr = Profils(d)
    rng = np.random.default_rng(GRAINE)
    idx = d.index
    groupes = []
    for membres in trouvees:
        g = Groupe(membres, famille=Counter(famille_de.get(c) for c in membres).most_common(1)[0][0])
        S = [idx[c] for c in membres]
        if int((np.asarray(d.X[S].sum(axis=0)).ravel() >= MIN_PRESENTS).sum()) < MIN_COMMUNS:
            continue
        mesurer(g, pr, toutes, rng, tirages)
        groupes.append(g)
    groupes.sort(key=lambda g: (-g.score, -g.mesures["marches_3"], g.signature))
    return Resultat(d, seuil, liste, groupes, ranger_familles(groupes), avis,
                    (datetime.now() - debut).total_seconds())


def accord(brut: Resultat, avis: dict, seuil_score: int = 50) -> dict:
    """Accord entre le score (calculé SANS les avis) et les avis tranchés.

    Pour chaque paire avec avis : score = le meilleur score d'un groupe qui contient les deux.
    Le score « dit liées » s'il atteint `seuil_score`.
    """
    meilleur: dict[tuple[str, str], int] = {}
    for g in brut.groupes:
        for x, a in enumerate(g.membres):
            for b in g.membres[x + 1:]:
                meilleur[(a, b)] = max(meilleur.get((a, b), 0), g.score)
    lignes = [(rep, meilleur.get(p, 0)) for p, rep in avis.items()]
    d = Counter((rep, s >= seuil_score) for rep, s in lignes)
    n = len(lignes)
    return {"avis": n, "oui_score_haut": d[("oui", True)], "oui_score_bas": d[("oui", False)],
            "non_score_haut": d[("non", True)], "non_score_bas": d[("non", False)],
            "accord": (d[("oui", True)] + d[("non", False)]) / n if n else None}


def identifiant(jusqua: str | None, seuil: float) -> str:
    nom = f"jusqua-{jusqua}" if jusqua else "courant"
    return nom if abs(seuil - SEUIL) < 1e-9 else f"{nom}-s{round(seuil * 100)}"


def enregistrer(db: sqlite3.Connection, r: Resultat, calcul: str | None = None,
                extra: dict | None = None) -> str:
    """Remplace, dans copilote_calcul.db, les lignes du calcul `calcul` par ce résultat."""
    calcul = calcul or identifiant(r.donnees.jusqua, r.seuil)
    db.executescript(SCHEMA_CALCUL)
    with db:
        for table in ("groupes_calculs", "groupes_paires", "groupes", "groupe_membres", "groupe_preuves",
                      "familles", "famille_cliques", "liens_faibles"):
            db.execute(f"DELETE FROM {table} WHERE calcul = ?", (calcul,))
        db.execute("INSERT INTO groupes_calculs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            calcul, r.donnees.jusqua, r.seuil, datetime.now(timezone.utc).isoformat(timespec="seconds"),
            len(r.donnees.marches), len(r.donnees.societes), len(r.paires),
            int(sum(p.retenue for p in r.paires)), len(r.groupes), r.donnees.nb_fusions, len(r.avis),
            json.dumps({"familles": len(r.familles), "duree_s": round(r.duree, 1), "poids": POIDS,
                        **(extra or {})}, ensure_ascii=False)))
        db.executemany("INSERT INTO groupes_paires VALUES (?,?,?,?,?,?,?,?)", [
            (calcul, p.a, p.b, p.communs, round(p.part_a, 4), round(p.part_b, 4), int(p.retenue),
             p.origine) for p in r.paires])
        db.executemany("INSERT INTO liens_faibles VALUES (?,?,?,?,?,?)", [
            (calcul, p.a, p.b, p.communs, round(p.part_a, 4), round(p.part_b, 4))
            for p in liens_faibles(r.paires, r.seuil)])
        numero_de = {g.signature: n for n, g in enumerate(r.groupes, 1)}
        maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for f in r.familles:
            permanents, occasionnels = json.dumps(f.permanents), json.dumps(f.occasionnels)
            db.execute("INSERT INTO familles VALUES (?,?,?,?,?,?,?,?,?)", (
                calcul, f.cle, f.numero, f.score, len(f.membres), len(f.cliques),
                numero_de[f.meilleure.signature], permanents, occasionnels))
            db.executemany("INSERT INTO famille_cliques VALUES (?,?,?)",
                           [(calcul, f.cle, numero_de[g.signature]) for g in f.cliques])
            if calcul == "courant":      # mémoire des liens déjà envoyés : jamais effacée par un recalcul
                db.execute("INSERT OR REPLACE INTO familles_anciennes VALUES (?,?,?,?)",
                           (f.cle, permanents, occasionnels, maintenant))
        for n, g in enumerate(r.groupes, 1):
            m = g.mesures
            details = {k: v for k, v in m.items() if k not in ("membres", "preuves")}
            details["composantes"] = g.composantes
            db.execute("INSERT INTO groupes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                calcul, n, g.signature, m["taille"], g.famille, g.score, m["chef_de_file"],
                m["structure"], m["marches_complet"], m["marches_3"], m["acheteurs"], m["regions"],
                m["domaines"], round(m["part_hors_profil"], 4), round(m["part_gains"], 4),
                round(m["attendu_hasard"], 3), round(m["rapport_hasard"], 3), round(m["p_hasard"], 4),
                m["premier"], m["dernier"], json.dumps(details, ensure_ascii=False)))
            db.executemany("INSERT INTO groupe_membres VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                (calcul, n, x["cle"], x["nom"], x["marches"], x["presences_groupe"], x["gains_groupe"],
                 x["taux_avec"], x["n_avec"], x["taux_sans"], x["n_sans"], x["hors_profil"])
                for x in m["membres"]])
            db.executemany("INSERT INTO groupe_preuves VALUES (?,?,?,?,?,?,?,?)", [
                (calcul, n, x["ref"], x["date"], x["acheteur"], x["presents"], int(x["complet"]),
                 ",".join(x["gagnants"])) for x in m["preuves"]])
    return calcul
