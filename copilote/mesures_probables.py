"""Ajustement et mesure des sociétés probables (étapes A et A2).

    python -m copilote.mesures_probables ajuster [--annee 2025] [--n 1200]  coefficients → probables.json
    python -m copilote.mesures_probables mesurer [--annee 2026] [--n 500]   backtest sur des marchés tirés au hasard
    python -m copilote.mesures_probables rejeux                              les AO « REJEU- » (par l'API interne)
    python -m copilote.mesures_probables tout                                mesurer + rejeux, versions A et A2
                                                                              → rapports/copilote_etape_A2_mesures.json

Deux versions sont mesurées sur les MÊMES marchés : A (probables_A.json, groupes par union-find, figée le
02/11/2026) et A2 (probables.json : présence chez l'acheteur, calibration par segment, historique du nombre de
concurrents, groupes en étoile). Les deux sont ajustées sur 2025.

Sans fuite : un marché du jour d est analysé avec l'historique daté au plus tard de la veille (Memoire.coupe),
les similaires datés avant d (similarite.Moteur), un index dont l'idf n'a vu que les années d'avant celle
testée. Les coefficients sont ajustés sur l'année précédant le backtest : les marchés mesurés n'ont servi
à rien. Ce module lit la vérité (les offres réellement déposées) ; le moteur, lui, ne la voit jamais.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta

import numpy as np

from . import analyse, bases, config, groupes_ao, probables, reglages, rejeu, similarite
from .mesures_similarite import cible_du_marche

SORTIE = config.RAPPORTS / "copilote_etape_A_mesures.json"
SORTIE_A2 = config.RAPPORTS / "copilote_etape_A2_mesures.json"
N_SIMILAIRES = 50
SEUILS_CALIBRATION = (0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1.01)
TRANCHES_HABITUEES = ((0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))
HABITUEE_MIN_MARCHES = 4          # contrôle du point 1 : au moins 4 marchés passés de même famille chez l'acheteur
HABITUEE_MIN_TAUX = 0.6           # … et présente sur au moins 60 % d'entre eux


def versions() -> dict[str, dict]:
    """Les deux jeux de coefficients mesurés côte à côte."""
    return {"A": probables.charger_coefficients(probables.FICHIER_A), "A2": probables.charger_coefficients()}


# --------------------------------------------------------------------------- préparation

def _veille(tri_date: str) -> str:
    return (datetime.strptime(tri_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


def moteur_annee(annee: str, sauver: bool) -> similarite.Moteur:
    debut = similarite.jour_de(f"{annee}-01-01")
    with closing(bases.calcul()) as calc:
        return similarite.moteur(bases.historique, bases.chemin_historique(), calc, f"jusqua-{int(annee) - 1}-12-31",
                                 jour_limite=debut, sauver=sauver)


def cibles(m: similarite.Moteur, annee: str, n: int, graine: int = 0) -> list[int]:
    c = m.corpus
    debut, fin = similarite.jour_de(f"{annee}-01-01"), similarite.jour_de(f"{int(annee) + 1}-01-01")
    toutes = [i for i in range(len(c)) if debut <= c.jour[i] < fin and c.avant(int(c.jour[i])) >= N_SIMILAIRES
              and any(k["cle"] for k in c.unites[i].concurrents)]
    rng = random.Random(graine)
    return sorted(rng.sample(toutes, min(n, len(toutes))))


def contexte_marche(m: similarite.Moteur, mem: probables.Memoire, i: int, r0: dict) -> probables.Contexte:
    c = m.corpus
    cible = cible_du_marche(m, i)
    comp = m.composantes(cible, r0)
    notes = m.noter(comp, r0["poids"])
    retenus = m.meilleurs(notes, N_SIMILAIRES)
    limite = _veille(c.unites[i].date)
    return analyse.contexte(mem, limite, int(c.jour[i]), cible, m, notes, comp, retenus)


def reels(u) -> set[str]:
    return {k["cle"] for k in u.concurrents if k["cle"]}


# --------------------------------------------------------------------------- ajustement

def distribution_np(ps, lam: float, dispersion: float, decalage: float = 0.0) -> np.ndarray:
    """probables.distribution en numpy (mêmes nœuds de Gauss-Hermite), pour les grilles d'ajustement.
    Contrôlée contre la version de référence dans tests/test_probables.py."""
    from scipy.special import gammaln
    ps = np.clip(np.asarray(ps, dtype=float), 1e-12, 1 - 1e-12)
    notes = np.log(ps / (1 - ps))
    s = max(0.0, float(dispersion))
    total = np.zeros(1)
    for x, w in zip(probables.HERMITE_X, probables.HERMITE_W):
        u = decalage + s * x - s * s / 2
        q = 1 / (1 + np.exp(-(notes + u)))
        d = np.zeros(len(q) + 1)
        d[0] = 1.0
        for k, pk in enumerate(q):
            d[1:k + 2] = d[1:k + 2] * (1 - pk) + d[0:k + 1] * pk
            d[0] *= 1 - pk
        l2 = lam * np.exp(u)
        if l2 > 0:
            ks = np.arange(int(l2 + 10 * np.sqrt(l2) + 12))
            d = np.convolve(d, np.exp(ks * np.log(l2) - l2 - gammaln(ks + 1)))
        if len(d) > len(total):
            total = np.concatenate([total, np.zeros(len(d) - len(total))])
        total[:len(d)] += w * d
    return total


def _fourchette_np(d: np.ndarray, niveau: float = 0.8) -> tuple[int, int, int]:
    c = np.cumsum(d)
    bas = int(np.searchsorted(c, (1 - niveau) / 2 - 1e-12))
    haut = int(min(np.searchsorted(c, 1 - (1 - niveau) / 2 - 1e-12), len(d) - 1))
    return bas, haut, int(np.argmax(d))


def _preparer_compte(marches: list[dict], coefs: dict) -> list[dict]:
    """Pour chaque marché : les p envoyées, λ (autres + reste), le vrai nombre et l'historique de l'acheteur."""
    sortie = []
    for x in marches:
        p = [probables.probabilite(v, coefs)[0] for v in x["x"]]
        envoyes = [q for q in p if q >= probables.REGLAGES["seuil_envoi"]]
        reste = sum(q for q in p if q < probables.REGLAGES["seuil_envoi"])
        lam = max(0.0, coefs["autres"]["constante"] + coefs["autres"]["par_offre_similaire"] * x["moy"])
        sortie.append({"ps": envoyes, "lam": lam + reste, "vrais": x["vrais"], "hist": x.get("hist", [])})
    return sortie


def _evaluer(prep: list[dict], dispersion: float, historique: dict | None) -> dict:
    dedans, ecarts, largeurs = [], [], []
    for x in prep:
        h = probables.corriger_par_historique(x["ps"], x["lam"], dispersion, x["hist"], historique)
        bas, haut, probable = _fourchette_np(distribution_np(x["ps"], x["lam"], h["dispersion"], h["decalage"]))
        dedans.append(bas <= x["vrais"] <= haut)
        ecarts.append(abs(probable - x["vrais"]))
        largeurs.append(haut - bas)
    return {"couverture": float(np.mean(dedans)), "ecart_median": float(np.median(ecarts)),
            "ecart_moyen": float(np.mean(ecarts)), "largeur": float(np.mean(largeurs))}


def _couverture(marches: list[dict], coefs: dict, dispersion: float) -> float:
    return _evaluer(_preparer_compte(marches, coefs), dispersion, None)["couverture"]


def calibration(marches: list[dict], coefs: dict, points: int = 120, seg: str | None = None, min_bloc: int = 40) -> dict:
    """Table strictement croissante p de la formule → part réellement venue, par régression isotone sur
    des marchés que la régression logistique n'a pas vus ; les blocs voisins sont fusionnés jusqu'à `points`.
    Avec `seg`, sur les seuls couples société × marché de ce segment (probables.segment)."""
    from sklearn.isotonic import IsotonicRegression
    brutes, venues = [], []
    sans = {**coefs, "calibration": None, "calibration_segments": None}
    for mm in marches:
        for x, y in zip(mm["x"], mm["y"]):
            if seg is not None and probables.segment(x) != seg:
                continue
            brutes.append(probables.probabilite(x, sans)[2])
            venues.append(y)
    ordre = np.argsort(brutes)
    xb, yb = np.array(brutes)[ordre], np.array(venues, dtype=float)[ordre]
    ajuste = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit_transform(xb, yb)
    blocs = []                                   # [somme x, somme y, n] par valeur constante
    for x, y in zip(xb, ajuste):
        if blocs and abs(blocs[-1][1] / blocs[-1][2] - y) < 1e-12:
            blocs[-1][0] += x
            blocs[-1][1] += y
            blocs[-1][2] += 1
        else:
            blocs.append([x, y, 1])
    # fusionne la paire voisine la plus légère, tant qu'il y a trop de blocs ou un bloc de moins de
    # `min_bloc` couples : une extrémité apprise sur 3 sociétés donnait 0,999 (trop sûre sur 2026)
    while len(blocs) > 1 and (len(blocs) > points or min(b[2] for b in blocs) < min_bloc):
        k = min(range(len(blocs) - 1), key=lambda i: blocs[i][2] + blocs[i + 1][2])
        a, b = blocs[k], blocs[k + 1]
        blocs[k:k + 2] = [[a[0] + b[0], a[1] + b[1], a[2] + b[2]]]
    xs, ys = [], []
    for sx, sy, n in blocs:
        x, y = sx / n, max(sy / n, 1e-4)
        if xs and (x <= xs[-1] or y <= ys[-1]):
            y = max(y, ys[-1] + 1e-6)
            if x <= xs[-1]:
                continue
        xs.append(round(float(x), 6))
        ys.append(round(float(min(y, 0.999)), 6))
    return {"x": xs, "y": ys}


def ajuster(annee: str = "2025", n: int = 1200, graine: int = 0, ecrire: bool = True, cache: str | None = None) -> dict:
    """Coefficients de la formule sur les marchés pairs de l'échantillon ; calibration par segment, λ (sociétés
    que rien n'annonce), σ (dispersion), puis k et σ_h (historique de l'acheteur) sur les marchés impairs, que
    la régression n'a pas vus. Tout est daté de `annee` : rien d'après le 31/12 de cette année."""
    from sklearn.linear_model import LogisticRegression
    t0 = time.time()
    marches = _marches_ajustement(annee, n, graine, cache)
    pairs, impairs = marches[::2], marches[1::2]
    X = np.array([[x[v] for v in probables.VARIABLES] for mm in pairs for x in mm["x"]])
    y = np.array([v for mm in pairs for v in mm["y"]])
    lr = LogisticRegression(C=10.0, max_iter=5000).fit(X, y)
    # Les habituées : leur propre formule, sur leurs seuls couples (ils se perdaient parmi un million d'autres).
    hab = np.array([probables.segment(x) == "habituee" for mm in pairs for x in mm["x"]])
    lr_h = LogisticRegression(C=1.0, max_iter=5000).fit(X[hab], y[hab])
    A = np.vstack([np.ones(len(impairs)), [mm["moy"] for mm in impairs]]).T
    a, b = np.linalg.lstsq(A, np.array([mm["autres"] for mm in impairs], dtype=float), rcond=None)[0]
    coefs = {"version": "A2", "groupes": "etoile",
             # alliées cherchées dès 7 % : 57 % des vrais membres de groupe retrouvés au lieu de 41 % à 15 %
             # (200 marchés de 2026, groupes en étoile)
             "reglages": {"seuil_allies": 0.07},
             "constante": round(float(lr.intercept_[0]), 4),
             "coefficients": {v: round(float(w), 4) for v, w in zip(probables.VARIABLES, lr.coef_[0])},
             "par_segment": {"habituee": {
                 "constante": round(float(lr_h.intercept_[0]), 4),
                 "coefficients": {v: round(float(w), 4) for v, w in zip(probables.VARIABLES, lr_h.coef_[0])},
                 "couples": int(hab.sum()), "venues": int(y[hab].sum())}},
             "autres": {"constante": round(float(max(a, 0.0)), 4), "par_offre_similaire": round(float(max(b, 0.0)), 4)},
             "dispersion": 0.0}
    return _ajuster_suite(coefs, marches, pairs, impairs, y, annee, t0, ecrire)


def _marches_ajustement(annee: str, n: int, graine: int, cache: str | None) -> list[dict]:
    """Les variables de chaque candidate sur n marchés de `annee` ; gardées dans `cache` (pickle) si demandé."""
    import pickle
    from pathlib import Path
    if cache and Path(cache).exists():
        with open(cache, "rb") as f:
            return pickle.load(f)
    r0 = reglages.charger()
    m = moteur_annee(annee, sauver=False)
    mem = probables.memoire(bases.historique, bases.chemin_historique())
    marches = []
    t0 = time.time()
    for k, i in enumerate(cibles(m, annee, n, graine)):
        ctx = contexte_marche(m, mem, i, r0)
        vrais = reels(m.corpus.unites[i])
        sig = probables.signaux(mem, ctx)
        marches.append({"x": [s["x"] for s in sig], "y": [int(s["cle"] in vrais) for s in sig],
                        "autres": len(vrais - {s["cle"] for s in sig}), "vrais": len(vrais),
                        "moy": probables.offres_moyennes_similaires(ctx),
                        "hist": probables.historique_acheteur(mem, ctx)})
        if k % 100 == 0:
            print(f"  ajuster {k} ({time.time() - t0:.0f} s)", flush=True)
    if cache:
        with open(cache, "wb") as f:
            pickle.dump(marches, f)
    return marches


def _habituees_x(marches: list[dict], coefs: dict) -> list[dict]:
    """Le contrôle du point 1 sur les variables seules : présences et marchés chez l'acheteur se relisent dans
    acheteur_famille_n = log(1 + k) et acheteur_famille = k / (n + lissage)."""
    import math
    lissage = probables.REGLAGES["lissage_acheteur"]
    sortie = []
    for mm in marches:
        for x, venue in zip(mm["x"], mm["y"]):
            k = round(math.expm1(x["acheteur_famille_n"]))
            if k <= 0 or x["acheteur_famille"] <= 0:
                continue
            n = round(k / x["acheteur_famille"] - lissage)
            if n >= HABITUEE_MIN_MARCHES and k / n >= HABITUEE_MIN_TAUX:
                sortie.append({"taux": k / n, "p": probables.probabilite(x, coefs)[0], "venue": bool(venue)})
    return sortie


def _ajuster_suite(coefs, marches, pairs, impairs, y, annee, t0, ecrire) -> dict:
    coefs["calibration"] = calibration(impairs, coefs)
    coefs["calibration_segments"] = {seg: calibration(impairs, coefs, seg=seg) for seg in probables.SEGMENTS}
    coefs["ajuste_sur"] = {"couples_par_segment": {
        seg: sum(1 for mm in impairs for x in mm["x"] if probables.segment(x) == seg) for seg in probables.SEGMENTS}}
    coefs["ajuste_sur"]["controle_habituees"] = controle_habituees([{"habituees": _habituees_x(impairs, coefs)}])
    print(f"  contrôle des habituées (marchés tenus à part) : {coefs['ajuste_sur']['controle_habituees']}", flush=True)
    prep = _preparer_compte(impairs, coefs)
    essais = {}
    for s10 in range(0, 16):
        essais[s10 / 10] = _evaluer(prep, s10 / 10, None)["couverture"]
    coefs["dispersion"] = min(essais, key=lambda s: (abs(essais[s] - 0.80), s))
    print(f"  σ = {coefs['dispersion']} ({time.time() - t0:.0f} s)", flush=True)
    grille = {}
    for kk in (2, 4, 8, 15):
        for sh10 in range(0, 10):
            grille[(kk, sh10 / 10)] = _evaluer(prep, coefs["dispersion"], {"k": kk, "dispersion": sh10 / 10})
    sans = _evaluer(prep, coefs["dispersion"], None)
    # La couverture à ±1 point de 80 % d'abord (un premier essai à 76 % s'est révélé trop étroit sur 2026 et
    # les REJEU), puis l'erreur moyenne la plus faible.
    bons = [c for c, v in grille.items() if abs(v["couverture"] - 0.80) <= 0.01]
    choix = (min(bons, key=lambda c: (grille[c]["ecart_moyen"], grille[c]["largeur"]))
             if bons else min(grille, key=lambda c: abs(grille[c]["couverture"] - 0.8)))
    coefs["historique"] = {"k": choix[0], "dispersion": choix[1]}
    print(f"  historique k = {choix[0]}, σh = {choix[1]} : {grille[choix]} (sans : {sans})", flush=True)
    coefs["ajuste_sur"]["historique"] = {
        "sans": {k: round(v, 3) for k, v in sans.items()},
        "avec": {k: round(v, 3) for k, v in grille[choix].items()},
        "grille": {f"k={c[0]} sh={c[1]}": {k: round(v, 3) for k, v in g.items()} for c, g in grille.items()}}
    coefs["ajuste_sur"]["calibration_points"] = len(coefs["calibration"]["x"])
    coefs["origine"] = (f"régression logistique sur {len(y)} couples société × marché de {annee} "
                        f"({int(y.sum())} venues, {len(pairs)} marchés) ; calibration par segment, λ, σ et historique de "
                        f"l'acheteur sur {len(impairs)} autres marchés ; ajustée le {date.today().isoformat()}")
    coefs["appris_jusqu_au"] = f"{annee}-12-31"
    coefs["ajuste_sur"] = {**coefs["ajuste_sur"], "annee": annee, "marches": len(marches), "couples": int(len(y)),
                           "venues": int(y.sum()),
                           "autres_moyen": round(float(np.mean([mm["autres"] for mm in marches])), 3),
                           "couverture_selon_dispersion": {str(k): round(v, 3) for k, v in essais.items()}}
    if ecrire:
        with open(probables.FICHIER, "w", encoding="utf-8") as f:
            json.dump(coefs, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return coefs


# --------------------------------------------------------------------------- mesures d'un marché

def vrais_membres(mem: probables.Memoire, n: int, vrais: set[str], memo: dict) -> set[str]:
    """Les vraies soumissionnaires liées entre elles : Y est dans le groupe « autour » de X (même calcul,
    même coupure), X et Y ont toutes deux déposé."""
    sortie = set()
    for x in vrais:
        if x not in memo:
            memo[x] = set(groupes_ao.autour(mem, n, x)["groupe"][1:])
        communs = memo[x] & vrais
        if communs:
            sortie |= communs | {x}
    return sortie


def habituees(cands: list[dict], vrais: set[str]) -> list[dict]:
    """Contrôle du point 1 : les sociétés présentes sur au moins 60 % d'au moins 4 marchés passés de même famille
    chez l'acheteur (24 mois), avec leur probabilité prédite et leur venue réelle."""
    sortie = []
    for c in cands:
        b = c.get("brut") or {}
        total = b.get("acheteur_famille_total") or 0
        if total >= HABITUEE_MIN_MARCHES and b.get("acheteur_famille", 0) / total >= HABITUEE_MIN_TAUX:
            sortie.append({"taux": b["acheteur_famille"] / total, "p": c["p"], "venue": c["cle"] in vrais})
    return sortie


