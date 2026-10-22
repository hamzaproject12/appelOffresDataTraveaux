"""Moteur de marchés similaires : filtre, puis note sur 100 par critère.

Pour un AO (ou un marché du passé pris pour cible, au backtest), le moteur lit les marchés du corpus
datés STRICTEMENT avant le jour de la cible, écarte ceux qui ne sont pas comparables (qualification
incompatible, montant hors de 0,3× à 3×), puis note les autres sur 100 :

    objet et nature   40   cosinus TF-IDF de l'objet, famille et nature des travaux
    acheteur          30   même acheteur (ou rapproché), sinon même type d'acheteur
    qualification     10   codes de qualification en commun, puis proximité de classe
    montant           10   proximité en échelle logarithmique
    région             5   même région
    récence            5   décroissance en demi-vie

Les poids viennent de reglages.json (ou du paramètre `poids` de l'API). Chaque critère vaut entre 0 et 1 ;
la note est 100 × Σ poids·critère / Σ poids, et le détail renvoie les points de chaque critère. Le même code
note les AO réels et le backtest (mesures_similarite.py) : ce qu'on mesure est ce qu'on livre.
"""
from __future__ import annotations

import math
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np

from . import corpus as _corpus
from . import index_texte, nature, qualifs, regions, reglages
from .acheteurs import type_acheteur
from .bases import cle_acheteur

CRITERES = reglages.CRITERES
LIBELLES = {"objet_nature": "Objet et nature", "acheteur": "Acheteur", "qualification": "Qualification et classe",
            "montant": "Montant", "region": "Région", "recence": "Récence"}
_PROXIMITE_CLASSE = np.array([1.0, 0.6, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])


@dataclass
class Cible:
    """Ce qu'on cherche à rapprocher de l'historique : un AO (ou un lot), ou un marché du passé."""
    jour: int                                   # ordinal du jour de référence : seuls les marchés antérieurs comptent
    texte_index: str
    objet: str = ""
    acheteur: str = ""
    acheteur_cles: frozenset = frozenset()      # clés qui valent « même acheteur » (la sienne + correspondances)
    estimation: float | None = None
    quals: dict = field(default_factory=dict)   # {code: classe}
    region: str | None = None
    famille: str | None = None
    nature: str | None = None
    ref: str = ""
    lot: str | None = None

    @property
    def type_acheteur(self) -> str:
        return type_acheteur(self.acheteur)


def cible_depuis_texte(objet: str, acheteur: str, estimation: float | None, quals: dict, jour: int,
                       acheteur_cles=(), lieu: str | None = None, ref: str = "", lot: str | None = None) -> Cible:
    fam, nat = nature.classer(objet)
    cles = set(acheteur_cles) | ({cle_acheteur(acheteur)} if acheteur else set())
    return Cible(jour=jour, texte_index=_corpus.texte_index(objet), objet=objet, acheteur=acheteur,
                 acheteur_cles=frozenset(cles), estimation=estimation or None, quals=dict(quals),
                 region=regions.region_de(acheteur, lieu), famille=fam, nature=nat, ref=ref, lot=lot)


@dataclass
class Composantes:
    k: int                                      # nombre de marchés lus (les antérieurs)
    valeurs: dict[str, np.ndarray]              # critère -> valeur entre 0 et 1, par marché
    parties: dict[str, np.ndarray]              # texte / famille / nature (-1 si inconnu)
    masque: np.ndarray                          # True = comparable (passe le filtre)
    ratio: np.ndarray                           # montant du marché / montant de la cible
    cos: np.ndarray
    inutiles: frozenset = frozenset()           # critères sans information côté cible (poids ramené à 0)


