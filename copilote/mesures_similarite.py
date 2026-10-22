"""Backtest du moteur de marchés similaires (étape 4).

    python -m copilote.mesures_similarite [--annee 2026] [--limite N]

Question : la médiane du prix de référence (en taux) des 20 marchés les plus similaires ANTÉRIEURS prédit-elle
le prix de référence d'un marché de l'année testée mieux que la médiane globale ?

Strict, sans fuite :
- pour un marché du jour d, seuls les marchés datés avant d (jour exclu) sont candidats ;
- le vocabulaire et l'idf de l'index sont ajustés sur les marchés d'avant l'année testée seulement ;
- aucune donnée de groupes ou de profils n'entre dans ce score.
Le code qui note est celui de l'API (similarite.Moteur) : on mesure ce qu'on livre.

Lecture seule sur pv_travaux.db ; l'index « jusqua-… » est rangé dans copilote_calcul.db (recalculable).
"""
from __future__ import annotations

import json
import sys
import time
from contextlib import closing

import numpy as np

from . import acheteurs, bases, config, nature, reglages, similarite
from .donnees import tranche_de
from .similarite import CRITERES, Moteur, cible_depuis_texte

MIN_COMPARABLES = 5        # en dessous, le moteur n'a pas assez de voisins : on retombe sur la médiane globale


def cible_du_marche(m: Moteur, i: int) -> similarite.Cible:
    """Un marché du corpus pris pour cible : tout ce que l'AO aurait su de lui, rien de son issue."""
    c = m.corpus
    u = c.unites[i]
    return cible_depuis_texte(c.fiches[i].texte, u.acheteur or "", u.estimation, c.fiches[i].quals, int(c.jour[i]),
                              acheteur_cles=[c.acheteur_cle[i]], ref=u.ref, lot=u.lot)


def _erreurs(pred: np.ndarray, vrai: np.ndarray) -> dict:
    e = np.abs(pred - vrai)
    return {"mae": float(e.mean() * 100), "mediane": float(np.median(e) * 100),
            "dans_2_points": float((e <= 0.02).mean()), "n": int(len(e))}


def _bootstrap_diff(err_a: np.ndarray, err_b: np.ndarray, tirages: int = 2000, graine: int = 0) -> tuple[float, float]:
    """Intervalle à 95 % de la différence des MAE (a - b), en points de taux, par rééchantillonnage des marchés."""
    rng = np.random.default_rng(graine)
    d = (err_a - err_b) * 100
    moyennes = np.empty(tirages)
    for t in range(tirages):
        moyennes[t] = d[rng.integers(0, len(d), len(d))].mean()
    return float(np.percentile(moyennes, 2.5)), float(np.percentile(moyennes, 97.5))


