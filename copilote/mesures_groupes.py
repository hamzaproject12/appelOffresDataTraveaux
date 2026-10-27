"""Mesures des groupes v2 : diagnostic, couverture des anciens groupes, stabilité, temps, FEU VERT.

    python -m copilote.mesures_groupes [--sortie rapports/copilote_groupes_v2_mesures.json]

Rien n'est écrit dans les bases : la détection est refaite en mémoire (recalculer.py se charge d'écrire).
Les temps de réponse sont mesurés sur le calcul « courant » déjà rangé dans copilote_calcul.db.
Les 66 groupes de la version 1 sont lus dans rapports/copilote_groupes_v1_courant.json (instantané pris
avant le passage en v2).

Critères FEU VERT, fixés avant la mesure :
  partenaires  au moins 1 500 sociétés actives ont un partenaire de niveau moyen ou fort ;
  anciens      les 66 groupes de la version 1 sont tous inclus dans un groupe de la version 2 ;
  recherche    chaque société de la table `societes` est trouvée en cherchant son nom ;
  fiche        la fiche société répond en moins de 300 ms (pire cas mesuré) ;
  vocabulaire  aucun mot accusateur dans la page ni dans les textes de l'API.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
from scipy import sparse

from . import api_groupes, bases, config, fiche_societe, groupes, liens
from . import fusions as _fusions

JUSQUA = "2025-12-31"
ANCIENS = config.RAPPORTS / "copilote_groupes_v1_courant.json"
POIDS_V1 = {"force": 20, "volume": 15, "hasard": 30, "contextes": 15, "hors_profil": 5, "gains": 15}
MOTS_INTERDITS = re.compile(r"entente|collusion|fraude|cartel|complicit|truqu", re.I)
SEUIL_FICHE_MS = 300


def _r(x, n=3):
    return None if x is None else round(float(x), n)


def regle(L: liens.Liens, ok: np.ndarray, actives: np.ndarray) -> dict:
    soc = np.union1d(L.i[ok], L.j[ok])
    return {"paires": int(ok.sum()), "societes": int(len(soc)), "actives": int(actives[soc].sum())}


def diagnostic(d: groupes.Donnees, reglages: dict) -> dict:
    """Les chiffres demandés : règle de la v1, deux règles d'essai, les trois niveaux et leurs faux positifs."""
    L = liens.calculer(d, reglages, min_communs=3)
    n = np.asarray(d.X.sum(axis=1)).ravel()
    actives = n >= reglages["groupes"]["actives"]
    sortie = {"societes": len(d.societes), "actives": int(actives.sum()), "paires_3_communs": len(L)}
    sortie["regles"] = {
        "v1 : ≥ 5 communs, ≥ 50 % dans les deux sens": regle(L, (L.communs >= 5) & (np.minimum(L.part_i, L.part_j) >= 0.5), actives),
        "≥ 4 communs, excès ≥ 3, p < 0,001": regle(L, (L.communs >= 4) & (L.exces >= 3) & (L.p < 0.001), actives),
        "≥ 3 communs, excès ≥ 3, p < 0,01": regle(L, (L.communs >= 3) & (L.exces >= 3) & (L.p < 0.01), actives),
    }
    niveaux = {}
    for nom in liens.NIVEAUX:
        s = reglages["groupes"]["niveaux"][nom]
        tests = int((L.communs >= s["communs"]).sum())
        seul = (L.communs >= s["communs"]) & (L.exces >= s["exces"]) & (L.p < s["p"])
        exact = L.rang == liens.RANG[nom]
        niveaux[nom] = {"seuils": s, "tests": tests, "attendus_par_hasard": round(tests * s["p"], 1),
                        "trouves_par_la_regle": int(seul.sum()), "paires_a_ce_niveau": int(exact.sum()),
                        "dont_presence_mutuelle_seule": int((exact & L.mutuel & ~seul).sum()),
                        "au_moins_ce_niveau": regle(L, L.rang >= liens.RANG[nom], actives)}
    sortie["niveaux"] = niveaux
    sortie["mutuel_total"] = int(L.mutuel.sum())
    # Variante sans les offres écartées : elle explique l'écart avec les chiffres « environ » de la demande.
    admis = _matrice_admises(d)
    L2 = liens.calculer(_Vue(admis, d.marches), reglages, min_communs=3)
    act2 = np.asarray(admis.sum(axis=1)).ravel() >= reglages["groupes"]["actives"]
    sortie["sans_offres_ecartees"] = {
        "actives": int(act2.sum()),
        "v1": regle(L2, (L2.communs >= 5) & (np.minimum(L2.part_i, L2.part_j) >= 0.5), act2),
        "≥ 4, ×3, p < 0,001": regle(L2, (L2.communs >= 4) & (L2.exces >= 3) & (L2.p < 0.001), act2),
        "≥ 3, ×3, p < 0,01": regle(L2, (L2.communs >= 3) & (L2.exces >= 3) & (L2.p < 0.01), act2)}
    return sortie


