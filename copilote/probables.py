"""Les sociétés probables d'un AO : qui viendra, avec quelle probabilité, et combien elles seront.

Entrées : les marchés similaires de l'étape 4 (unité, note sur 100, récence), l'acheteur rapproché, les
codes de qualification, la région, la famille de travaux et l'estimation de l'AO, et sa LIMITE
(rejeu.limite_pour : la veille de l'ouverture réelle pour un « REJEU- », rien pour un vrai AO). Tout ce
qui est lu de l'historique passe par `Memoire.coupe(limite)` : un préfixe des unités datées, même règle
que rejeu.unites_pour.

Candidats : les sociétés présentes dans les 300 marchés les plus similaires, et celles qui ont déposé
chez l'acheteur (3 ans) ou dans la région de l'AO (12 mois), sans qualification incompatible.

Probabilité de venir : une formule logistique p = 1 / (1 + e^-(b0 + Σ bk·xk)) sur ces variables :

    similaires      Σ poids des 50 similaires où elle est présente / Σ poids (poids = note/100 × récence)
    similaires_n    log(1 + nombre des 50 similaires où elle est présente)
    proches_n       log(1 + nombre des 300 plus similaires où elle est présente)
    acheteur        marchés de l'acheteur (3 ans) où elle est présente / (marchés de l'acheteur + 1)
    acheteur_n      log(1 + nombre de ces marchés)
    qualification   1 compatible, 0 incompatible, 0,5 inconnu (ni l'AO ni elle n'en disent assez)
    activite        log(1 + unités déposées sur les 12 derniers mois)
    anciennete      années depuis sa dernière offre (3 au plus, 3 si jamais)
    region_part     part de ses unités des 2 dernières années dans la région de l'AO
    region_n        log(1 + ses unités des 12 derniers mois dans la région de l'AO)
    famille_part    part de ses unités des 2 dernières années dans la famille de travaux de l'AO
    montant_ecart   |log(estimation de l'AO) − moyenne des log(estimations) de ses marchés|, 3 au plus
    acheteur_famille     marchés de l'acheteur DANS LA FAMILLE de l'AO (24 mois) où elle est présente, lissé :
                         présences / (marchés + 2) — 4 sur 4 donne 0,67, 9 sur 10 donne 0,75
    acheteur_famille_n   log(1 + nombre de ces présences)
    acheteur_24          marchés de l'acheteur toutes familles (24 mois) où elle est présente / (marchés + 2)

Segments (version A2) : une « habituée » de l'acheteur est présente sur au moins 2 de ses marchés de la
même famille en 24 mois ; les autres sociétés forment le second segment. Les habituées ont leur propre
formule (par_segment : mêmes variables, coefficients appris sur leurs seuls couples) et chaque segment sa
table de calibration (calibration_segments) : la formule commune, apprise surtout sur des sociétés qui ne
viennent presque jamais, sous-estimait les habituées (89 % des seguias d'une DPA, notée 28 %).

Les coefficients sont ajustés par `python -m copilote.mesures_probables ajuster` sur des marchés de
l'année d'avant le backtest, et rangés dans probables.json : la formule est fixe, seuls ses poids
viennent des données. Sans ce fichier, des poids écrits à la main (DEFAUT) servent. La formule surestime
les fortes probabilités (une société notée 80 % ne vient qu'une fois sur trois) : une table de calibration
strictement croissante, apprise sur d'autres marchés de la même année, corrige l'échelle (calibrer).

Probabilité d'éviction : (offres écartées + k × taux global) / (offres + k), sur l'historique visible.

Nombre de concurrents : somme de variables de Bernoulli (loi de Poisson-binomiale) sur les candidats,
plus une loi de Poisson pour les sociétés qu'aucun signal n'annonce (λ ajusté de même). Les sociétés ne
sont pas indépendantes : un marché attire plus ou moins de monde à la fois. Un effet commun u ~ N(0, σ²)
s'ajoute donc à la note de chaque société (et à log λ), centré pour ne pas déplacer la moyenne
(u − σ²/2) ; la loi est la moyenne des lois de Poisson-binomiale sur 9 valeurs de u (quadrature de
Gauss-Hermite). σ (« dispersion ») est ajusté sur la même année que les coefficients, pour que la
fourchette à 80 % contienne le vrai nombre 80 % du temps ; σ = 0 redonne la Poisson-binomiale pure.
La même loi est recalculée dans le navigateur (statique/analyse_calcul.js) quand on décoche une
société ou change sa probabilité ; tests/cas_concurrents.json garantit que les deux copies s'accordent.

Historique de l'acheteur (version A2) : le nombre de concurrents de ses marchés de la même famille (3 ans)
corrige le modèle. Avec n tels marchés de nombre médian H et une espérance du modèle M, la cible est
T = exp((1 − w)·log M + w·log H), w = n / (n + k). Le décalage δ de l'effet commun (u = δ + σ'·x − σ'²/2)
est choisi pour que l'espérance de la loi vaille T, et σ' = (1 − w)·σ + w·σ_h : plus l'acheteur a
d'historique, plus la loi se resserre autour de lui. k et σ_h sont ajustés avec les coefficients.
δ et σ' voyagent dans le dossier : le navigateur les garde quand on décoche une société.
"""
from __future__ import annotations