class Moteur:
    """Un corpus et son index, avec les tableaux numpy utiles au calcul."""

    def __init__(self, corpus: _corpus.Corpus, index: index_texte.Index, calcul: str = "courant",
                 origine: str = "calcule"):
        self.corpus, self.index, self.calcul, self.origine = corpus, index, calcul, origine
        c = corpus
        self.familles = sorted({f for f in c.famille if f})
        self.natures = sorted({n for n in c.nature if n})
        self.regions_ = sorted({r for r in c.region if r})
        self.acheteurs = {a: i for i, a in enumerate(sorted(set(c.acheteur_cle)))}
        self.types = {t: i for i, t in enumerate(sorted(set(c.acheteur_type)))}
        fi = {f: i for i, f in enumerate(self.familles)}
        ni = {n: i for i, n in enumerate(self.natures)}
        ri = {r: i for i, r in enumerate(self.regions_)}
        self.fam_i = np.array([fi.get(f, -1) for f in c.famille], dtype=np.int16)
        self.nat_i = np.array([ni.get(n, -1) for n in c.nature], dtype=np.int16)
        self.reg_i = np.array([ri.get(r, -1) for r in c.region], dtype=np.int16)
        self.ach_i = np.array([self.acheteurs[a] for a in c.acheteur_cle], dtype=np.int32)
        self.typ_i = np.array([-1 if t == "autre" else self.types[t] for t in c.acheteur_type], dtype=np.int16)
        self.position = {code: k for k, code in enumerate(c.codes)}
        self.a_des_quals = (c.Q > 0).any(axis=1)
        self._fi, self._ni, self._ri = fi, ni, ri

    # ------------------------------------------------------------------ calcul des critères
    def composantes(self, cible: Cible, r: dict, cos: np.ndarray | None = None) -> Composantes:
        c = self.corpus
        k = c.avant(cible.jour)
        if k == 0:
            vide = np.zeros(0)
            return Composantes(0, {n: vide for n in CRITERES}, {}, np.zeros(0, bool), vide, vide,
                               _inutiles(cible))
        if cos is None or len(cos) < k:
            cos = self.index.similarites(cible.texte_index, k)
        cos = cos[:k]

        # objet et nature : le texte compte toujours ; famille et nature seulement si la cible les a
        parties = {"texte": cos}
        sous = r["objet_nature"]
        total, acc = sous["texte"], sous["texte"] * cos
        fam = np.full(k, -1.0)
        if cible.famille is not None:
            fam = np.where(self.fam_i[:k] < 0, -1.0, (self.fam_i[:k] == self._fi.get(cible.famille, -2)) * 1.0)
            acc = acc + sous["famille"] * np.maximum(fam, 0)
            total += sous["famille"]
        nat = np.full(k, -1.0)
        if cible.nature is not None:
            nat = np.where(self.nat_i[:k] < 0, -1.0, (self.nat_i[:k] == self._ni.get(cible.nature, -2)) * 1.0)
            acc = acc + sous["nature"] * np.maximum(nat, 0)
            total += sous["nature"]
        parties["famille"], parties["nature"] = fam, nat
        objet_nature = acc / total

        # acheteur
        cles = [self.acheteurs[a] for a in cible.acheteur_cles if a in self.acheteurs]
        meme = np.isin(self.ach_i[:k], cles) if cles else np.zeros(k, bool)
        type_cible = self.types.get(cible.type_acheteur) if cible.type_acheteur != "autre" else None
        meme_type = (self.typ_i[:k] == type_cible) if type_cible is not None else np.zeros(k, bool)
        acheteur = np.where(meme, 1.0, np.where(meme_type, r["acheteur"]["meme_type"], 0.0))

        # qualification : par code, puis proximité de classe
        qualification = np.zeros(k)
        compatible = np.ones(k, bool)
        if cible.quals:
            Q = c.Q[:k]
            a_des = self.a_des_quals[:k]
            colonnes = [(self.position[code], cl) for code, cl in cible.quals.items() if code in self.position]
            if colonnes:
                proches = np.zeros((k, len(colonnes)))
                for j, (col, cl) in enumerate(colonnes):
                    q = Q[:, col].astype(np.int16)
                    if cl is None:
                        prox = np.where(q > 0, 0.5, 0.0)
                    else:
                        prox = np.where(q == 0, 0.0, np.where(q >= _corpus.CLASSE_INCONNUE, 0.5,
                                                             _PROXIMITE_CLASSE[np.minimum(np.abs(q - cl), 9)]))
                    proches[:, j] = prox
                qualification = proches.sum(axis=1) / len(cible.quals)
                en_commun = np.any([Q[:, col] > 0 for col, _ in colonnes], axis=0)
            else:
                en_commun = np.zeros(k, bool)
            mode = r["filtre"]["qualification"]
            if mode == "code":
                compatible = ~a_des | en_commun
            elif mode == "domaine":
                lettres = {code[0] for code in cible.quals}
                cols = [self.position[code] for code in c.codes if code[0] in lettres]
                compatible = ~a_des | ((Q[:, cols] > 0).any(axis=1) if cols else np.zeros(k, bool))
            qualification = np.where(a_des, qualification, r["qualification"]["inconnue"])

        # montant
        ratio = np.ones(k)
        montant = np.zeros(k)
        masque = compatible
        if cible.estimation:
            ratio = c.estimation[:k] / cible.estimation
            echelle = math.log(max(r["filtre"]["montant_max"], 1 / r["filtre"]["montant_min"]))
            montant = np.clip(1 - np.abs(np.log(ratio)) / echelle, 0, 1)
            masque = masque & (ratio >= r["filtre"]["montant_min"]) & (ratio <= r["filtre"]["montant_max"])

        # région et récence
        reg = self._ri.get(cible.region, -2) if cible.region else -2
        region = (self.reg_i[:k] == reg) * 1.0
        age = np.maximum(cible.jour - c.jour[:k], 0)
        recence = 0.5 ** (age / r["recence"]["demi_vie_jours"])

        valeurs = {"objet_nature": objet_nature, "acheteur": acheteur, "qualification": qualification,
                   "montant": montant, "region": region, "recence": recence}
        return Composantes(k, valeurs, parties, masque, ratio, cos, _inutiles(cible))

    @staticmethod
    def noter(comp: Composantes, poids: dict[str, float]) -> np.ndarray:
        """Note sur 100 de chaque marché lu ; -1 pour ceux que le filtre écarte.

        Un critère sans information côté cible (pas de qualification, d'estimation ou de région) sort du
        calcul : sinon il vaudrait 0 pour tous et plafonnerait la note. Le classement n'en change pas.
        """
        poids = poids_utiles(comp, poids)
        somme = sum(poids.values())
        note = sum(poids[n] * comp.valeurs[n] for n in CRITERES) / somme * 100.0
        return np.where(comp.masque, note, -1.0)

    def meilleurs(self, notes: np.ndarray, n: int) -> np.ndarray:
        """Indices des `n` meilleures notes (écartés exclus), à égalité le plus récent d'abord."""
        ok = np.flatnonzero(notes >= 0)
        if len(ok) == 0:
            return ok
        ordre = np.lexsort((-ok, -self.corpus.jour[ok], -np.round(notes[ok], 9)))
        return ok[ordre[:n]]

    # ------------------------------------------------------------------ sortie lisible
    def detail(self, cible: Cible, comp: Composantes, i: int, poids: dict[str, float]) -> dict:
        c = self.corpus
        utiles = poids_utiles(comp, poids)
        somme = sum(utiles.values())
        d = {}
        for n in CRITERES:
            v = float(comp.valeurs[n][i])
            d[n] = {"libelle": LIBELLES[n], "poids": poids[n], "poids_effectif": round(utiles[n] / somme * 100.0, 2),
                    "ignore": n in comp.inutiles, "valeur": round(v, 3),
                    "points": round(utiles[n] * v / somme * 100.0, 2), "explication": ""}
        # objet et nature
        p = comp.parties
        morceaux = [f"texte {p['texte'][i]:.0%}"]
        if cible.famille is not None:
            if p["famille"][i] < 0:
                morceaux.append("famille inconnue pour ce marché")
            elif p["famille"][i] > 0:
                morceaux.append(f"même famille ({nature.libelle(c.famille[i])})")
            else:
                morceaux.append(f"famille différente ({nature.libelle(c.famille[i])} ≠ {nature.libelle(cible.famille)})")
        if cible.nature is not None:
            if p["nature"][i] < 0:
                morceaux.append("nature inconnue pour ce marché")
            elif p["nature"][i] > 0:
                morceaux.append(f"même nature ({nature.libelle(c.nature[i]).lower()})")
            else:
                morceaux.append(f"nature différente ({nature.libelle(c.nature[i]).lower()})")
        d["objet_nature"]["explication"] = " ; ".join(morceaux)
        d["objet_nature"]["parties"] = {"texte": round(float(p["texte"][i]), 3),
                                        "famille": None if p["famille"][i] < 0 else int(p["famille"][i]),
                                        "nature": None if p["nature"][i] < 0 else int(p["nature"][i])}
        # acheteur
        v = comp.valeurs["acheteur"][i]
        d["acheteur"]["explication"] = ("même acheteur" if v >= 1 else
                                        f"même type d'acheteur ({c.acheteur_type[i]})" if v > 0 else "autre acheteur")
        # qualification
        fiche = c.fiches[i].quals
        if not cible.quals:
            d["qualification"]["explication"] = "l'AO ne donne pas de qualification"
        elif not fiche:
            d["qualification"]["explication"] = "qualification non renseignée pour ce marché"
        else:
            communs = [f"{code} (classe {fiche[code] if fiche[code] is not None else '?'} / "
                       f"{cible.quals[code] if cible.quals[code] is not None else '?'})"
                       for code in cible.quals if code in fiche]
            d["qualification"]["explication"] = ("codes communs : " + ", ".join(communs)) if communs else \
                "aucun code en commun"
        # montant
        if cible.estimation:
            d["montant"]["explication"] = f"×{comp.ratio[i]:.2f} ({_millions(c.estimation[i])} contre {_millions(cible.estimation)})"
        else:
            d["montant"]["explication"] = "l'AO n'a pas d'estimation"
        # région
        d["region"]["explication"] = (f"même région ({c.region[i]})" if comp.valeurs["region"][i] > 0 else
                                      "région différente ou inconnue")
        # récence
        mois = max(0, cible.jour - int(c.jour[i])) / 30.44
        d["recence"]["explication"] = f"il y a {mois:.0f} mois"
        return d

    def ligne(self, cible: Cible, comp: Composantes, i: int, notes: np.ndarray, poids: dict) -> dict:
        c = self.corpus
        u = c.unites[i]
        return {
            "identifiant": u.identifiant, "ref": u.ref, "lot": u.lot,
            "objet": c.fiches[i].texte[:260], "acheteur": u.acheteur,
            "date": date.fromordinal(int(c.jour[i])).isoformat(), "estimation": float(c.estimation[i]),
            "famille": nature.libelle(c.famille[i]), "nature": nature.libelle(c.nature[i]),
            "score": round(float(notes[i]), 1), "detail": self.detail(cible, comp, i, poids),
            "gagnant": c.gagnant(i), "reference_taux": round(float(c.ref_taux[i]), 4),
            "nb_offres": int(c.nb_offres[i]), "nb_concurrents": int(c.nb_concurrents[i]),
        }