def mesurer_un(a: dict, vrais: set[str], gagnant: str | None, mem: probables.Memoire, n: int,
               memo: dict | None = None) -> dict:
    """`a` : la sortie de probables.analyser, ou le dossier de l'API (mêmes clés)."""
    cands = a["candidats"] if "candidats" in a else a["probables"]["candidats"]
    ordre = sorted(cands, key=lambda c: (-c["p"], c["cle"]))
    top10 = {c["cle"] for c in ordre[:10]}
    top15 = {c["cle"] for c in ordre[:15]}
    top30 = {c["cle"] for c in ordre[:30]}
    par_assoc = {c["cle"] for c in cands if c["association"]}
    liste = {c["cle"] for c in cands if c["dans_liste"]} | par_assoc
    seulement_assoc = par_assoc - {c["cle"] for c in cands if c["dans_liste"]}
    base = [c for c in cands if c.get("brut")]
    base.sort(key=lambda c: (-c["brut"]["similaires"], -c["brut"]["acheteur"], c["cle"]))
    base15 = {c["cle"] for c in base[:15]}
    base10 = {c["cle"] for c in base[:10]}
    conc = a["concurrents"]
    membres_predits = {m for g in a["groupes"] for m in g["membres"]}
    vm = vrais_membres(mem, n, vrais, memo if memo is not None else {})
    return {
        "vrais": len(vrais), "top10": len(vrais & top10), "top15": len(vrais & top15), "liste": len(vrais & liste),
        "taille_liste": len(liste), "base10": len(vrais & base10), "base15": len(vrais & base15),
        "gagnant": gagnant, "gagnant_liste": None if not gagnant else gagnant in liste,
        "gagnant_top15": None if not gagnant else gagnant in top15,
        "gagnant_top30": None if not gagnant else gagnant in top30,
        "gagnant_base15": None if not gagnant else gagnant in base15,
        "bas": conc["bas"], "haut": conc["haut"], "probable": conc["probable"],
        "dans_fourchette": conc["bas"] <= len(vrais) <= conc["haut"],
        "association": len(par_assoc), "trouvees_par_association": len(vrais & seulement_assoc),
        "vrais_membres": len(vm), "membres_retrouves": len(vm & membres_predits),
        "groupes": len(a["groupes"]), "taille_groupes": sum(len(g["membres"]) for g in a["groupes"]),
        "habituees": habituees(cands, vrais),
        "p": {c["cle"]: c["p"] for c in cands}, "venus": sorted(vrais & {c["cle"] for c in cands}),
    }