def backtest(annee: str = "2026", limite: int | None = None, n_mediane: int | None = None,
             progression: bool = True) -> dict:
    r0 = reglages.charger()
    n_med = n_mediane or r0["pour_mediane"]
    debut_annee = similarite.jour_de(f"{annee}-01-01")
    with closing(bases.calcul()) as calc:
        m = similarite.moteur(bases.historique, bases.chemin_historique(), calc, f"jusqua-{int(annee) - 1}-12-31",
                              jour_limite=debut_annee, sauver=True)
    c = m.corpus
    cibles = [i for i in range(len(c)) if c.jour[i] >= debut_annee and c.jour[i] < similarite.jour_de(f"{int(annee) + 1}-01-01")]
    if limite:
        cibles = cibles[:limite]

    jeux = {nom: (r0["poids"] if p is None else p) for nom, p in reglages.JEUX.items()}
    variantes_filtre = ("code", "domaine", "aucun")
    ks = (10, n_med, 50)
    verite, globale, famille, recents, acheteur_med = [], [], [], [], []
    pred = {nom: [] for nom in jeux}
    pred_k = {k: [] for k in ks}
    pred_filtre = {v: [] for v in variantes_filtre}
    comparables, violations, tranches, avec_acheteur = [], 0, [], []
    t0 = time.time()
    # Tableau des marchés par famille, pour la base « famille ».
    for pas, i in enumerate(cibles):
        k = c.avant(int(c.jour[i]))
        if k < 50:
            continue
        cible = cible_du_marche(m, i)
        ref_k = c.ref_taux[:k]
        mediane_globale = float(np.median(ref_k))
        cos = m.index.similarites(cible.texte_index, k)
        comp = m.composantes(cible, r0, cos=cos)
        comparables.append(int(comp.masque.sum()))

        def mediane_des(notes, n):
            idx = m.meilleurs(notes, n)
            return idx, (float(np.median(ref_k[idx])) if len(idx) >= MIN_COMPARABLES else None)

        premiers = None
        for nom, poids in jeux.items():
            notes = m.noter(comp, poids)
            idx, med = mediane_des(notes, n_med)
            if len(idx) and int(c.jour[idx].max()) >= int(c.jour[i]):
                violations += 1
            pred[nom].append(med if med is not None else mediane_globale)
            if nom == "defaut":
                premiers = notes
        for kk in ks:
            _, med = mediane_des(premiers, kk)
            pred_k[kk].append(med if med is not None else mediane_globale)
        for v in variantes_filtre:
            rv = {**r0, "filtre": {**r0["filtre"], "qualification": v}}
            cv = m.composantes(cible, rv, cos=cos)
            _, med = mediane_des(m.noter(cv, r0["poids"]), n_med)
            pred_filtre[v].append(med if med is not None else mediane_globale)

        # bases simples
        verite.append(float(c.ref_taux[i]))
        globale.append(mediane_globale)
        meme_fam = np.flatnonzero(m.fam_i[:k] == m._fi.get(cible.famille, -2)) if cible.famille else []
        famille.append(float(np.median(ref_k[meme_fam[-n_med:]])) if len(meme_fam) >= MIN_COMPARABLES else mediane_globale)
        recents.append(float(np.median(ref_k[-n_med:])))
        memes = np.flatnonzero(m.ach_i[:k] == m.acheteurs.get(c.acheteur_cle[i], -1))
        avec_acheteur.append(len(memes) >= 3)
        acheteur_med.append(float(np.median(ref_k[memes[-n_med:]])) if len(memes) >= 3 else mediane_globale)
        tranches.append(tranche_de(int(c.nb_offres[i])))
        if progression and pas % 1000 == 0:
            print(f"  … {pas}/{len(cibles)} marchés, {time.time() - t0:.0f} s", flush=True)

    vrai = np.array(verite)
    methodes = {"globale": np.array(globale), "famille_20": np.array(famille), "recents_20": np.array(recents),
                "acheteur_20": np.array(acheteur_med)}
    methodes.update({f"sim_{nom}": np.array(p) for nom, p in pred.items()})
    resultats = {nom: _erreurs(p, vrai) for nom, p in methodes.items()}
    err = {nom: np.abs(p - vrai) for nom, p in methodes.items()}
    comparaisons = {}
    for nom in [n for n in methodes if n.startswith("sim_")]:
        for base in ("globale", "famille_20"):
            a, b = err[nom], err[base]
            diff = a - b
            bas, haut = _bootstrap_diff(a, b)
            comparaisons[f"{nom} contre {base}"] = {
                "difference_mae_points": float(diff.mean() * 100), "ic95": [bas, haut],
                "victoires": float((diff < -1e-9).mean()), "egalites": float((np.abs(diff) <= 1e-9).mean()),
                "defaites": float((diff > 1e-9).mean())}
    par_tranche = {}
    for t in ("1-2", "3-4", "5-7", "8+"):
        s = np.array([x == t for x in tranches])
        if s.sum():
            par_tranche[t] = {"n": int(s.sum()), "globale": float(err["globale"][s].mean() * 100),
                              "sim_defaut": float(err["sim_defaut"][s].mean() * 100)}
    s = np.array(avec_acheteur)
    par_acheteur = {}
    for nom, masque in (("acheteur connu (≥3 marchés antérieurs)", s), ("acheteur peu ou pas connu", ~s)):
        if masque.sum():
            par_acheteur[nom] = {"n": int(masque.sum()), "globale": float(err["globale"][masque].mean() * 100),
                                 "sim_defaut": float(err["sim_defaut"][masque].mean() * 100)}
    sensibilite = {
        "k": {str(kk): _erreurs(np.array(p), vrai) for kk, p in pred_k.items()},
        "filtre_qualification": {v: _erreurs(np.array(p), vrai) for v, p in pred_filtre.items()},
    }
    comp_arr = np.array(comparables)
    return {
        "annee": annee, "marches_testes": int(len(vrai)), "mediane_sur": n_med,
        "index": {"calcul": m.calcul, "origine": m.origine, "documents": int(m.index.matrice.shape[0]),
                  "traits": int(m.index.matrice.shape[1])},
        "fuites": {"candidats_non_anterieurs": violations,
                   "idf_ajuste_sur": int(c.avant(debut_annee)), "corpus_total": len(c)},
        "comparables": {"mediane": float(np.median(comp_arr)), "min": int(comp_arr.min()),
                        "moins_de_5": int((comp_arr < MIN_COMPARABLES).sum()),
                        "moins_de_20": int((comp_arr < n_med).sum())},
        "jeux_de_poids": {n: {k: float(v) for k, v in p.items()} for n, p in jeux.items()},
        "resultats": resultats, "comparaisons": comparaisons, "par_tranche": par_tranche,
        "par_acheteur": par_acheteur, "sensibilite": sensibilite,
        "duree_s": round(time.time() - t0, 1)}