def _inutiles(cible: Cible) -> frozenset:
    return frozenset(n for n, absent in (("qualification", not cible.quals), ("montant", not cible.estimation),
                                         ("region", not cible.region)) if absent)


def poids_utiles(comp: Composantes, poids: dict[str, float]) -> dict[str, float]:
    """Les poids, mis à 0 pour les critères sans information côté cible."""
    utiles = {n: (0.0 if n in comp.inutiles else float(poids[n])) for n in CRITERES}
    # Poids passés en essai qui ne portent que sur des critères inutilisables : on garde les poids demandés.
    return utiles if sum(utiles.values()) > 0 else {n: float(poids[n]) for n in CRITERES}


def _millions(m: float) -> str:
    return f"{m / 1e6:.2f} M".replace(".", ",")


def mediane_reference(moteur: Moteur, indices: np.ndarray, n: int) -> dict:
    """Médiane (et quartiles) du prix de référence en taux des `n` premiers marchés."""
    v = moteur.corpus.ref_taux[indices[:n]]
    if len(v) == 0:
        return {"nb": 0, "mediane": None, "q1": None, "q3": None}
    q1, med, q3 = np.percentile(v, [25, 50, 75])
    return {"nb": int(len(v)), "mediane": round(float(med), 4), "q1": round(float(q1), 4), "q3": round(float(q3), 4)}


