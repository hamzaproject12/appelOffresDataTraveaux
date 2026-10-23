"""Groupes de présence commune : des sociétés qui soumissionnent souvent ensemble.

Ce sont des faits statistiques de présence, pas des accusations. Un groupe peut réunir des
partenaires habituels, des sociétés d'un même dirigeant, des membres d'un groupement, ou des
concurrents qui visent les mêmes acheteurs. Les avis humains (table avis_groupes de
copilote_retours.db) tranchent.

Étapes :
  1. matrice creuse sociétés × marchés (une colonne par MARCHÉ, pas par lot ; toute offre déposée
     compte, écartée comprise), puis X·Xᵀ = marchés communs de toutes les paires ;
  2. liens (liens.py) : pour chaque paire, attendu par hasard chez les mêmes acheteurs, excès,
     valeur p de Poisson, et un niveau fort / moyen / faible (seuils de reglages.json) ;
     avis « liées : oui » ⇒ retenue d'office, « non » ⇒ exclue ;
  3. graphe des liens fort et moyen (et des avis oui) ; un GROUPE est une clique maximale d'au
     moins 2 sociétés. Une clique de 3 ou plus doit réunir au moins 3 membres sur ≥ MIN_COMMUNS
     marchés, sinon ses liens restent des groupes de 2. Les cliques qui se recoupent forment une
     FAMILLE (Louvain, découpée pour que toutes ses cliques aient un membre en commun, puis
     redécoupée au-delà de `taille_max_famille` sociétés) : c'est l'unité affichée. Membres
     permanents = présents dans toutes les cliques de la famille ; occasionnels = dans certaines ;
  4. confirmation : contextes (acheteurs, domaines, régions), présence hors profil, comparaison
     au hasard ; qui gagne et, sur les marchés perdus, qui l'emporte et à quel écart ;
  5. score de 0 à 100. « Qui gagne » décrit le comportement et ne compte pas dans le score.

`jusqua` (AAAA-MM-JJ) ne garde que les marchés datés jusqu'à ce jour inclus (dates douteuses
exclues), y compris pour les profils, le hasard et les avis : aucune fuite pour le backtest.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property

import networkx as nx
import numpy as np
from scipy import sparse

from . import fusions as _fusions
from . import liens as _liens
from .donnees import ADMIS, Unite, charger_unites, rejouer_attribution
from .regions import region_de

MIN_COMMUNS = 5             # marchés à ≥ 3 membres présents pour garder une clique de 3 ou plus
MIN_PRESENTS = 3            # membres présents sur un marché pour qu'il serve de preuve (2 pour une paire)
TIRAGES = 200               # tirages de la comparaison au hasard (groupes de 3 ou plus)
GRAINE = 0
PART_HABITUELLE = 0.10      # un domaine est habituel s'il fait ≥ 10 % des marchés hors groupe
MIN_PROFIL = 5              # marchés hors groupe nécessaires pour juger d'un profil
CHEF_DE_FILE = 0.60         # part des victoires du groupe au-delà de laquelle un membre mène
MIN_VICTOIRES = 3           # en dessous, on ne dit rien de la répartition des victoires
SCHEMA_VERSION = "2"        # change quand les tables du calcul changent : elles sont alors refaites

CRITERES = ("force", "volume", "hasard", "contextes", "hors_profil", "gains")

SCHEMA_AVIS = """
CREATE TABLE IF NOT EXISTS avis_groupes (
  id INTEGER PRIMARY KEY,
  cle_a TEXT NOT NULL, cle_b TEXT NOT NULL,          -- cle_a < cle_b
  reponse TEXT NOT NULL CHECK (reponse IN ('oui', 'non', 'nsp')),
  commentaire TEXT, auteur TEXT NOT NULL, cree_le TEXT NOT NULL,
  groupe TEXT);                                      -- signature du groupe affiché, pour mémoire
CREATE INDEX IF NOT EXISTS i_avis_paire ON avis_groupes(cle_a, cle_b);
"""

# Tables refaites quand SCHEMA_VERSION change. familles_anciennes n'en fait pas partie : elle garde
# la mémoire des liens déjà envoyés et n'est jamais vidée.
TABLES_CALCUL = ("groupes_calculs", "groupes_liens", "lien_preuves", "groupes_societes", "groupes",
                 "groupe_membres", "groupe_preuves", "groupes_defaites", "familles", "famille_cliques",
                 "groupes_paires", "liens_faibles")             # les deux dernières : version 1

SCHEMA_CALCUL = """
CREATE TABLE IF NOT EXISTS groupes_calculs (
  calcul TEXT PRIMARY KEY, jusqua TEXT, cree_le TEXT, marches INTEGER, societes INTEGER,
  paires_examinees INTEGER, paires_retenues INTEGER, groupes INTEGER, fusions INTEGER, avis INTEGER,
  details TEXT);