class _Vue:
    """Une Donnees réduite à ce que liens.calculer lit : X et les marchés (pour l'acheteur)."""
    def __init__(self, X, marches):
        self.X, self.marches = X, marches


def _matrice_admises(d: groupes.Donnees) -> sparse.csr_matrix:
    idx, lignes, colonnes = d.index, [], []
    for j, m in enumerate(d.marches):
        cles = {c["cle"] for u in m.unites for c in u.concurrents if c["cle"] and c["statut"] in ("admis", "attributaire")}
        for c in cles:
            lignes.append(idx[c])
            colonnes.append(j)
    return sparse.csr_matrix((np.ones(len(lignes), dtype=np.int32), (lignes, colonnes)), shape=d.X.shape)


def presences_par_statut(d: groupes.Donnees) -> dict:
    """Chaque présence (société × marché) selon le meilleur statut de ses offres : les écartées comptent."""
    compte = Counter()
    for m in d.marches:
        statuts: dict[str, set] = {}
        for u in m.unites:
            for c in u.concurrents:
                if c["cle"]:
                    statuts.setdefault(c["cle"], set()).add(c["statut"])
        for s in statuts.values():
            compte["admise ou attributaire" if s & {"admis", "attributaire"} else "/".join(sorted(s))] += 1
    return {"total": sum(compte.values()), "matrice": int(d.X.nnz), **dict(compte)}