# ------------------------------------------------------------------ moteurs en mémoire
_verrou = threading.RLock()
_corpus_memo: dict = {}
_moteurs: "OrderedDict[tuple, Moteur]" = OrderedDict()
MAX_MOTEURS_TEMPORAIRES = 4


def _cle_fichier(chemin) -> tuple:
    st = chemin.stat()
    return (str(chemin), st.st_mtime_ns, st.st_size)


def corpus_courant(ouvrir_historique, chemin) -> _corpus.Corpus:
    """Le corpus de l'historique actuel, refait seulement quand pv_travaux.db change (remplacée au déploiement)."""
    cle = _cle_fichier(chemin)
    with _verrou:
        if _corpus_memo.get("cle") != cle:
            db = ouvrir_historique()
            try:
                _corpus_memo.clear()
                _corpus_memo.update(cle=cle, corpus=_corpus.construire(db))
                _moteurs.clear()
            finally:
                db.close()
        return _corpus_memo["corpus"]


def moteur(ouvrir_historique, chemin, calc: sqlite3.Connection | None, calcul: str = "courant",
           jour_limite: int | None = None, sauver: bool | None = None) -> Moteur:
    """Le moteur du calcul « courant », ou d'un calcul « jusqua-… » dont l'idf n'a vu que les marchés
    antérieurs à `jour_limite` (rejeu ou backtest). Le premier appel recharge l'index depuis
    copilote_calcul.db, ou le calcule et le range."""
    with _verrou:
        corpus = corpus_courant(ouvrir_historique, chemin)
        cle = (_corpus_memo["cle"], calcul)
        if cle in _moteurs:
            _moteurs.move_to_end(cle)
            return _moteurs[cle]
        sig = corpus.signature() + ("" if calcul == "courant" else f"|{calcul}")
        ajuste = None if jour_limite is None else corpus.avant(jour_limite)
        if sauver is None:
            sauver = calcul == "courant"
        index, origine = index_texte.obtenir(calc, calcul, corpus.texte, sig, sauver=sauver, ajuste_sur=ajuste)
        m = Moteur(corpus, index, calcul, origine)
        _moteurs[cle] = m
        temporaires = [k for k in _moteurs if k[1] != "courant"]
        while len(temporaires) > MAX_MOTEURS_TEMPORAIRES:
            _moteurs.pop(temporaires.pop(0))
        return m


def oublier() -> None:
    """Vide les caches en mémoire (tests)."""
    with _verrou:
        _corpus_memo.clear()
        _moteurs.clear()


def jour_de(texte: str | None) -> int | None:
    """AAAA-MM-JJ ou AAAAMMJJ → ordinal."""
    t = (texte or "").replace("-", "")[:8]
    try:
        return datetime.strptime(t, "%Y%m%d").date().toordinal()
    except ValueError:
        return None