CREATE TABLE IF NOT EXISTS groupes_liens (
  calcul TEXT, cle_a TEXT, cle_b TEXT, communs INTEGER, attendu REAL, exces REAL, p REAL,
  part_a REAL, part_b REAL, niveau TEXT, mutuel INTEGER, retenue INTEGER, origine TEXT,
  unites INTEGER, gains_a INTEGER, gains_b INTEGER, gains_autres INTEGER, taux_victoire REAL,
  perdues INTEGER, ecart_meilleure REAL, ecart_reference REAL,
  PRIMARY KEY (calcul, cle_a, cle_b));
CREATE INDEX IF NOT EXISTS i_liens_b ON groupes_liens(calcul, cle_b);
CREATE TABLE IF NOT EXISTS lien_preuves (
  calcul TEXT, cle_a TEXT, cle_b TEXT, ref TEXT, date TEXT, acheteur TEXT, gagnants TEXT,
  PRIMARY KEY (calcul, cle_a, cle_b, ref));
CREATE TABLE IF NOT EXISTS groupes_societes (
  calcul TEXT, cle TEXT, nom TEXT, marches INTEGER, fort INTEGER, moyen INTEGER, faible INTEGER,
  groupes INTEGER, PRIMARY KEY (calcul, cle));
CREATE TABLE IF NOT EXISTS groupes (
  calcul TEXT, id INTEGER, signature TEXT, taille INTEGER, famille INTEGER, score INTEGER,
  niveau TEXT, confirme INTEGER, chef_de_file TEXT, structure TEXT, marches_complet INTEGER,
  marches_3 INTEGER, acheteurs INTEGER, regions INTEGER, domaines TEXT, part_hors_profil REAL,
  part_gains REAL, taux_victoire REAL, attendu_hasard REAL, rapport_hasard REAL, p_hasard REAL,
  premier TEXT, dernier TEXT, details TEXT, PRIMARY KEY (calcul, id));
CREATE TABLE IF NOT EXISTS groupe_membres (
  calcul TEXT, groupe INTEGER, cle TEXT, nom TEXT, marches INTEGER, presences_groupe INTEGER,
  gains_groupe INTEGER, taux_avec REAL, n_avec INTEGER, taux_sans REAL, n_sans INTEGER,
  hors_profil INTEGER, PRIMARY KEY (calcul, groupe, cle));
CREATE INDEX IF NOT EXISTS i_gm_cle ON groupe_membres(calcul, cle);
CREATE TABLE IF NOT EXISTS groupe_preuves (
  calcul TEXT, groupe INTEGER, ref TEXT, date TEXT, acheteur TEXT, presents INTEGER,
  complet INTEGER, gagnants TEXT, PRIMARY KEY (calcul, groupe, ref));
CREATE TABLE IF NOT EXISTS groupes_defaites (       -- marchés ou lots perdus par un groupe ou une paire
  calcul TEXT, type TEXT, groupe INTEGER, cle_a TEXT, cle_b TEXT, ref TEXT, lot TEXT, date TEXT,
  acheteur TEXT, gagnant_cle TEXT, gagnant_nom TEXT, taux_gagnant REAL, meilleur_membre TEXT,
  taux_meilleur REAL, taux_reference REAL, ecart_meilleure REAL, ecart_reference REAL,
  membres_ecartes INTEGER);
CREATE INDEX IF NOT EXISTS i_def_groupe ON groupes_defaites(calcul, groupe);
CREATE INDEX IF NOT EXISTS i_def_paire ON groupes_defaites(calcul, cle_a, cle_b);
CREATE TABLE IF NOT EXISTS familles (
  calcul TEXT, id TEXT, numero INTEGER, score INTEGER, taille INTEGER, cliques INTEGER,
  meilleure_clique INTEGER, permanents TEXT, occasionnels TEXT, niveaux TEXT, marches INTEGER,
  premier TEXT, dernier TEXT, PRIMARY KEY (calcul, id));
CREATE TABLE IF NOT EXISTS famille_cliques (
  calcul TEXT, famille TEXT, groupe INTEGER, PRIMARY KEY (calcul, groupe));
CREATE INDEX IF NOT EXISTS i_fc_famille ON famille_cliques(calcul, famille);
CREATE TABLE IF NOT EXISTS familles_anciennes (   -- jamais vidée par un recalcul : sert aux liens déjà envoyés
  id TEXT PRIMARY KEY, permanents TEXT, occasionnels TEXT, vue_le TEXT);