def controles(annee: str = "2026", n: int = 300) -> dict:
    """Contrôles indépendants du score : filtre, antériorité, temps de rechargement de l'index.

    Le filtre est revérifié à la main sur les dictionnaires bruts (pas sur les tableaux numpy du moteur) :
    pour `n` marchés tirés au hasard, chacun des 50 premiers résultats doit être antérieur, avoir un
    montant entre 0,3× et 3×, et partager un code de qualification quand les deux côtés en ont.
    """
    r0 = reglages.charger()
    t = time.time()
    with closing(bases.calcul()) as calc:
        similarite.oublier()
        m = similarite.moteur(bases.historique, bases.chemin_historique(), calc, "courant")
    chargement = time.time() - t
    # temps pour refaire l'index depuis les textes, à comparer au rechargement
    from . import index_texte
    t = time.time()
    index_texte.construire(m.corpus.texte)
    calcul_index = time.time() - t
    with closing(bases.calcul()) as calc:
        t = time.time()
        index_texte.charger(calc, "courant", m.corpus.signature())
        rechargement = time.time() - t
        taille = calc.execute("SELECT length(matrice) + length(vocabulaire) + length(idf) FROM sim_index"
                              " WHERE calcul = 'courant'").fetchone()[0]
    c = m.corpus
    debut = similarite.jour_de(f"{annee}-01-01")
    fin = similarite.jour_de(f"{int(annee) + 1}-01-01")
    cibles = [i for i in range(len(c)) if debut <= c.jour[i] < fin]
    rng = np.random.default_rng(0)
    tirage = rng.choice(cibles, size=min(n, len(cibles)), replace=False)
    total = violations = vides = 0
    t = time.time()
    for i in tirage:
        cible = cible_du_marche(m, int(i))
        comp = m.composantes(cible, r0)
        idx = m.meilleurs(m.noter(comp, r0["poids"]), r0["resultats"])
        vides += len(idx) == 0
        for j in idx:
            total += 1
            ratio = c.estimation[j] / cible.estimation
            codes_j, codes_c = set(c.fiches[j].quals), set(cible.quals)
            ok = (c.jour[j] < cible.jour and r0["filtre"]["montant_min"] <= ratio <= r0["filtre"]["montant_max"]
                  and (not codes_c or not codes_j or bool(codes_j & codes_c)))
            violations += not ok
    latence = (time.time() - t) / len(tirage)
    return {"corpus": len(c), "mise_en_memoire_s": round(chargement, 2), "index_calcul_s": round(calcul_index, 2),
            "index_rechargement_s": round(rechargement, 2), "index_taille_mo": round(taille / 1e6, 1),
            "resultats_verifies": total, "violations_du_filtre": violations, "cibles_sans_resultat": int(vides),
            "cibles": len(tirage), "latence_moyenne_par_marche_s": round(latence, 3)}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if "--controles" in argv:
        print(json.dumps(controles(), ensure_ascii=False, indent=1))
        return 0
    annee = argv[argv.index("--annee") + 1] if "--annee" in argv else "2026"
    limite = int(argv[argv.index("--limite") + 1]) if "--limite" in argv else None
    r = backtest(annee, limite)
    config.RAPPORTS.mkdir(exist_ok=True)
    chemin = config.RAPPORTS / f"copilote_etape_4_backtest_{annee}.json"
    chemin.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in r.items() if k != "comparaisons"}, ensure_ascii=False, indent=1))
    print(json.dumps(r["comparaisons"], ensure_ascii=False, indent=1))
    print(f"→ {chemin}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
