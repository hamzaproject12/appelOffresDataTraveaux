"""Index de similarité de l'objet : TF-IDF sur caractères, calculé une fois et rangé dans copilote_calcul.db.

Même calcul que sklearn.feature_extraction.text.TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 4),
min_df=2, sublinear_tf=True, lowercase=False) — un test le vérifie — mais écrit en deux temps
(CountVectorizer + idf), pour pouvoir ranger le vocabulaire et l'idf sans pickle : un vecteur de texte
nouveau (l'objet d'un AO) se calcule avec le même vocabulaire et le même idf que l'historique.

Rangement : une ligne par calcul dans `sim_index` (« courant », ou « jusqua-AAAA-MM-JJ » pour un
backtest), avec la signature du corpus. Si le corpus a changé (nouvelle pv_travaux.db), la signature ne
correspond plus et l'index est refait ; on ne sert jamais un index qui ne va pas avec les lignes.
"""
from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import scipy.sparse as sp
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.preprocessing import normalize

ANALYSEUR = "char_wb"
NGRAMMES = (3, 4)
MIN_DF = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_index (
  calcul TEXT PRIMARY KEY,            -- courant | jusqua-AAAA-MM-JJ
  signature TEXT NOT NULL,            -- empreinte du corpus (identifiants + textes, dans l'ordre)
  nb_docs INTEGER, nb_traits INTEGER,
  parametres TEXT,                    -- analyseur, n-grammes, min_df
  cree_le TEXT,
  vocabulaire TEXT NOT NULL,          -- JSON {n-gramme: colonne}
  idf BLOB NOT NULL,                  -- npy float32
  matrice BLOB NOT NULL);             -- npz (CSR float32, lignes normalisées)
"""


def _compteur(vocabulaire: dict | None = None) -> CountVectorizer:
    return CountVectorizer(analyzer=ANALYSEUR, ngram_range=NGRAMMES, lowercase=False, dtype=np.float32,
                           min_df=MIN_DF if vocabulaire is None else 1, vocabulary=vocabulaire)


def _tfidf(comptes: sp.csr_matrix, idf: np.ndarray) -> sp.csr_matrix:
    """Fréquence sous-linéaire (1 + ln tf) × idf, puis norme L2 : le calcul de TfidfVectorizer."""
    x = comptes.tocsr().astype(np.float32, copy=True)
    x.data = 1.0 + np.log(x.data)
    x = x.multiply(idf).tocsr()
    return normalize(x, norm="l2", copy=False).astype(np.float32)


@dataclass
class Index:
    vocabulaire: dict[str, int]
    idf: np.ndarray
    matrice: sp.csr_matrix                      # (documents × traits), lignes de norme 1
    signature: str = ""
    cree_le: str = ""

    def vecteur(self, texte: str) -> sp.csr_matrix:
        """Le vecteur d'un texte à plat (voir corpus.texte_index), dans l'espace de l'index."""
        comptes = _compteur(self.vocabulaire).transform([texte])
        return _tfidf(comptes, self.idf)

    def similarites(self, texte: str, jusqua: int | None = None) -> np.ndarray:
        """Cosinus du texte avec les `jusqua` premiers documents (tous par défaut)."""
        x = self.vecteur(texte)
        m = self.matrice if jusqua is None else self.matrice[:jusqua]
        return np.asarray((m @ x.T).todense()).ravel()


def construire(textes: list[str], signature: str = "", ajuste_sur: int | None = None) -> Index:
    """Calcule l'index de tous les textes. Vocabulaire et idf sont ajustés sur les `ajuste_sur` premiers
    seulement (les plus anciens, le corpus étant rangé par date) : c'est ce qui garde un backtest strict,
    aucun texte postérieur ne pesant dans le vocabulaire ni dans l'idf."""
    m = len(textes) if ajuste_sur is None else ajuste_sur
    cv = _compteur()
    comptes_ajuste = cv.fit_transform(textes[:m])
    df = np.asarray((comptes_ajuste > 0).sum(axis=0)).ravel()
    idf = (np.log((1.0 + m) / (1.0 + df)) + 1.0).astype(np.float32)
    comptes = comptes_ajuste if m == len(textes) else sp.vstack(
        [comptes_ajuste, cv.transform(textes[m:])], format="csr")
    return Index({k: int(v) for k, v in cv.vocabulary_.items()}, idf, _tfidf(comptes, idf), signature,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def enregistrer(calc: sqlite3.Connection, calcul: str, index: Index) -> None:
    calc.executescript(SCHEMA)
    tampon = io.BytesIO()
    sp.save_npz(tampon, index.matrice, compressed=True)
    idf = io.BytesIO()
    np.save(idf, index.idf, allow_pickle=False)
    with calc:
        calc.execute("DELETE FROM sim_index WHERE calcul = ?", (calcul,))
        calc.execute(
            "INSERT INTO sim_index (calcul, signature, nb_docs, nb_traits, parametres, cree_le, vocabulaire, idf,"
            " matrice) VALUES (?,?,?,?,?,?,?,?,?)",
            (calcul, index.signature, index.matrice.shape[0], index.matrice.shape[1],
             json.dumps({"analyseur": ANALYSEUR, "ngrammes": NGRAMMES, "min_df": MIN_DF}), index.cree_le,
             json.dumps(index.vocabulaire, ensure_ascii=False), idf.getvalue(), tampon.getvalue()))


def charger(calc: sqlite3.Connection, calcul: str, signature: str | None = None) -> Index | None:
    """L'index rangé, ou None s'il n'existe pas ou ne correspond pas à `signature`."""
    try:
        r = calc.execute("SELECT signature, cree_le, vocabulaire, idf, matrice FROM sim_index WHERE calcul = ?",
                         (calcul,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if r is None or (signature is not None and r["signature"] != signature):
        return None
    return Index(json.loads(r["vocabulaire"]), np.load(io.BytesIO(r["idf"]), allow_pickle=False),
                 sp.load_npz(io.BytesIO(r["matrice"])).tocsr(), r["signature"], r["cree_le"])


def obtenir(calc: sqlite3.Connection | None, calcul: str, textes: list[str], signature: str,
            sauver: bool = True, ajuste_sur: int | None = None) -> tuple[Index, str]:
    """L'index de ce corpus : rechargé s'il est rangé, sinon calculé (et rangé). Renvoie (index, origine)."""
    if calc is not None:
        trouve = charger(calc, calcul, signature)
        if trouve is not None:
            return trouve, "recharge"
    index = construire(textes, signature, ajuste_sur)
    if calc is not None and sauver:
        enregistrer(calc, calcul, index)
    return index, "calcule"