"""


def poids_effectifs(reglages: dict | None = None) -> dict[str, float]:
    """Les poids de reglages.json ramenés à un total de 100 (« gains » vaut 0 : ses points sont répartis)."""
    p = _liens.reglages_groupes(reglages)["poids"]
    total = sum(p.get(c, 0) for c in CRITERES)
    return {c: p.get(c, 0) * 100 / total for c in CRITERES}


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
    X: sparse.csr_matrix            # sociétés × marchés, 1 = au moins une offre déposée (écartée comprise)
    jusqua: str | None
    nb_fusions: int

    @cached_property
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

@dataclass(slots=True)                  # 117 000 paires sur l'historique complet : la mémoire compte
class Paire:
    a: str
    b: str
    communs: int
    part_a: float           # part des marchés de A où B est présente
    part_b: float           # part des marchés de B où A est présente
    attendu: float          # marchés communs attendus par hasard chez les mêmes acheteurs
    exces: float            # communs / attendu
    p: float                # valeur p de Poisson
    rang: int               # 3 fort, 2 moyen, 1 faible, 0 aucun (liens.RANG)
    mutuel: bool            # présence mutuelle habituelle (seconde voie vers « moyen »)
    retenue: bool           # sert à former les groupes : fort, moyen, ou avis oui
    origine: str            # fort | moyen | faible | '' | avis oui | avis non
    issues: dict = field(default_factory=dict)        # qui gagne quand elles sont ensemble (rempli à l'écriture)

    @property
    def niveau(self) -> str | None:
        return _liens.NOM_RANG[self.rang]


def paires(d: Donnees, avis: dict | None = None, reglages: dict | None = None) -> list[Paire]:
    """Toutes les paires croisées au moins 3 fois (minimum du niveau faible), plus celles qui ont un avis."""
    avis = avis or {}
    L = _liens.calculer(d, reglages)
    resultat: dict[tuple[str, str], Paire] = {}
    soc = d.societes
    for k in range(len(L)):
        a, b = soc[L.i[k]], soc[L.j[k]]
        rang = int(L.rang[k])
        resultat[(a, b)] = Paire(a, b, int(L.communs[k]), float(L.part_i[k]), float(L.part_j[k]),
                                 float(L.attendu[k]), float(L.exces[k]), float(L.p[k]), rang,
                                 bool(L.mutuel[k]), rang >= 2, _liens.NOM_RANG[rang] or "")
    idx = d.index
    for (a, b), reponse in avis.items():
        p = resultat.get((a, b))
        if p is None:
            if a not in idx or b not in idx:
                continue                         # société absente de la fenêtre
            m = _liens.paire_isolee(d, idx[a], idx[b])
            p = resultat[(a, b)] = Paire(a, b, m["communs"], m["part_a"], m["part_b"], m["attendu"],
                                         m["exces"], m["p"], 0, False, False, "")
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
    defaites: list = field(default_factory=list)

    @property
    def signature(self) -> str:
        return "|".join(self.membres)


def graphe(liste: list[Paire]) -> nx.Graph:
    """Les liens qui forment les groupes : fort, moyen, avis oui. Poids : la part la plus élevée."""
    G = nx.Graph()
    for p in liste:
        if p.retenue:
            G.add_edge(p.a, p.b, weight=max(p.part_a, p.part_b))
    return G


def cliques(liste: list[Paire]) -> tuple[nx.Graph, list[list[str]], dict[str, int]]:
    G = graphe(liste)
    trouvees = sorted(sorted(c) for c in nx.find_cliques(G) if len(c) >= 2)
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


RESOLUTIONS = (1.5, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0)


def _redecouper(cliques: list[Groupe], G: nx.Graph, taille_max: int) -> list[list[Groupe]]:
    """Garde-fou contre les familles géantes : un morceau de plus de `taille_max` sociétés est redécoupé en
    communautés plus denses (Louvain sur ses seuls membres, résolution croissante jusqu'à ce que chaque
    communauté tienne dans `taille_max`). Chaque clique rejoint la communauté qui a le plus de ses
    membres. Une clique seule plus grande que `taille_max` reste seule."""
    finis, a_faire = [], [cliques]
    while a_faire:
        morceau = a_faire.pop()
        membres = set().union(*(g.membres for g in morceau))
        if len(membres) <= taille_max or len(morceau) == 1:
            finis.append(morceau)
            continue
        # Sous-graphe construit dans un ordre trié : une vue G.subgraph() parcourt ses nœuds dans l'ordre
        # d'un set, qui change d'un lancement à l'autre, et Louvain dépend de cet ordre.
        noeuds = sorted(membres)
        sous = nx.Graph()
        sous.add_nodes_from(noeuds)
        sous.add_weighted_edges_from((a, b, G[a][b]["weight"]) for a in noeuds for b in sorted(G[a])
                                     if a < b and b in membres)
        communautes = None
        for r in RESOLUTIONS:
            communautes = nx.community.louvain_communities(sous, weight="weight", resolution=r, seed=GRAINE)
            if max(len(c) for c in communautes) <= taille_max:
                break
        rang = {}
        for k, c in enumerate(sorted(communautes, key=lambda c: (-len(c), sorted(c)))):
            for cle in c:
                rang[cle] = k
        par_communaute: dict[int, list[Groupe]] = defaultdict(list)
        for g in morceau:
            compte = Counter(rang[c] for c in g.membres)
            par_communaute[min(compte, key=lambda k: (-compte[k], k))].append(g)
        nouveaux = [m for liste in par_communaute.values() for m in _decouper(liste)]
        if len(nouveaux) == 1:
            # Des cliques qui se recouvrent presque toutes tombent dans la même communauté : on les
            # range alors une à une, sans jamais dépasser `taille_max` sociétés par morceau.
            finis.extend(_emballer(morceau, taille_max))
        else:
            a_faire.extend(nouveaux)
    return finis


def _emballer(cliques: list[Groupe], taille_max: int) -> list[list[Groupe]]:
    """Comme _decouper, mais une clique ne rejoint un morceau que si le morceau reste à `taille_max`
    sociétés au plus ; sinon elle en ouvre un nouveau."""
    morceaux: list[tuple[set[str], set[str], list[Groupe]]] = []
    for g in sorted(cliques, key=lambda g: (-g.score, g.signature)):
        membres = set(g.membres)
        meilleur_i, meilleur_n = None, 0
        for i, (noyau, tous, _) in enumerate(morceaux):
            n = len(noyau & membres)
            if n > meilleur_n and len(tous | membres) <= taille_max:
                meilleur_i, meilleur_n = i, n
        if meilleur_i is None:
            morceaux.append((membres, set(membres), [g]))
        else:
            noyau, tous, liste = morceaux[meilleur_i]
            morceaux[meilleur_i] = (noyau & membres, tous | membres, liste + [g])
    return [liste for _, _, liste in morceaux]


def ranger_familles(groupes: list[Groupe], G: nx.Graph | None = None, taille_max: int | None = None) -> list[Famille]:
    """Rassemble les cliques en familles ; numérote par score décroissant et pose les empreintes."""
    par_louvain: dict[int | None, list[Groupe]] = defaultdict(list)
    for g in groupes:
        par_louvain[g.famille].append(g)
    familles = []
    for cliques in par_louvain.values():
        for morceau in _decouper(cliques):
            pieces = _redecouper(morceau, G, taille_max) if G is not None and taille_max else [morceau]
            for piece in pieces:
                tous = [set(g.membres) for g in piece]
                permanents = set.intersection(*tous)
                familles.append(Famille(piece, sorted(permanents), sorted(set.union(*tous) - permanents)))
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
        par_acheteur: dict[str, list[int]] = defaultdict(list)
        for j, m in enumerate(d.marches):
            par_acheteur[m.acheteur].append(j)
        self.par_acheteur = {a: np.array(js, dtype=np.int64) for a, js in par_acheteur.items()}
        self.dates = np.array([m.date for m in d.marches], dtype="U8")
        self._attributions: dict[int, object] = {}

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

    def attribution(self, u: Unite):
        """L'ouverture des plis rejouée (construire_base), gardée pour ne la faire qu'une fois par unité."""
        a = self._attributions.get(id(u))
        if a is None:
            a = self._attributions[id(u)] = rejouer_attribution(u)
        return a


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _taux_de(u: Unite, cle: str) -> float | None:
    return next((c["taux"] for c in u.concurrents if c["cle"] == cle and c["taux"]), None)


def _mediane(v: list[float]) -> float | None:
    return float(statistics.median(v)) if v else None


# --------------------------------------------------------------------------- qui gagne, qui perd

def issues(unites: list[Unite], membres: set[str], pr: Profils, noms: dict[str, str]) -> tuple[dict, list[dict]]:
    """Victoires du groupe (ou de la paire) sur `unites`, et le détail des unités perdues.

    `unites` : les marchés ou lots où au moins un membre a déposé une offre. Une unité est jugée si
    son attributaire est connu. Sur une unité perdue on note qui l'a emporté (forcément hors du
    groupe), son taux, son écart avec la meilleure offre du groupe — celle que la commission classe
    le mieux parmi les offres admises des membres — et avec le prix de référence, et si toutes les
    offres des membres avaient été écartées.
    """
    jugees = [u for u in unites if u.cle_gagnant]
    gains = Counter(u.cle_gagnant for u in jugees if u.cle_gagnant in membres)
    perdues = []
    for u in jugees:
        if u.cle_gagnant in membres:
            continue
        admises = [(k, c) for k, c in enumerate(u.concurrents)
                   if c["cle"] in membres and c["statut"] in ADMIS]
        meilleur = None
        if admises:
            rangs = pr.attribution(u).rangs
            classees = [(rangs[k], c) for k, c in admises if k in rangs]
            if classees:
                meilleur = min(classees, key=lambda x: x[0])[1]
        t_g = _taux_de(u, u.cle_gagnant)
        t_ref = u.prix_reference_stocke / u.estimation if u.prix_reference_stocke and u.estimation else None
        t_m = meilleur["taux"] if meilleur else None
        nom_g = next((c["nom"] for c in u.concurrents if c["cle"] == u.cle_gagnant), None)
        perdues.append({
            "ref": u.ref, "lot": u.lot, "date": u.date, "acheteur": u.acheteur or "",
            "gagnant_cle": u.cle_gagnant, "gagnant_nom": nom_g or noms.get(u.cle_gagnant, u.cle_gagnant),
            "taux_gagnant": t_g, "meilleur_membre": meilleur["cle"] if meilleur else None,
            "taux_meilleur": t_m, "taux_reference": t_ref,
            "ecart_meilleure": t_g - t_m if t_g is not None and t_m is not None else None,
            "ecart_reference": t_g - t_ref if t_g is not None and t_ref is not None else None,
            "membres_ecartes": not admises})
    gagnants = Counter(x["gagnant_cle"] for x in perdues)
    resume = {
        "unites": len(jugees), "gagnees": sum(gains.values()),
        "taux": sum(gains.values()) / len(jugees) if jugees else None,
        "perdues": len(perdues), "perdues_ecartes": sum(x["membres_ecartes"] for x in perdues),
        "ecart_meilleure": _mediane([x["ecart_meilleure"] for x in perdues if x["ecart_meilleure"] is not None]),
        "ecart_reference": _mediane([x["ecart_reference"] for x in perdues if x["ecart_reference"] is not None]),
        "gains": dict(gains),
        "gagnants_exterieurs": [{"cle": c, "nom": noms.get(c, c), "fois": n} for c, n in gagnants.most_common(5)]}
    return resume, perdues


def a_detailler(p: Paire) -> bool:
    """Les paires dont on range qui gagne, les marchés en preuve et les marchés perdus."""
    return bool(p.rang) or p.origine.startswith("avis")


def mesurer_paire(p: Paire, pr: Profils) -> tuple[dict, list[tuple], list[dict]]:
    """Marchés communs (ref, date, acheteur, gagnants), qui gagne, marchés perdus d'une paire.

    Calculé au moment d'écrire, paire par paire : garder ces détails pour 30 000 paires coûterait
    plusieurs centaines de Mo au recalcul, qui tourne à côté du serveur."""
    d, idx = pr.d, pr.d.index
    communs = np.intersect1d(pr.marches_de[idx[p.a]], pr.marches_de[idx[p.b]], assume_unique=True)
    duo = {p.a, p.b}
    unites, preuves = [], []
    for j in communs:
        m = d.marches[j]
        unites.extend(u for u in m.unites if duo & {c["cle"] for c in u.concurrents})
        preuves.append((m.ref, m.date, m.acheteur, ",".join(sorted({u.cle_gagnant for u in m.unites if u.cle_gagnant}))))
    resume, perdues = issues(unites, duo, pr, d.noms)
    return resume, preuves, perdues


# --------------------------------------------------------------------------- mesures d'un groupe

def mesurer(g: Groupe, pr: Profils, toutes: dict[tuple[str, str], Paire],
            rng: np.random.Generator, tirages: int = TIRAGES, poids: dict | None = None) -> None:
    d, idx = pr.d, pr.d.index
    S = [idx[c] for c in g.membres]
    k = len(S)
    presents = np.asarray(d.X[S].sum(axis=0)).ravel()
    preuves = np.flatnonzero(presents >= min(MIN_PRESENTS, k))
    ens_preuves = set(preuves.tolist())
    marches = [d.marches[j] for j in preuves]
    dates = sorted(m.date for m in marches if m.date)
    membres = set(g.membres)
    liens_du_groupe = [toutes[paire(a, b)] for x, a in enumerate(g.membres) for b in g.membres[x + 1:]
                       if paire(a, b) in toutes]

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
    unites_groupe = [u for m in marches for u in m.unites if membres & {c["cle"] for c in u.concurrents}]
    victoires, perdues = issues(unites_groupe, membres, pr, d.noms)
    gains = Counter(victoires["gains"])
    total_gains = sum(gains.values())
    part_gains = victoires["taux"] or 0.0
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

    # Comparaison au hasard. Une paire : son attendu chez les mêmes acheteurs et sa valeur p de Poisson
    # (liens.py). Trois sociétés ou plus : tirages de sociétés comparables chez les mêmes acheteurs.
    if k == 2:
        lien = liens_du_groupe[0]
        attendu, p_valeur, rapport, methode = lien.attendu, lien.p, lien.exces, "poisson"
    else:
        attendu, p_valeur, rapport = _hasard(S, preuves, acheteurs, dates, pr, rng, tirages)
        methode = "tirages"

    rangs = [l.rang for l in liens_du_groupe]
    rang_min = min(rangs) if rangs else 0
    confirme = any(l.origine == "avis oui" for l in liens_du_groupe)
    g.mesures = {
        "taille": k,
        "force": float(np.mean([max(l.part_a, l.part_b) for l in liens_du_groupe])) if liens_du_groupe else 0.0,
        "force_mutuelle": float(np.mean([min(l.part_a, l.part_b) for l in liens_du_groupe])) if liens_du_groupe else 0.0,
        "exces_min": float(min(l.exces for l in liens_du_groupe)) if liens_du_groupe else 0.0,
        "niveau": _liens.NOM_RANG[rang_min] or "sans niveau", "rang_min": rang_min, "confirme": confirme,
        "marches_3": int(len(preuves)), "marches_complet": int((presents[preuves] == k).sum()),
        "acheteurs": len(acheteurs), "regions": len(regions),
        "domaines": "".join(sorted(domaines)), "part_hors_profil": part_hors,
        "hors_profil_evaluables": evaluables,
        "unites": victoires["unites"], "gains": total_gains, "part_gains": part_gains,
        "chef_de_file": chef, "part_chef": part_chef, "structure": structure,
        "entropie_gains": entropie, "position": position, "victoires": victoires,
        "attendu_hasard": attendu, "p_hasard": p_valeur, "rapport_hasard": rapport, "methode_hasard": methode,
        "premier": dates[0] if dates else "", "dernier": dates[-1] if dates else "",
        "membres": detail_membres,
        # (ref, date, acheteur, présents, au complet, gagnants) : des tuples, plus légers que des dicts
        "preuves": [(m.ref, m.date, m.acheteur, int(presents[j]), int(presents[j] == k),
                     ",".join(sorted({u.cle_gagnant for u in m.unites if u.cle_gagnant})))
                    for j, m in zip(preuves, marches)],
    }
    g.defaites = perdues
    score(g, poids)


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
    vides = np.array([], dtype=np.int64)
    univers = np.sort(np.concatenate([pr.par_acheteur.get(a, vides) for a in acheteurs] or [vides]))
    du = pr.dates[univers]
    univers = univers[(du == "") | ((du >= lo) & (du <= hi))]
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


def score(g: Groupe, poids: dict | None = None) -> None:
    """Score de 0 à 100 : somme pondérée de composantes entre 0 et 1 (poids_effectifs : total 100).

    force       présence moyenne des paires, dans le sens le plus élevé (B accompagne A compte),
                30 % → 0, 100 % → 1
    volume      marchés de preuve, échelle log : 1 → 0,18 ; 10 → 0,61 ; 50 et plus → 1
    hasard      rapport au hasard, échelle log : 1 → 0, 10 et plus → 1 ; divisé par 2 si p > 0,05
                (tirages pour 3 sociétés ou plus, excès et Poisson pour une paire)
    contextes   acheteurs (½, 5 et plus → 1), régions (¼, 3 et plus → 1), domaines (¼, 3 et plus → 1)
    hors_profil part des présences hors des domaines habituels, 20 % et plus → 1
    gains       part des unités gagnées par un membre, 60 % et plus → 1. Poids 0 : décrit le
                comportement (chef de file, rotation, peu de victoires), ne mesure pas le lien.
    """
    poids = poids or poids_effectifs()
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
    g.score = int(round(sum(poids[k] * v for k, v in c.items())))


# --------------------------------------------------------------------------- ensemble

@dataclass
class Resultat:
    donnees: Donnees
    paires: list[Paire]
    groupes: list[Groupe]
    familles: list[Famille]
    avis: dict
    poids: dict
    reglages: dict
    duree: float = 0.0
    profils: Profils | None = None          # pour détailler les paires à l'écriture (mesurer_paire)


def detecter(d: Donnees, avis: dict | None = None, tirages: int = TIRAGES,
             reglages: dict | None = None) -> Resultat:
    debut = datetime.now()
    avis = avis or {}
    rg = _liens.reglages_groupes(reglages)
    poids = poids_effectifs(reglages)
    liste = paires(d, avis, reglages)
    toutes = {(p.a, p.b): p for p in liste}
    G, trouvees, famille_de = cliques(liste)
    pr = Profils(d)
    rng = np.random.default_rng(GRAINE)
    idx = d.index

    def famille_de_groupe(membres):
        compte = Counter(famille_de.get(c) for c in membres)
        return min(compte, key=lambda f: (-compte[f], f if f is not None else 0))

    gardees, couvertes = [], set()
    for membres in trouvees:
        if len(membres) < 3:
            continue
        S = [idx[c] for c in membres]
        if int((np.asarray(d.X[S].sum(axis=0)).ravel() >= MIN_PRESENTS).sum()) < MIN_COMMUNS:
            continue                             # ses liens restent, en groupes de 2
        gardees.append(membres)
        couvertes.update(paire(a, b) for x, a in enumerate(membres) for b in membres[x + 1:])
    # Les liens qu'aucune clique gardée ne couvre forment des groupes de 2 sociétés.
    gardees += sorted([p.a, p.b] for p in liste if p.retenue and (p.a, p.b) not in couvertes)

    groupes = []
    for membres in gardees:
        g = Groupe(membres, famille=famille_de_groupe(membres))
        mesurer(g, pr, toutes, rng, tirages, poids)
        groupes.append(g)
    groupes.sort(key=lambda g: (-g.score, -g.mesures["marches_3"], g.signature))
    return Resultat(d, liste, groupes, ranger_familles(groupes, G, rg["taille_max_famille"]), avis,
                    poids, rg, (datetime.now() - debut).total_seconds(), pr)


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


def identifiant(jusqua: str | None) -> str:
    return f"jusqua-{jusqua}" if jusqua else "courant"


def preparer_calcul(db: sqlite3.Connection) -> None:
    """Crée les tables ; si elles datent d'une autre version, les refait (tous les calculs sont à refaire)."""
    db.execute("CREATE TABLE IF NOT EXISTS meta (cle TEXT PRIMARY KEY, valeur TEXT)")
    ligne = db.execute("SELECT valeur FROM meta WHERE cle = 'groupes_schema'").fetchone()
    if ligne is None or ligne[0] != SCHEMA_VERSION:
        with db:
            for table in TABLES_CALCUL:
                db.execute(f"DROP TABLE IF EXISTS {table}")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('groupes_schema', ?)", (SCHEMA_VERSION,))
    db.executescript(SCHEMA_CALCUL)