import bisect
import json
import math
import statistics
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import fusions as _fusions
from . import nature, regions
from .bases import cle_acheteur
from .corpus import charger_fiches
from .corpus import jour as jour_ordinal
from .donnees import Unite, charger_unites, rejouer_attribution

FICHIER = Path(__file__).with_name("probables.json")
FICHIER_A = Path(__file__).with_name("probables_A.json")      # version A figée, pour les mesures avant / après
ECARTE = "ecarte_dossier_administratif_technique"
VARIABLES = ("similaires", "similaires_n", "proches_n", "acheteur", "acheteur_n", "qualification", "activite",
             "anciennete", "region_part", "region_n", "famille_part", "montant_ecart",
             "acheteur_famille", "acheteur_famille_n", "acheteur_24")
LIBELLES = {
    "similaires": "présence sur les 50 marchés similaires (pondérée par la note et la récence)",
    "similaires_n": "nombre des 50 marchés similaires où elle est présente",
    "proches_n": "nombre des 300 marchés les plus similaires où elle est présente",
    "acheteur": "part des marchés de l'acheteur (3 ans) où elle est présente",
    "acheteur_n": "nombre de marchés chez l'acheteur (3 ans)",
    "qualification": "qualification (1 compatible, 0 non, 0,5 inconnu)",
    "activite": "activité sur 12 mois",
    "anciennete": "années depuis sa dernière offre",
    "region_part": "part de ses marchés (2 ans) dans la région de l'AO",
    "region_n": "marchés dans la région de l'AO (12 mois)",
    "famille_part": "part de ses marchés (2 ans) dans la même famille de travaux",
    "montant_ecart": "écart entre l'estimation de l'AO et ses montants habituels",
    "acheteur_famille": "présence chez l'acheteur dans la même famille (24 mois, lissée)",
    "acheteur_famille_n": "nombre de ces présences",
    "acheteur_24": "présence chez l'acheteur toutes familles (24 mois, lissée)",
}
SEGMENTS = {"habituee": "habituée de l'acheteur (au moins 2 de ses marchés de la même famille en 24 mois)",
            "autre": "pas habituée de l'acheteur dans cette famille"}

REGLAGES = {
    "seuil_liste": 0.03,        # une société entre dans la liste affichée à partir de cette probabilité
    "taille_liste": 30,         # au plus
    "seuil_envoi": 0.01,        # en dessous, la société n'est pas envoyée : sa probabilité passe dans λ
    "seuil_allies": 0.15,       # on cherche les alliées des sociétés à partir de cette probabilité
    "c_association": 0.6,       # accompagnatrice hors liste : ajoutée « par association » si C atteint ce seuil
    "taille_groupe": 9,         # sociétés au plus par groupe de l'AO
    "proches": 300,             # marchés similaires lus pour proches_n
    "fenetre_acheteur": 1095,   # jours
    "fenetre_activite": 365,    # jours
    "fenetre_profil": 730,      # jours : région, famille, montants habituels
    "fenetre_habituee": 730,    # jours : présence chez l'acheteur (même famille, toutes familles)
    "lissage_acheteur": 2,      # présences / (marchés + lissage)
    "habituee_min": 2,          # présences chez l'acheteur, même famille, pour être « habituée »
    "fenetre_historique": 1095, # jours : nombre de concurrents des marchés de l'acheteur, même famille
    "eviction_k": 5,            # lissage du taux d'éviction vers le taux global
    "niveau": 0.8,              # la fourchette du nombre de concurrents
    "association": True,        # alliées hors liste ajoutées « par association » (reglages.json : false)
    "seuil_groupe_affiche": 0.15,  # la page montre d'abord les groupes dont le chef a au moins cette probabilité
}


def reglages_fichier() -> dict:
    """La section « probables » de reglages.json (sans les clés d'aide) : elle passe avant REGLAGES."""
    from . import reglages as _reglages
    return {k: v for k, v in (_reglages.charger().get("probables") or {}).items() if not k.startswith("_")}

# Poids écrits à la main, utilisés tant que probables.json n'a pas été produit.
DEFAUT = {"constante": -5.0,
          "coefficients": {"similaires": 1.5, "similaires_n": 1.0, "proches_n": 0.3, "acheteur": 3.5,
                           "acheteur_n": 0.4, "qualification": 1.5, "activite": 0.0, "anciennete": -3.5,
                           "region_part": 1.0, "region_n": 0.3, "famille_part": 0.5, "montant_ecart": -0.5},
          "autres": {"constante": 0.2, "par_offre_similaire": 0.3}, "dispersion": 0.8,
          "origine": "poids par défaut (probables.json absent)"}