def controle_habituees(lignes: list[dict]) -> list[dict]:
    sortie = []
    tout = [h for x in lignes for h in x["habituees"]]
    for lo, hi in TRANCHES_HABITUEES:
        hs = [h for h in tout if lo <= h["taux"] < hi]
        if not hs:
            sortie.append({"de": lo, "a": min(hi, 1.0), "n": 0})
            continue
        p = float(np.mean([h["p"] for h in hs]))
        venues = float(np.mean([h["venue"] for h in hs]))
        taux = float(np.mean([h["taux"] for h in hs]))
        sortie.append({"de": lo, "a": min(hi, 1.0), "n": len(hs), "p_moyenne": round(p, 3),
                       "venues": round(venues, 3), "taux_passe": round(taux, 3),
                       "ecart_points": round(100 * (p - venues), 1), "ok": abs(p - venues) <= 0.10})
    return sortie


def resumer(lignes: list[dict]) -> dict:
    if not lignes:
        return {"n": 0}
    def moy(f):
        v = [f(x) for x in lignes]
        v = [x for x in v if x is not None]
        return round(float(np.mean(v)), 4) if v else None
    calib = []
    for lo, hi in zip(SEUILS_CALIBRATION, SEUILS_CALIBRATION[1:]):
        ps, obs = [], []
        for x in lignes:
            venus = set(x["venus"])
            for cle, p in x["p"].items():
                if lo <= p < hi:
                    ps.append(p)
                    obs.append(cle in venus)
        if ps:
            calib.append({"de": lo, "a": min(hi, 1.0), "n": len(ps), "p_moyenne": round(float(np.mean(ps)), 3),
                          "venues": round(float(np.mean(obs)), 3)})
    tous_vrais = sum(x["vrais"] for x in lignes)
    vm = sum(x["vrais_membres"] for x in lignes)
    return {
        "n": len(lignes),
        "rappel_top10": moy(lambda x: x["top10"] / x["vrais"]),
        "rappel_top15": moy(lambda x: x["top15"] / x["vrais"]),
        "rappel_liste": moy(lambda x: x["liste"] / x["vrais"]),
        "rappel_top15_global": round(sum(x["top15"] for x in lignes) / tous_vrais, 4),
        "rappel_top15_possible": moy(lambda x: x["top15"] / min(x["vrais"], 15)),
        "base_top10": moy(lambda x: x["base10"] / x["vrais"]),
        "base_top15": moy(lambda x: x["base15"] / x["vrais"]),
        "gagnant_liste": moy(lambda x: None if x["gagnant_liste"] is None else float(x["gagnant_liste"])),
        "gagnant_top15": moy(lambda x: None if x["gagnant_top15"] is None else float(x["gagnant_top15"])),
        "gagnant_top30": moy(lambda x: None if x.get("gagnant_top30") is None else float(x["gagnant_top30"])),
        "gagnant_base15": moy(lambda x: None if x["gagnant_base15"] is None else float(x["gagnant_base15"])),
        "dans_fourchette": moy(lambda x: float(x["dans_fourchette"])),
        "sous_fourchette": moy(lambda x: float(x["vrais"] < x["bas"])),
        "sur_fourchette": moy(lambda x: float(x["vrais"] > x["haut"])),
        "largeur_fourchette": moy(lambda x: x["haut"] - x["bas"]),
        "ecart_probable": moy(lambda x: abs(x["probable"] - x["vrais"])),
        "ecart_median": round(float(np.median([abs(x["probable"] - x["vrais"]) for x in lignes])), 2),
        "taille_liste": moy(lambda x: x["taille_liste"]),
        "groupes_par_marche": moy(lambda x: x.get("groupes", 0)),
        "association_par_marche": moy(lambda x: x["association"]),
        "trouvees_par_association": sum(x["trouvees_par_association"] for x in lignes),
        "vrais_total": tous_vrais,
        "vrais_membres": vm,
        "membres_retrouves": round(sum(x["membres_retrouves"] for x in lignes) / vm, 4) if vm else None,
        "calibration": calib,
        "habituees": controle_habituees(lignes),
    }