def a_jour(db: sqlite3.Connection) -> bool:
    """Vrai si les tables du calcul sont celles de cette version."""
    try:
        ligne = db.execute("SELECT valeur FROM meta WHERE cle = 'groupes_schema'").fetchone()
    except sqlite3.OperationalError:
        return False
    return ligne is not None and ligne[0] == SCHEMA_VERSION


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(float(x), n)


def _lignes_defaites(calcul: str, type_: str, groupe: int | None, a: str | None, b: str | None,
                     defaites: list[dict]) -> list[tuple]:
    return [(calcul, type_, groupe, a, b, x["ref"], x["lot"], x["date"], x["acheteur"], x["gagnant_cle"],
             x["gagnant_nom"], _r(x["taux_gagnant"]), x["meilleur_membre"], _r(x["taux_meilleur"]),
             _r(x["taux_reference"]), _r(x["ecart_meilleure"]), _r(x["ecart_reference"]),
             int(x["membres_ecartes"])) for x in defaites]


def enregistrer(db: sqlite3.Connection, r: Resultat, calcul: str | None = None,
                extra: dict | None = None) -> str:
    """Remplace, dans copilote_calcul.db, les lignes du calcul `calcul` par ce résultat."""
    calcul = calcul or identifiant(r.donnees.jusqua)
    preparer_calcul(db)
    d = r.donnees
    with db:
        for table in TABLES_CALCUL:
            if table in ("groupes_paires", "liens_faibles"):
                continue
            db.execute(f"DELETE FROM {table} WHERE calcul = ?", (calcul,))
        db.execute("INSERT INTO groupes_calculs VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            calcul, d.jusqua, datetime.now(timezone.utc).isoformat(timespec="seconds"),
            len(d.marches), len(d.societes), len(r.paires), int(sum(p.retenue for p in r.paires)),
            len(r.groupes), d.nb_fusions, len(r.avis),
            json.dumps({"familles": len(r.familles), "duree_s": round(r.duree, 1), "poids": r.poids,
                        "reglages": {k: v for k, v in r.reglages.items() if not k.startswith("_")},
                        **(extra or {})}, ensure_ascii=False)))

        # Les liens : toutes les paires examinées ; qui gagne, marchés en preuve et marchés perdus pour celles
        # qui ont un niveau ou un avis, calculés ici et écrits par lots (la mémoire reste bornée).
        pr = r.profils or Profils(d)
        lignes, preuves, defaites = [], [], []

        def vider():
            db.executemany("INSERT INTO groupes_liens VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lignes)
            db.executemany("INSERT INTO lien_preuves VALUES (?,?,?,?,?,?,?)", preuves)
            db.executemany("INSERT INTO groupes_defaites VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", defaites)
            lignes.clear()
            preuves.clear()
            defaites.clear()

        for p in r.paires:
            s = {}
            if a_detailler(p):
                s, marches, perdues = mesurer_paire(p, pr)
                p.issues = s
                preuves.extend((calcul, p.a, p.b, *x) for x in marches)
                defaites.extend(_lignes_defaites(calcul, "paire", None, p.a, p.b, perdues))
            lignes.append((calcul, p.a, p.b, p.communs, _r(p.attendu), _r(p.exces, 3), float(f"{p.p:.3g}"),
                           _r(p.part_a), _r(p.part_b), p.niveau, int(p.mutuel), int(p.retenue), p.origine,
                           s.get("unites"), s["gains"].get(p.a, 0) if s else None,
                           s["gains"].get(p.b, 0) if s else None,
                           s.get("perdues"),                          # gagnées par une société extérieure
                           _r(s.get("taux")), s.get("perdues"), _r(s.get("ecart_meilleure")),
                           _r(s.get("ecart_reference"))))
            if len(lignes) >= 20_000 or len(preuves) >= 50_000:
                vider()
        vider()

        # Chaque société de la fenêtre : ses marchés, ses liens par niveau, ses groupes.
        n = np.asarray(d.X.sum(axis=1)).ravel()
        par_niveau: dict[str, Counter] = {nom: Counter() for nom in _liens.NIVEAUX}
        for p in r.paires:
            if p.niveau:
                par_niveau[p.niveau][p.a] += 1
                par_niveau[p.niveau][p.b] += 1
        nb_groupes = Counter(c for g in r.groupes for c in g.membres)
        db.executemany("INSERT INTO groupes_societes VALUES (?,?,?,?,?,?,?,?)", [
            (calcul, c, d.noms.get(c, c), int(n[i]), par_niveau["fort"][c], par_niveau["moyen"][c],
             par_niveau["faible"][c], nb_groupes[c]) for i, c in enumerate(d.societes)])

        numero_de = {g.signature: k for k, g in enumerate(r.groupes, 1)}
        maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for f in r.familles:
            permanents, occasionnels = json.dumps(f.permanents), json.dumps(f.occasionnels)
            niveaux = sorted({g.mesures["niveau"] for g in f.cliques}
                             | ({"confirme"} if any(g.mesures["confirme"] for g in f.cliques) else set()))
            dates = [g.mesures[x] for g in f.cliques for x in ("premier", "dernier") if g.mesures[x]]
            db.execute("INSERT INTO familles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                calcul, f.cle, f.numero, f.score, len(f.membres), len(f.cliques),
                numero_de[f.meilleure.signature], permanents, occasionnels, ",".join(niveaux),
                max(g.mesures["marches_3"] for g in f.cliques), min(dates, default=""), max(dates, default="")))
            db.executemany("INSERT INTO famille_cliques VALUES (?,?,?)",
                           [(calcul, f.cle, numero_de[g.signature]) for g in f.cliques])
            if calcul == "courant":      # mémoire des liens déjà envoyés : jamais effacée par un recalcul
                db.execute("INSERT OR REPLACE INTO familles_anciennes VALUES (?,?,?,?)",
                           (f.cle, permanents, occasionnels, maintenant))
        for k, g in enumerate(r.groupes, 1):
            m = g.mesures
            details = {x: v for x, v in m.items() if x not in ("membres", "preuves")}
            details["composantes"] = g.composantes
            details["poids"] = r.poids
            db.execute("INSERT INTO groupes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                calcul, k, g.signature, m["taille"], g.famille, g.score, m["niveau"], int(m["confirme"]),
                m["chef_de_file"], m["structure"], m["marches_complet"], m["marches_3"], m["acheteurs"],
                m["regions"], m["domaines"], round(m["part_hors_profil"], 4), round(m["part_gains"], 4),
                _r(m["victoires"]["taux"]), round(m["attendu_hasard"], 3), round(m["rapport_hasard"], 3),
                float(f"{m['p_hasard']:.3g}"), m["premier"], m["dernier"], json.dumps(details, ensure_ascii=False)))
            db.executemany("INSERT INTO groupe_membres VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                (calcul, k, x["cle"], x["nom"], x["marches"], x["presences_groupe"], x["gains_groupe"],
                 x["taux_avec"], x["n_avec"], x["taux_sans"], x["n_sans"], x["hors_profil"])
                for x in m["membres"]])
            db.executemany("INSERT INTO groupe_preuves VALUES (?,?,?,?,?,?,?,?)",
                           [(calcul, k, *x) for x in m["preuves"]])
            defaites.extend(_lignes_defaites(calcul, "groupe", k, None, None, g.defaites))
        db.executemany("INSERT INTO groupes_defaites VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", defaites)
    return calcul