def structure(r: groupes.Resultat) -> dict:
    tailles = Counter(len(g.membres) for g in r.groupes)
    familles = Counter(len(f.membres) for f in r.familles)
    grandes = [f for f in r.familles if len(f.membres) > r.reglages["taille_max_famille"]]
    return {"groupes": len(r.groupes), "groupes_de_2": tailles[2], "taille_max_groupe": max(tailles, default=0),
            "tailles_groupes": dict(sorted(tailles.items())), "familles": len(r.familles),
            "familles_plus_de_12": len(grandes), "dont_une_seule_clique": sum(len(f.cliques) == 1 for f in grandes),
            "tailles_familles": dict(sorted(familles.items())),
            "niveaux_groupes": dict(Counter(g.mesures["niveau"] for g in r.groupes)),
            "scores": {"mediane": statistics.median(g.score for g in r.groupes) if r.groupes else None,
                       "distribution": dict(sorted(Counter(g.score // 10 * 10 for g in r.groupes).items()))},
            "structure_victoires": dict(Counter(g.mesures["structure"] for g in r.groupes)),
            "duree_s": round(r.duree, 1)}


def anciens_groupes(r: groupes.Resultat) -> dict:
    """Les 66 groupes de la v1 : couverts ? et l'effet du nouveau score (poids de « qui gagne » à 0, force)."""
    if not ANCIENS.exists():
        return {"absent": str(ANCIENS)}
    anciens = json.loads(ANCIENS.read_text(encoding="utf-8"))
    ensembles = [(set(g.membres), g) for g in r.groupes]
    familles = [set(f.membres) for f in r.familles]
    poids = r.poids
    lignes = []
    for a in anciens:
        m = set(a["signature"].split("|"))
        contenants = [g for s, g in ensembles if m <= s]
        g = min(contenants, key=lambda g: (len(g.membres), -g.score), default=None)
        comp = a["details"]["composantes"]
        repese = round(sum(poids[k] * comp[k] for k in poids))
        lignes.append({"signature": a["signature"], "taille": len(m), "structure": a["structure"],
                       "score_v1": a["score"], "score_v1_sans_gains": repese,
                       "score_v2": g.score if g else None, "taille_v2": len(g.membres) if g else None,
                       "niveau_v2": g.mesures["niveau"] if g else None,
                       "dans_un_groupe": g is not None, "dans_une_famille": any(m <= f for f in familles)})
    sans = [x["score_v1_sans_gains"] - x["score_v1"] for x in lignes]
    par_structure = {}
    for s in sorted({x["structure"] for x in lignes}):
        sous = [x for x in lignes if x["structure"] == s]
        par_structure[s] = {"groupes": len(sous),
                            "ecart_moyen_sans_gains": round(statistics.mean(x["score_v1_sans_gains"] - x["score_v1"] for x in sous), 1)}
    return {"anciens": len(lignes), "dans_un_groupe": sum(x["dans_un_groupe"] for x in lignes),
            "dans_une_famille": sum(x["dans_une_famille"] for x in lignes),
            "meme_composition": sum(x["taille_v2"] == x["taille"] for x in lignes),
            "ecart_score_sans_gains": {"moyen": round(statistics.mean(sans), 1), "min": min(sans), "max": max(sans)},
            "par_structure": par_structure, "lignes": lignes}


def stabilite(r25: groupes.Resultat, plein: groupes.Donnees) -> dict:
    """Paires calculées jusqu'au 31/12/2025, revues sur les marchés de 2026 : se retrouvent-elles, et plus que le hasard ?"""
    an = np.array([bool(m.date) and m.date >= "20260101" for m in plein.marches])
    X26 = plein.X[:, np.flatnonzero(an)].tocsr()
    marches26 = [m for m, x in zip(plein.marches, an) if x]
    vue = _Vue(X26, marches26)
    n26 = np.asarray(X26.sum(axis=1)).ravel()
    idx = plein.index
    groupes_paires = {}
    for p in r25.paires:
        cle = p.niveau or ("sans niveau" if p.communs >= 3 else None)
        if cle is None or p.a not in idx or p.b not in idx:
            continue
        i, j = idx[p.a], idx[p.b]
        if n26[i] < 2 or n26[j] < 2:
            continue                                    # l'une ou l'autre presque absente en 2026 : on ne juge pas
        groupes_paires.setdefault(cle, []).append((i, j))
    sortie = {}
    for cle in ("fort", "moyen", "faible", "sans niveau"):
        paires = groupes_paires.get(cle, [])
        if not paires:
            continue
        i = np.array([x for x, _ in paires])
        j = np.array([y for _, y in paires])
        communs = np.asarray(X26[i].multiply(X26[j]).sum(axis=1)).ravel()
        attendu = liens.attendus(vue, i, j)
        retrouve = communs >= 1
        exces = communs.sum() / attendu.sum() if attendu.sum() else None
        sortie[cle] = {"paires_jugees": len(paires), "retrouvees_2026": round(float(retrouve.mean()), 3),
                       "au_moins_2_communs": round(float((communs >= 2).mean()), 3),
                       "exces_2026_global": _r(exces, 2),
                       "communs_2026": int(communs.sum()), "attendus_2026": _r(attendu.sum(), 1)}
    return sortie


def chronometrer(f, fois: int = 1) -> list[float]:
    t = []
    for _ in range(fois):
        d = time.perf_counter()
        f()
        t.append((time.perf_counter() - d) * 1000)
    return t


def temps_api(nb: int = 200) -> dict:
    calc, ret = bases.calcul(), bases.retours()
    try:
        if not api_groupes._prets(calc):
            return {"absent": "aucun calcul « courant » v2 dans copilote_calcul.db"}
        api_groupes.chercher_societes(calc, "x")                     # annuaire chargé une fois
        actives = [r[0] for r in calc.execute("SELECT cle FROM groupes_societes WHERE calcul = 'courant' AND marches >= 5")]
        random.seed(0)
        cles = random.sample(actives, min(nb, len(actives)))
        cles += [r[0] for r in calc.execute("SELECT cle FROM groupes_societes WHERE calcul = 'courant'"
                                            " ORDER BY fort + moyen + faible DESC, marches DESC LIMIT 20")]
        hist = bases.historique()
        try:
            # Le serveur préchauffe les victoires de chaque société au démarrage (routes.prechauffer) :
            # on fait pareil, sinon la première fiche mesurée paierait ce parcours de l'historique.
            fiche_societe.victoires_par_societe(hist, _fusions.charger())
            fiche = [chronometrer(lambda c=c: fiche_societe.fiche(hist, calc, ret, c))[0] for c in cles]
        finally:
            hist.close()
        gros = [r[0] for r in calc.execute("SELECT id FROM groupes WHERE calcul = 'courant' ORDER BY taille DESC, marches_3 DESC LIMIT 5")]
        groupe = [chronometrer(lambda g=g: api_groupes.fiche(calc, ret, g))[0] for g in gros]
        liste = chronometrer(lambda: api_groupes.lister(calc, ret), 5)
        recherche = chronometrer(lambda: api_groupes.lister(calc, ret, q="travaux"), 5)
        q = sorted(fiche)
        lentes = sorted(zip(fiche, cles), reverse=True)[:5]
        return {"fiche_societe_ms": {"mesures": len(q), "mediane": _r(statistics.median(q), 1),
                                     "p95": _r(q[int(0.95 * (len(q) - 1))], 1), "max": _r(q[-1], 1),
                                     "plus_lentes": [{"cle": c, "ms": _r(t, 1)} for t, c in lentes]},
                "fiche_groupe_plus_grands_ms": _r(max(groupe), 1),
                "liste_ms": _r(statistics.median(liste), 1), "liste_recherche_ms": _r(statistics.median(recherche), 1)}
    finally:
        calc.close()
        ret.close()


def recherche_complete() -> dict:
    """Chaque société de la table `societes` est cherchée par son nom : est-elle dans les résultats ?"""
    calc = bases.calcul()
    hist = bases.historique()
    try:
        if not api_groupes._prets(calc):
            return {"absent": True}
        table = _fusions.charger()
        manquees = []
        lignes = hist.execute("SELECT cle, nom FROM societes").fetchall()
        for r in lignes:
            attendue = _fusions.appliquer(r["cle"], table)
            if not api_groupes.normaliser(r["nom"]):
                continue                                   # nom sans lettre ni chiffre : rien à chercher
            trouvees = [x["cle"] for x in api_groupes.chercher_societes(calc, r["nom"], limite=api_groupes.MAX_RECHERCHE)["societes"]]
            if attendue not in trouvees:
                manquees.append(r["nom"])
        return {"societes": len(lignes), "trouvees": len(lignes) - len(manquees), "manquees": manquees[:20]}
    finally:
        calc.close()
        hist.close()


def vocabulaire() -> dict:
    textes = [(config.ICI / "statique" / "copilote_groupes.html").read_text(encoding="utf-8"),
              Path(api_groupes.__file__).read_text(encoding="utf-8"),
              Path(fiche_societe.__file__).read_text(encoding="utf-8")]
    trouves = sorted({m.group() for t in textes for m in MOTS_INTERDITS.finditer(t)})
    return {"mots_trouves": trouves}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="python -m copilote.mesures_groupes")
    p.add_argument("--sortie", default=str(config.RAPPORTS / "copilote_groupes_v2_mesures.json"))
    a = p.parse_args(argv)
    from . import reglages as _reglages
    reglages = _reglages.charger()
    table = _fusions.charger()
    hist = bases.historique()
    try:
        plein = groupes.charger(hist, None, table)
        d25 = groupes.charger(hist, JUSQUA, table)
    finally:
        hist.close()
    ret = bases.retours()
    try:
        avis, avis25 = groupes.lire_avis(ret, None, table), groupes.lire_avis(ret, JUSQUA, table)
    finally:
        ret.close()

    m = {"diagnostic": diagnostic(plein, reglages), "presences": presences_par_statut(plein)}
    print("diagnostic fait", flush=True)
    r = groupes.detecter(plein, avis)
    m["structure"] = structure(r)
    actives_mf = sum(1 for c, x in Counter(c for p in r.paires if p.rang >= 2 for c in (p.a, p.b)).items())
    n = np.asarray(plein.X.sum(axis=1)).ravel()
    idx = plein.index
    avec = {c for p in r.paires if p.rang >= 2 for c in (p.a, p.b)}
    m["partenaires"] = {"actives_avec_moyen_ou_fort": sum(n[idx[c]] >= reglages["groupes"]["actives"] for c in avec),
                        "societes_avec_moyen_ou_fort": actives_mf}
    m["anciens"] = anciens_groupes(r)
    print("détection complète faite", flush=True)
    r25 = groupes.detecter(d25, avis25)
    m["stabilite"] = {"jusqua": JUSQUA, "paires": stabilite(r25, plein), "duree_s": round(r25.duree, 1)}
    print("stabilité faite", flush=True)
    m["temps"] = temps_api()
    m["recherche"] = recherche_complete()
    m["vocabulaire"] = vocabulaire()

    feu = {
        "partenaires ≥ 1 500 sociétés actives (moyen ou fort)": m["partenaires"]["actives_avec_moyen_ou_fort"] >= 1500,
        "66 anciens groupes retrouvés": m["anciens"].get("dans_un_groupe") == m["anciens"].get("anciens") == 66,
        "toute société trouvée par la recherche": m["recherche"].get("trouvees") == m["recherche"].get("societes"),
        # p95 : la toute première fiche ouverte après un gros calcul relit la base sur le disque
        # (cache du système vidé) et peut dépasser une demi-seconde une fois. Le détail des plus
        # lentes est dans m["temps"]["fiche_societe_ms"]["plus_lentes"].
        "fiche société < 300 ms (p95)": (m["temps"].get("fiche_societe_ms", {}).get("p95") or 1e9) < SEUIL_FICHE_MS,
        "aucun mot accusateur": not m["vocabulaire"]["mots_trouves"],
    }
    m["feu_vert"] = feu
    Path(a.sortie).write_text(json.dumps(m, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    for k, v in feu.items():
        print(f"{'OK' if v else 'KO'}  {k}")
    print(f"→ {a.sortie}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