def charger_coefficients(chemin: Path | None = None) -> dict:
    try:
        with open(chemin or FICHIER, encoding="utf-8") as f:
            c = json.load(f)
    except FileNotFoundError:
        return DEFAUT
    for v in VARIABLES:
        c["coefficients"].setdefault(v, 0.0)
    c.setdefault("autres", DEFAUT["autres"])
    c.setdefault("dispersion", 0.0)
    c.setdefault("version", "A2")
    c.setdefault("groupes", "etoile")
    return c


def empreinte_coefficients(coefs: dict) -> str:
    """Les 12 premiers caractères du SHA-256 des coefficients et des réglages : la « version des réglages »
    d'une prévision figée (suivi.py)."""
    import hashlib
    brut = json.dumps({"c": coefs, "r": {**REGLAGES, **reglages_fichier()}}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(brut.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- l'historique en mémoire

class Memoire:
    """Les unités datées, rangées par date, et des index par société, acheteur, région, famille, code.

    `coupe(limite)` donne le nombre n d'unités visibles : les unités 0..n-1. Toutes les listes d'indices
    sont croissantes, donc « avant la coupure » = bisect, sans relire la base.
    """

    def __init__(self, unites: list[Unite], fiches: dict):
        datees = sorted((u for u in unites if u.annee and u.date), key=lambda u: (u.date, u.identifiant))
        self.unites = datees
        self.dates = [u.date for u in datees]
        self.jours = [jour_ordinal(u.date) for u in datees]
        self.codes: list[frozenset] = []
        self.region: list[str | None] = []
        self.famille: list[str | None] = []
        self.par_societe: dict[str, list[int]] = defaultdict(list)
        self.victoires: dict[str, list[int]] = defaultdict(list)
        self.ecartees: dict[str, list[int]] = defaultdict(list)
        self.par_acheteur: dict[str, list[int]] = defaultdict(list)
        self.par_region: dict[str, list[int]] = defaultdict(list)
        self.par_societe_region: dict[tuple, list[int]] = defaultdict(list)
        self.par_societe_famille: dict[tuple, list[int]] = defaultdict(list)
        self.par_societe_code: dict[tuple, list[int]] = defaultdict(list)
        self.avec_codes: dict[str, list[int]] = defaultdict(list)     # unités de la société où le marché a des codes
        self.log_estimation: dict[str, list[tuple[int, float]]] = defaultdict(list)
        self.presents: list[frozenset] = []
        self.noms: dict[str, str] = {}
        self.offres_cumul = [0]                 # offres (une par société et par unité) des unités < i
        self.ecartees_cumul = [0]
        for i, u in enumerate(datees):
            f = fiches.get(u.identifiant)
            codes = frozenset(f.quals) if f else frozenset()
            reg = regions.region_de(u.acheteur)
            fam = nature.classer(f.texte)[0] if f else None
            self.codes.append(codes)
            self.region.append(reg)
            self.famille.append(fam)
            vus: dict[str, str] = {}
            for c in u.concurrents:
                k = c["cle"]
                if k and k not in vus:
                    vus[k] = c["statut"]
                    self.noms[k] = c["nom"]
            for k, statut in vus.items():
                self.par_societe[k].append(i)
                if statut == ECARTE:
                    self.ecartees[k].append(i)
                if reg:
                    self.par_societe_region[(k, reg)].append(i)
                if fam:
                    self.par_societe_famille[(k, fam)].append(i)
                if codes:
                    self.avec_codes[k].append(i)
                    for code in codes:
                        self.par_societe_code[(k, code)].append(i)
                if u.estimation:
                    self.log_estimation[k].append((i, math.log(u.estimation)))
            if u.cle_gagnant:
                self.victoires[u.cle_gagnant].append(i)
            self.par_acheteur[cle_acheteur(u.acheteur)].append(i)
            if reg:
                self.par_region[reg].append(i)
            self.presents.append(frozenset(vus))
            self.offres_cumul.append(self.offres_cumul[-1] + len(vus))
            self.ecartees_cumul.append(self.ecartees_cumul[-1] + sum(1 for s in vus.values() if s == ECARTE))
        # Sommes cumulées des log(estimation) par société : la moyenne d'une fenêtre se lit en O(1).
        self.log_idx: dict[str, list[int]] = {}
        self.log_cumul: dict[str, list[float]] = {}
        for k, lignes in self.log_estimation.items():
            self.log_idx[k] = [i for i, _ in lignes]
            cumul = [0.0]
            for _, v in lignes:
                cumul.append(cumul[-1] + v)
            self.log_cumul[k] = cumul
        del self.log_estimation
        self._attributions: dict[int, object] = {}

    def coupe(self, limite: str | None) -> int:
        """Nombre d'unités visibles : celles datées au plus tard le jour `limite` (AAAAMMJJ, inclus)."""
        return len(self.unites) if limite is None else bisect.bisect_right(self.dates, limite)

    @staticmethod
    def avant(indices: list[int], n: int) -> list[int]:
        return indices[:bisect.bisect_left(indices, n)]

    @staticmethod
    def compte(indices: list[int], n: int, depuis: int = 0) -> int:
        return bisect.bisect_left(indices, n) - (bisect.bisect_left(indices, depuis) if depuis else 0)

    def indice_du_jour(self, jour: int) -> int:
        """Premier indice dont le jour est ≥ `jour` (ordinal)."""
        return bisect.bisect_left(self.jours, jour)

    def nom(self, cle: str) -> str:
        return self.noms.get(cle, cle)

    def moyenne_log_estimation(self, cle: str, depuis: int, n: int) -> float | None:
        idx = self.log_idx.get(cle)
        if not idx:
            return None
        a, b = bisect.bisect_left(idx, depuis), bisect.bisect_left(idx, n)
        if b <= a:
            a = 0
        if b <= a:
            return None
        cumul = self.log_cumul[cle]
        return (cumul[b] - cumul[a]) / (b - a)

    def attribution(self, u: Unite):
        """L'ouverture des plis rejouée (construire_base), gardée : même interface que groupes.Profils."""
        a = self._attributions.get(id(u))
        if a is None:
            a = self._attributions[id(u)] = rejouer_attribution(u)
        return a

    def taux_reference(self, u: Unite) -> float | None:
        a = self.attribution(u)
        return a.reference / u.estimation if a.reference and u.estimation else None


_memo: dict = {}
_verrou = threading.Lock()


def construire_memoire(db, fusions: dict[str, str] | None = None) -> Memoire:
    unites = charger_unites(db, fusions=fusions)
    return Memoire(unites, charger_fiches(db, unites))


def memoire(ouvrir_historique, chemin) -> Memoire:
    """La mémoire de l'historique actuel, refaite seulement quand pv_travaux.db ou les fusions changent."""
    fusions = _fusions.charger()
    try:
        st = Path(chemin).stat()
        cle = (str(chemin), st.st_mtime_ns, st.st_size, tuple(sorted(fusions.items())))
    except OSError:
        cle = (time.monotonic(),)
    with _verrou:
        if _memo.get("cle") != cle:
            db = ouvrir_historique()
            try:
                m = construire_memoire(db, fusions)
            finally:
                db.close()
            _memo.clear()
            _memo.update(cle=cle, valeur=m)
        return _memo["valeur"]


def oublier() -> None:
    with _verrou:
        _memo.clear()


# --------------------------------------------------------------------------- le contexte d'un AO

@dataclass
class Similaire:
    unite: Unite
    note: float                 # sur 100
    recence: float              # entre 0 et 1

    @property
    def poids(self) -> float:
        return max(self.note, 0.0) / 100.0 * self.recence


@dataclass
class Contexte:
    n: int                                  # unités visibles (Memoire.coupe)
    jour: int                               # ordinal du jour de référence (le lendemain de la coupure)
    acheteur_cles: frozenset
    codes: frozenset                        # codes de qualification de l'AO (B.1…)
    similaires: list[Similaire] = field(default_factory=list)      # les 50 premiers
    proches: list[Unite] = field(default_factory=list)            # les 300 premiers
    region: str | None = None
    famille: str | None = None
    estimation: float | None = None


def _qualification(mem: Memoire, cle: str, n: int, codes: frozenset) -> float:
    if not codes:
        return 0.5
    for code in codes:
        if Memoire.compte(mem.par_societe_code.get((cle, code), []), n):
            return 1.0
    return 0.0 if Memoire.compte(mem.avec_codes.get(cle, []), n) else 0.5


def acheteur_unites(mem: Memoire, ctx: Contexte, fenetre: int | None = None) -> list[int]:
    """Les unités visibles de l'acheteur (ses clés rapprochées) sur la fenêtre (3 ans par défaut)."""
    debut = mem.indice_du_jour(ctx.jour - (fenetre or REGLAGES["fenetre_acheteur"]))
    sortie: set[int] = set()
    for a in ctx.acheteur_cles:
        idx = mem.par_acheteur.get(a, [])
        sortie.update(idx[bisect.bisect_left(idx, debut):bisect.bisect_left(idx, ctx.n)])
    return sorted(sortie)


class _Fenetres:
    def __init__(self, mem: Memoire, ctx: Contexte):
        self.act = mem.indice_du_jour(ctx.jour - REGLAGES["fenetre_activite"])
        self.profil = mem.indice_du_jour(ctx.jour - REGLAGES["fenetre_profil"])
        self.total_sim = sum(s.poids for s in ctx.similaires) or 1.0
        self.log_ao = math.log(ctx.estimation) if ctx.estimation else None


def variables(mem: Memoire, ctx: Contexte, cle: str, poids_sim: float, n_sim: int, n_proches: int, chez: int,
              n_ach: int, fen: _Fenetres | None = None, extra: dict | None = None) -> dict:
    """Les variables d'une société, et les comptes bruts qui les expliquent. `extra` : ses présences chez
    l'acheteur sur 24 mois (même famille, toutes familles) et ses victoires parmi les marchés proches."""
    fen = fen or _Fenetres(mem, ctx)
    e = extra or {}
    lissage = REGLAGES["lissage_acheteur"]
    chez_fam, n_fam = e.get("chez_famille", 0), e.get("n_famille", 0)
    chez24, n24 = e.get("chez_24", 0), e.get("n_24", 0)
    n = ctx.n
    mes = mem.par_societe.get(cle, [])
    k_n = bisect.bisect_left(mes, n)
    actives = k_n - bisect.bisect_left(mes, fen.act)
    profil = k_n - bisect.bisect_left(mes, fen.profil)
    annees = min(3.0, max(0, ctx.jour - mem.jours[mes[k_n - 1]]) / 365.0) if k_n else 3.0
    q = _qualification(mem, cle, n, ctx.codes)
    reg = mem.par_societe_region.get((cle, ctx.region), []) if ctx.region else []
    en_region = Memoire.compte(reg, n, fen.profil)
    region_12 = Memoire.compte(reg, n, fen.act)
    fam = mem.par_societe_famille.get((cle, ctx.famille), []) if ctx.famille else []
    en_famille = Memoire.compte(fam, n, fen.profil)
    moy = mem.moyenne_log_estimation(cle, fen.profil, n)
    ecart = min(3.0, abs(fen.log_ao - moy)) if fen.log_ao is not None and moy is not None else 1.0
    return {
        "x": {"similaires": poids_sim / fen.total_sim, "similaires_n": math.log1p(n_sim),
              "proches_n": math.log1p(n_proches),
              "acheteur": chez / (n_ach + 1), "acheteur_n": math.log1p(chez),
              "qualification": q, "activite": math.log1p(actives), "anciennete": annees,
              "region_part": en_region / profil if profil else 0.0, "region_n": math.log1p(region_12),
              "famille_part": en_famille / profil if profil else 0.0, "montant_ecart": ecart,
              "acheteur_famille": chez_fam / (n_fam + lissage), "acheteur_famille_n": math.log1p(chez_fam),
              "acheteur_24": chez24 / (n24 + lissage)},
        "brut": {"similaires": n_sim, "similaires_total": len(ctx.similaires), "proches": n_proches,
                 "proches_total": len(ctx.proches),
                 "acheteur": chez, "acheteur_total": n_ach, "unites_12_mois": actives, "unites": k_n,
                 "unites_2_ans": profil, "region_2_ans": en_region, "region_12_mois": region_12,
                 "famille_2_ans": en_famille,
                 "montant_habituel": round(math.exp(moy)) if moy is not None else None,
                 "derniere": mem.dates[mes[k_n - 1]] if k_n else None,
                 "acheteur_famille": chez_fam, "acheteur_famille_total": n_fam,
                 "acheteur_24": chez24, "acheteur_24_total": n24,
                 "victoires_proches": e.get("victoires_proches", 0)},
    }


def segment(x: dict) -> str:
    """« habituee » : présente sur au moins `habituee_min` marchés de l'acheteur de la même famille (24 mois)."""
    return "habituee" if x.get("acheteur_famille_n", 0.0) >= math.log1p(REGLAGES["habituee_min"]) - 1e-9 else "autre"


def _logistique(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z)) if z > -60 else 0.0


def calibrer(p: float, calibration: dict | None) -> float:
    """La probabilité de la formule, corrigée par la table de calibration (probables.json) : sur les marchés
    tenus à part de l'ajustement, combien de sociétés notées ainsi sont réellement venues. La table est
    strictement croissante : le classement des sociétés ne change pas, seule l'échelle est corrigée."""
    if not calibration:
        return p
    xs, ys = calibration["x"], calibration["y"]
    if p <= xs[0]:
        return ys[0] * p / xs[0] if xs[0] > 0 else ys[0]
    if p >= xs[-1]:
        return ys[-1] + (p - xs[-1]) * (1 - ys[-1]) / (1 - xs[-1]) if xs[-1] < 1 else ys[-1]
    k = bisect.bisect_right(xs, p)
    x0, x1, y0, y1 = xs[k - 1], xs[k], ys[k - 1], ys[k]
    return y0 + (p - x0) * (y1 - y0) / (x1 - x0)


def table_calibration(x: dict, coefs: dict) -> dict | None:
    """La table du segment de la société (version A2), sinon la table unique (version A)."""
    par_segment = coefs.get("calibration_segments")
    if par_segment:
        return par_segment.get(segment(x)) or coefs.get("calibration")
    return coefs.get("calibration")


def formule_de(x: dict, coefs: dict) -> dict:
    """La formule qui s'applique à la société : celle de son segment si elle existe (par_segment, version A2 :
    les habituées ont leurs propres coefficients), sinon la formule commune."""
    propre = (coefs.get("par_segment") or {}).get(segment(x))
    return propre or coefs


def probabilite(x: dict, coefs: dict) -> tuple[float, dict, float]:
    """(p, contribution de chaque variable bk·xk à la note z, p de la formule avant calibration)."""
    f = formule_de(x, coefs)
    contrib = {v: f["coefficients"].get(v, 0.0) * x.get(v, 0.0) for v in VARIABLES}
    z = f["constante"] + sum(contrib.values())
    brute = _logistique(z)
    return calibrer(brute, table_calibration(x, coefs)), contrib, brute


def eviction(mem: Memoire, cle: str, n: int) -> dict:
    offres = Memoire.compte(mem.par_societe.get(cle, []), n)
    ecartees = Memoire.compte(mem.ecartees.get(cle, []), n)
    g = mem.ecartees_cumul[n] / mem.offres_cumul[n] if mem.offres_cumul[n] else 0.0
    k = REGLAGES["eviction_k"]
    return {"p": (ecartees + k * g) / (offres + k), "ecartees": ecartees, "offres": offres, "global": g}


def _cles(u: Unite) -> set[str]:
    return {c["cle"] for c in u.concurrents if c["cle"]}


def signaux(mem: Memoire, ctx: Contexte) -> list[dict]:
    """Les candidats et leurs variables, sans probabilité (sert aussi à l'ajustement des coefficients)."""
    poids_sim: Counter = Counter()
    n_sim: Counter = Counter()
    for s in ctx.similaires:
        for k in _cles(s.unite):
            poids_sim[k] += s.poids
            n_sim[k] += 1
    n_proches: Counter = Counter()
    v_proches: Counter = Counter()
    for u in ctx.proches:
        n_proches.update(_cles(u))
        if u.cle_gagnant:
            v_proches[u.cle_gagnant] += 1
    ach = acheteur_unites(mem, ctx)
    chez: Counter = Counter()
    for i in ach:
        chez.update(mem.presents[i])
    ach24 = acheteur_unites(mem, ctx, REGLAGES["fenetre_habituee"])
    fam24 = [i for i in ach24 if ctx.famille and mem.famille[i] == ctx.famille]
    chez24: Counter = Counter()
    for i in ach24:
        chez24.update(mem.presents[i])
    chez_fam: Counter = Counter()
    for i in fam24:
        chez_fam.update(mem.presents[i])
    en_region: set[str] = set()
    if ctx.region:
        fen = mem.indice_du_jour(ctx.jour - REGLAGES["fenetre_activite"])
        idx = mem.par_region.get(ctx.region, [])
        for i in idx[bisect.bisect_left(idx, fen):bisect.bisect_left(idx, ctx.n)]:
            en_region |= mem.presents[i]
    fen = _Fenetres(mem, ctx)
    sortie = []
    for cle in sorted(set(n_sim) | set(n_proches) | set(chez) | en_region):
        v = variables(mem, ctx, cle, poids_sim[cle], n_sim[cle], n_proches[cle], chez[cle], len(ach), fen,
                      {"chez_famille": chez_fam[cle], "n_famille": len(fam24), "chez_24": chez24[cle],
                       "n_24": len(ach24), "victoires_proches": v_proches[cle]})
        if not n_sim[cle] and v["x"]["qualification"] == 0.0:
            continue                    # rien dans les 50 similaires, et sa qualification ne convient pas
        sortie.append({"cle": cle, **v})
    return sortie


def candidats(mem: Memoire, ctx: Contexte, coefs: dict | None = None) -> list[dict]:
    """Toutes les sociétés candidates, triées par probabilité décroissante."""
    coefs = coefs or charger_coefficients()
    sortie = []
    for s in signaux(mem, ctx):
        p, contrib, brute = probabilite(s["x"], coefs)
        sortie.append({"cle": s["cle"], "p": p, "p_formule": brute, "x": s["x"], "brut": s["brut"],
                       "contributions": contrib, "constante": formule_de(s["x"], coefs)["constante"]})
    sortie.sort(key=lambda c: (-c["p"], c["cle"]))
    return sortie


def offres_moyennes_similaires(ctx: Contexte) -> float:
    if not ctx.similaires:
        return 0.0
    return sum(len(_cles(s.unite)) for s in ctx.similaires) / len(ctx.similaires)


def lambda_autres(ctx: Contexte, coefs: dict) -> float:
    """Sociétés attendues que rien n'annonce : une constante plus une part du nombre moyen d'offres
    des marchés similaires (un AO dont les semblables attirent beaucoup de monde en attire d'imprévues)."""
    a = coefs.get("autres", DEFAUT["autres"])
    return max(0.0, a["constante"] + a["par_offre_similaire"] * offres_moyennes_similaires(ctx))


# --------------------------------------------------------------------------- le nombre de concurrents

# Quadrature de Gauss-Hermite à 9 points (loi normale centrée réduite), poids normalisés. Recopiés dans
# statique/analyse_calcul.js.
HERMITE_X = (-4.5127458634, -3.205429002856, -2.076847978678, -1.023255663789, 0.0,
             1.023255663789, 2.076847978678, 3.205429002856, 4.5127458634)
HERMITE_W = (2.234584401e-05, 0.00278914132123, 0.04991640676522, 0.24409750289494, 0.40634920634921,
             0.24409750289494, 0.04991640676522, 0.00278914132123, 2.234584401e-05)


def poisson_binomiale(ps: list[float], lam: float = 0.0, epsilon: float = 1e-9) -> list[float]:
    """Poisson-binomiale sur `ps` (indépendantes), convoluée avec une Poisson(λ). Queue sous epsilon coupée."""
    d = [1.0]
    for p in ps:
        p = min(1.0, max(0.0, float(p)))
        nd = [0.0] * (len(d) + 1)
        for k, v in enumerate(d):
            nd[k] += v * (1 - p)
            nd[k + 1] += v * p
        d = nd
    if lam > 0:
        poisson, k, terme, cumul = [], 0, math.exp(-lam), 0.0
        while True:
            poisson.append(terme)
            cumul += terme
            if cumul >= 1 - epsilon or k > 500:
                break
            k += 1
            terme = terme * lam / k
        nd = [0.0] * (len(d) + len(poisson) - 1)
        for i, a in enumerate(d):
            for j, b in enumerate(poisson):
                nd[i + j] += a * b
        d = nd
    while len(d) > 1 and d[-1] < epsilon:
        d.pop()
    return d


def _notes(ps: list[float]) -> list[float]:
    sortie = []
    for p in ps:
        p = min(1 - 1e-12, max(1e-12, float(p)))
        sortie.append(math.log(p / (1 - p)))
    return sortie


def distribution(ps: list[float], lam: float = 0.0, dispersion: float = 0.0, decalage: float = 0.0) -> list[float]:
    """Loi du nombre de sociétés venues : Poisson-binomiale à effet commun (voir l'en-tête du module).
    `decalage` δ déplace l'effet commun : u = δ + σ·x − σ²/2.

    Recopiée À L'IDENTIQUE dans statique/analyse_calcul.js (distribution) ; tests/cas_concurrents.json.
    """
    if dispersion <= 0 and not decalage:
        return poisson_binomiale(ps, lam)
    s = max(0.0, float(dispersion))
    notes = _notes(ps)
    total: list[float] = []
    for x, w in zip(HERMITE_X, HERMITE_W):
        u = decalage + s * x - s * s / 2
        d = poisson_binomiale([1 / (1 + math.exp(-(z + u))) for z in notes], lam * math.exp(u))
        if len(d) > len(total):
            total.extend([0.0] * (len(d) - len(total)))
        for k, v in enumerate(d):
            total[k] += w * v
    while len(total) > 1 and total[-1] < 1e-9:
        total.pop()
    return total


def esperance(ps: list[float], lam: float = 0.0, dispersion: float = 0.0, decalage: float = 0.0) -> float:
    """L'espérance de distribution(ps, lam, dispersion, decalage), sans la convolution."""
    s = max(0.0, float(dispersion))
    notes = _notes(ps)
    total = 0.0
    for x, w in zip(HERMITE_X, HERMITE_W):
        u = decalage + s * x - s * s / 2
        total += w * (sum(1 / (1 + math.exp(-(z + u))) for z in notes) + lam * math.exp(u))
    return total


def historique_acheteur(mem: Memoire, ctx: Contexte) -> list[int]:
    """Le nombre de sociétés de chaque marché visible de l'acheteur, dans la famille de l'AO (3 ans)."""
    if not ctx.famille:
        return []
    return [len(mem.presents[i]) for i in acheteur_unites(mem, ctx, REGLAGES["fenetre_historique"])
            if mem.famille[i] == ctx.famille and mem.presents[i]]


def corriger_par_historique(ps: list[float], lam: float, dispersion: float, nombres: list[int],
                            reglage: dict | None) -> dict:
    """δ et σ' (voir l'en-tête) : l'espérance de la loi est ramenée vers le nombre médian de concurrents
    des marchés de l'acheteur de la même famille, d'autant plus qu'il en a."""
    m0 = esperance(ps, lam, dispersion)
    sortie = {"n": len(nombres), "mediane": float(statistics.median(nombres)) if nombres else None,
              "poids": 0.0, "esperance_modele": m0, "cible": m0, "decalage": 0.0, "dispersion": dispersion,
              "derniers": nombres[-12:]}
    if not reglage or not nombres or m0 <= 0:
        return sortie
    w = len(nombres) / (len(nombres) + float(reglage["k"]))
    h = max(1.0, sortie["mediane"])
    cible = math.exp((1 - w) * math.log(m0) + w * math.log(h))
    s2 = (1 - w) * dispersion + w * float(reglage["dispersion"])
    bas, haut = -6.0, 6.0
    for _ in range(50):                       # l'espérance croît avec δ : dichotomie
        mil = (bas + haut) / 2
        if esperance(ps, lam, s2, mil) < cible:
            bas = mil
        else:
            haut = mil
    sortie.update(poids=w, cible=cible, decalage=round((bas + haut) / 2, 6), dispersion=round(s2, 6))
    return sortie


def fourchette(d: list[float], niveau: float = 0.8) -> dict:
    """[bas, haut] : bas = premier k où la loi cumulée atteint (1 − niveau)/2, haut = premier k où elle
    atteint 1 − (1 − niveau)/2 ; « probable » = la valeur la plus probable ; « moyenne » = l'espérance ;
    « couverte » = la probabilité réelle de [bas, haut] (≥ niveau, la loi est discrète).
    Recopiée dans statique/analyse_calcul.js (fourchette)."""
    bas_q, haut_q = (1 - niveau) / 2, 1 - (1 - niveau) / 2
    bas = haut = None
    cumul = 0.0
    for k, v in enumerate(d):
        cumul += v
        if bas is None and cumul >= bas_q - 1e-12:
            bas = k
        if haut is None and cumul >= haut_q - 1e-12:
            haut = k
            break
    if haut is None:
        haut = len(d) - 1
    if bas is None:
        bas = haut
    probable = 0
    for k, v in enumerate(d):
        if v > d[probable]:
            probable = k
    moyenne = sum(k * v for k, v in enumerate(d))
    return {"bas": bas, "haut": haut, "probable": probable, "moyenne": moyenne,
            "couverte": sum(d[bas:haut + 1]), "niveau": niveau}


# --------------------------------------------------------------------------- tout ensemble

def analyser(mem: Memoire, ctx: Contexte, coefs: dict | None = None, reglages: dict | None = None,
             avec_groupes: bool = True) -> dict:
    """Candidats, liste affichée, groupes (groupes_ao.py) et nombre de concurrents."""
    from . import groupes_ao
    coefs = coefs or charger_coefficients()
    # Les réglages propres à une version de coefficients (A2 : seuil_allies 0,07) passent avant les défauts.
    r = {**REGLAGES, **reglages_fichier(), **(coefs.get("reglages") or {}), **(reglages or {})}
    tous = candidats(mem, ctx, coefs)
    for c in tous:
        c["dans_liste"] = False
        c["association"] = None
    for c in [c for c in tous if c["p"] >= r["seuil_liste"]][:int(r["taille_liste"])]:
        c["dans_liste"] = True
    if not avec_groupes:
        g = {"groupes": [], "individuelles": [], "ajoutees": []}
    elif coefs.get("groupes") == "union":
        g = groupes_ao.former_union(mem, ctx.n, tous, r)           # version A, pour les mesures avant / après
    else:
        g = groupes_ao.former(mem, ctx.n, tous, r)
    tous = tous + g["ajoutees"]
    tous.sort(key=lambda c: (-c["p"], c["cle"]))
    envoyes = [c for c in tous if c["p"] >= r["seuil_envoi"] or c["dans_liste"] or c["association"]]
    garde = {c["cle"] for c in envoyes}
    reste = sum(c["p"] for c in tous if c["cle"] not in garde)
    for c in envoyes:
        c["nom"] = mem.nom(c["cle"])
        c.setdefault("eviction", eviction(mem, c["cle"], ctx.n))
    autres = lambda_autres(ctx, coefs)
    ps = [c["p"] for c in envoyes]
    lam = autres + reste
    h = corriger_par_historique(ps, lam, coefs.get("dispersion", 0.0), historique_acheteur(mem, ctx),
                                coefs.get("historique"))
    d = distribution(ps, lam, h["dispersion"], h["decalage"])
    for c in envoyes:
        c["segment"] = segment(c["x"]) if c.get("x") else None
    return {"candidats": envoyes, "nb_candidats": len(tous), "groupes": g["groupes"],
            "individuelles": g["individuelles"],
            "concurrents": {"distribution": d, "lambda": lam, "lambda_autres": autres,
                            "lambda_reste": reste, "dispersion": h["dispersion"],
                            "dispersion_modele": coefs.get("dispersion", 0.0), "decalage": h["decalage"],
                            "historique": h,
                            "offres_moyennes_similaires": offres_moyennes_similaires(ctx),
                            **fourchette(d, r["niveau"])},
            "reglages": r, "coefficients": coefs}