def _tranche(v: int) -> str:
    return "1-3" if v <= 3 else "4-7" if v <= 7 else "8-15" if v <= 15 else "16+"


def _rendu(lignes: list[dict]) -> dict:
    return {"resume": resumer(lignes),
            "par_tranche": {t: resumer([x for x in lignes if _tranche(x["vrais"]) == t])
                            for t in ("1-3", "4-7", "8-15", "16+")},
            "lignes": [{k: v for k, v in x.items() if k not in ("p", "venus", "habituees")} for x in lignes]}


# --------------------------------------------------------------------------- backtest et rejeux

def mesurer(annee: str = "2026", n: int = 500, graine: int = 1, coefs: dict[str, dict] | None = None) -> dict:
    """Le backtest de chaque version sur les mêmes marchés (contexte calculé une fois)."""
    r0 = reglages.charger()
    coefs = coefs or versions()
    m = moteur_annee(annee, sauver=True)
    mem = probables.memoire(bases.historique, bases.chemin_historique())
    lignes: dict[str, list] = {v: [] for v in coefs}
    t0 = time.time()
    for k, i in enumerate(cibles(m, annee, n, graine)):
        ctx = contexte_marche(m, mem, i, r0)
        u = m.corpus.unites[i]
        memo: dict = {}
        for v, c in coefs.items():
            a = probables.analyser(mem, ctx, c)
            x = mesurer_un(a, reels(u), u.cle_gagnant, mem, ctx.n, memo)
            x["identifiant"] = u.identifiant
            lignes[v].append(x)
        if k % 50 == 0:
            print(f"  mesurer {k} ({time.time() - t0:.0f} s)", flush=True)
    return {"annee": annee, "graine": graine, "duree_s": round(time.time() - t0),
            "versions": {v: _rendu(l) for v, l in lignes.items()}}


def rejeux(prefixe: str = "REJEU-", coefs: dict[str, dict] | None = None, exclure: str | None = "REJEU-R",
           ouvrir_ao=None) -> dict:
    """Les AO rejoués (par l'API interne), chaque version sur les mêmes marchés ou lots. `exclure` : préfixe
    laissé de côté (les REJEU-R ont leur propre mesure, rejeu_recent.py). `ouvrir_ao` : d'où lire les annonces
    (ao_ouverts.db par défaut ; rejeu_recent.py passe une base en mémoire, sans attendre le serveur)."""
    from . import verite_rejeu
    coefs = coefs or versions()
    lignes: dict[str, list] = {v: [] for v in coefs}
    erreurs = []
    with closing(bases.calcul()) as calc, closing(bases.retours()) as ret, \
            closing((ouvrir_ao or bases.ao_ouverts)()) as ao:
        refs = [r[0] for r in calc.execute("SELECT ref_annonce FROM rejeu_ao ORDER BY ref_annonce")
                if r[0].startswith(prefixe) and not (exclure and r[0].startswith(exclure))]
        mem = probables.memoire(bases.historique, bases.chemin_historique())
        for ref in refs:
            lots = [r[0] for r in ao.execute("SELECT lot FROM appels_lots WHERE ref_annonce = ? ORDER BY rowid", (ref,))]
            for lot in lots or [None]:
                v = verite_rejeu.pour_lot(calc, ref, lot)
                if v is None:
                    erreurs.append({"ref": ref, "lot": lot, "erreur": "vérité absente"})
                    continue
                n = mem.coupe(rejeu.limite_pour(ref, calc))
                memo: dict = {}
                for nom, c in coefs.items():
                    try:
                        a = analyse.analyser(ref, lot, ouvrir_historique=bases.historique,
                                             chemin_historique=bases.chemin_historique(), ao=ao, calc=calc, ret=ret,
                                             coefficients=c)
                    except Exception as e:                      # noqa: BLE001 : on note et on continue
                        erreurs.append({"ref": ref, "lot": lot, "version": nom, "erreur": f"{type(e).__name__}: {e}"})
                        continue
                    x = mesurer_un({**a["probables"], "concurrents": a["concurrents"], "groupes": a["groupes"]},
                                   {c_["cle"] for c_ in v["concurrents"]}, v["gagnant"]["cle"], mem, n, memo)
                    x.update(ref=ref, lot=lot)
                    lignes[nom].append(x)
                    print(f"  {nom} {ref} lot {lot or '-'} : {x['top15']}/{x['vrais']} dans le top 15, "
                          f"fourchette {x['bas']}-{x['haut']}, gagnant {'oui' if x['gagnant_top30'] else 'non'}",
                          flush=True)
    return {"versions": {v: _rendu(l) for v, l in lignes.items()}, "erreurs": erreurs}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m copilote.mesures_probables")
    p.add_argument("commande", choices=("ajuster", "mesurer", "rejeux", "tout"))
    p.add_argument("--annee")
    p.add_argument("--n", type=int)
    p.add_argument("--graine", type=int, default=None)
    p.add_argument("--cache", help="ajuster : fichier pickle où garder les variables des marchés (réajustement rapide)")
    a = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.commande == "ajuster":
        c = ajuster(a.annee or "2025", a.n or 1200, a.graine or 0, cache=a.cache)
        print(json.dumps({k: v for k, v in c.items() if k not in ("calibration", "calibration_segments")},
                         ensure_ascii=False, indent=2)[:6000])
        return 0
    sortie = {}
    if a.commande in ("mesurer", "tout"):
        sortie["backtest"] = mesurer(a.annee or "2026", a.n or 500, 1 if a.graine is None else a.graine)
        for v, r in sortie["backtest"]["versions"].items():
            print(v, json.dumps({k: x for k, x in r["resume"].items() if k != "calibration"}, ensure_ascii=False))
    if a.commande in ("rejeux", "tout"):
        sortie["rejeux"] = rejeux()
        for v, r in sortie["rejeux"]["versions"].items():
            print(v, json.dumps({k: x for k, x in r["resume"].items() if k != "calibration"}, ensure_ascii=False))
        for e in sortie["rejeux"]["erreurs"]:
            print("  erreur", e)
    sortie["coefficients"] = {v: {k: x for k, x in c.items() if k not in ("calibration", "calibration_segments")}
                              for v, c in versions().items()}
    with open(SORTIE_A2, "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=1)
    print(f"→ {SORTIE_A2}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
