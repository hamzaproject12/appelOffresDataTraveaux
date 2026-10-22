"""Mesures de l'étape 2 : groupes de présence commune, seuils, scores, et critères FEU VERT.

    python -m copilote.mesures_groupes

Aucune écriture : tout est calculé en mémoire (recalculer.py se charge d'écrire).

Critères FEU VERT, fixés avant la mesure :
  stabilité   groupes calculés jusqu'au 31/12/2025 : ≥ 50 % présents à ≥ 3 membres sur au moins un
              marché de 2026, et au moins 3 fois la part obtenue par des ensembles semblables tirés
              au hasard ;
  hasard      top 50 : rapport au hasard médian ≥ 3 et ≥ 90 % des groupes à p ≤ 0,05 ;
  grands      aucun groupe formé uniquement des 20 sociétés les plus actives ;
  taille      entre 20 et 500 groupes, taille médiane entre 3 et 7, ≥ 90 % des groupes de 3 à 7.
"""
from __future__ import annotations

import statistics
import sys
from collections import Counter

import numpy as np

from . import bases, groupes
from . import fusions as _fusions

JUSQUA = "2025-12-31"
TIRAGES_STABILITE = 50


def pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f} %"


def ok(v: bool) -> str:
    return "OK" if v else "KO"


def reapparus(g_membres, plein: groupes.Donnees, an: np.ndarray, minimum: int = 1) -> bool:
    idx = plein.index
    S = [idx[c] for c in g_membres if c in idx]
    if len(S) < groupes.MIN_PRESENTS:
        return False
    pres = np.asarray(plein.X[S].sum(axis=0)).ravel()
    return int(((pres >= groupes.MIN_PRESENTS) & an).sum()) >= minimum


def stabilite(r25: groupes.Resultat, plein: groupes.Donnees) -> dict:
    """Part des groupes de 2025 revus en 2026, et la même part pour des ensembles semblables."""
    an = np.array([m.date[:4] == "2026" for m in plein.marches])
    d25 = r25.donnees
    pr = groupes.Profils(d25)
    rng = np.random.default_rng(groupes.GRAINE)
    obs = [reapparus(g.membres, plein, an) for g in r25.groupes]
    obs3 = [reapparus(g.membres, plein, an, 3) for g in r25.groupes]
    hasard = []
    for g in r25.groupes:
        m = g.mesures
        S = [d25.index[c] for c in g.membres]
        dates = [m["premier"], m["dernier"]] if m["premier"] else []
        tire = groupes.tirer_semblables(S, {p["acheteur"] for p in m["preuves"]}, dates, pr, rng,
                                        TIRAGES_STABILITE)
        if tire is None:
            continue
        D, _ = tire
        for ligne in range(D.shape[0]):
            cles = [d25.societes[i] for i in D[ligne].indices]
            hasard.append(reapparus(cles, plein, an))
    top = r25.groupes[:20]
    return {"n": len(obs), "part": sum(obs) / len(obs) if obs else 0.0,
            "part_3": sum(obs3) / len(obs3) if obs3 else 0.0,
            "part_top20": sum(obs[:len(top)]) / len(top) if top else 0.0,
            "hasard": sum(hasard) / len(hasard) if hasard else 0.0, "n_hasard": len(hasard)}


def resume(r: groupes.Resultat) -> dict:
    tailles = [g.mesures["taille"] for g in r.groupes]
    top = r.groupes[:50]
    return {
        "examinees": len(r.paires), "retenues": sum(p.retenue for p in r.paires),
        "groupes": len(r.groupes), "societes": len({c for g in r.groupes for c in g.membres}),
        "familles": len(r.familles),
        "mediane": statistics.median(tailles) if tailles else None, "max": max(tailles, default=0),
        "part_3_7": sum(3 <= t <= 7 for t in tailles) / len(tailles) if tailles else 0.0,
        "score_median": statistics.median([g.score for g in r.groupes]) if r.groupes else None,
        "top50_rapport": statistics.median([g.mesures["rapport_hasard"] for g in top]) if top else None,
        "top50_p": sum(g.mesures["p_hasard"] <= 0.05 for g in top) / len(top) if top else 0.0,
    }


def nom_court(d: groupes.Donnees, cle: str, n: int = 26) -> str:
    nom = d.noms.get(cle, cle)
    return nom if len(nom) <= n else nom[:n - 1] + "…"


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    table_fusions = _fusions.charger()
    hist = bases.historique()
    try:
        plein = groupes.charger(hist, None, table_fusions)
        d25 = groupes.charger(hist, JUSQUA, table_fusions)
        sans_fusion = groupes.charger(hist, None, {})
    finally:
        hist.close()
    ret = bases.retours()
    try:
        avis = groupes.lire_avis(ret, None, table_fusions)
        avis25 = groupes.lire_avis(ret, JUSQUA, table_fusions)
    finally:
        ret.close()

    regions = sum(m.region is not None for m in plein.marches)
    domaines = sum(bool(m.domaines) for m in plein.marches)
    print("## Données\n")
    print(f"{len(plein.marches)} marchés, {len(plein.societes)} sociétés, {plein.X.nnz} présences "
          f"(société × marché). Fusions : {len(table_fusions)} appliquées "
          f"({len(sans_fusion.societes) - len(plein.societes)} sociétés en moins).")
    print(f"Région trouvée pour {pct(regions / len(plein.marches))} des marchés ; "
          f"domaine (lettre de qualification) pour {pct(domaines / len(plein.marches))}.")

    print("\n## Seuils\n")
    print("| seuil | paires ≥ 5 marchés | retenues | groupes | sociétés | familles | taille méd. | max |"
          " 3–7 | score méd. | top 50 : rapport méd. | top 50 : p ≤ 0,05 |")
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    resultats = {}
    for s in (0.30, 0.50, 0.70):
        r = resultats[s] = groupes.detecter(plein, s, avis)
        x = resume(r)
        print(f"| {s:.0%} | {x['examinees']} | {x['retenues']} | {x['groupes']} | {x['societes']} | "
              f"{x['familles']} | {x['mediane']} | {x['max']} | {pct(x['part_3_7'])} | "
              f"{x['score_median']} | {x['top50_rapport']:.1f} | {pct(x['top50_p'])} |")

    r = resultats[groupes.SEUIL]
    print("\n## Distribution des scores (seuil 50 %)\n")
    print("| score | groupes |")
    print("|---|---:|")
    for bas in range(0, 100, 10):
        n = sum(bas <= g.score < bas + 10 or (bas == 90 and g.score == 100) for g in r.groupes)
        print(f"| {bas}–{bas + 9 if bas < 90 else 100} | {n} |")
    moy = {k: statistics.mean(g.composantes[k] for g in r.groupes) for k in groupes.POIDS}
    print("\nComposantes moyennes (0–1) : " + ", ".join(f"{k} {v:.2f}" for k, v in moy.items()))
    print("Structure des victoires : " + ", ".join(
        f"{k} {v}" for k, v in Counter(g.mesures["structure"] for g in r.groupes).most_common()))

    print("\n## Top 20 (seuil 50 %)\n")
    print("| # | score | famille | membres | marchés ≥3 / complet | acheteurs | régions | domaines |"
          " victoires | rapport au hasard (p) |")
    print("|---:|---:|---:|---|---:|---:|---:|---|---|---:|")
    for n, g in enumerate(r.groupes[:20], 1):
        m = g.mesures
        membres = ", ".join(nom_court(plein, c) for c in g.membres)
        if m["structure"] == "peu de victoires":
            vic = f"peu ({m['gains']})"
        else:
            vic = (f"{m['structure']} — {nom_court(plein, m['chef_de_file'], 20)} "
                   f"{pct(m['part_chef'])} de {m['gains']}")
        print(f"| {n} | {g.score} | {g.famille} | {membres} | {m['marches_3']} / {m['marches_complet']} | "
              f"{m['acheteurs']} | {m['regions']} | {m['domaines'] or '—'} | {vic} | "
              f"{m['rapport_hasard']:.1f} ({m['p_hasard']:.3f}) |")

    print("\n## Position des offres (top 20, médianes en points de taux)\n")
    print("| # | n | membre non gagnant − gagnant | autres − gagnant | membre − réf. | autres − réf. |"
          " gagne avec / sans le groupe (chef de file) |")
    print("|---:|---:|---:|---:|---:|---:|---|")
    for n, g in enumerate(r.groupes[:20], 1):
        p = g.mesures["position"]
        f = lambda v: "—" if v is None else f"{100 * v:+.1f}"            # noqa: E731
        chef = next((x for x in g.mesures["membres"] if x["cle"] == g.mesures["chef_de_file"]), None)
        av = (f"{pct(chef['taux_avec'])} ({chef['n_avec']}) / {pct(chef['taux_sans'])} ({chef['n_sans']})"
              if chef else "—")
        print(f"| {n} | {p['n_membres']} | {f(p['membres_gagnant'])} | {f(p['autres_gagnant'])} | "
              f"{f(p['membres_reference'])} | {f(p['autres_reference'])} | {av} |")

    print("\n## Avis\n")
    if avis:
        a = groupes.accord(groupes.detecter(plein, groupes.SEUIL, {}), avis)
        print(f"{a['avis']} avis tranchés ; accord score/avis {pct(a['accord'])} "
              f"(oui & score ≥ 50 : {a['oui_score_haut']}, non & score < 50 : {a['non_score_bas']}).")
    else:
        print("Aucun avis enregistré : accord non mesurable.")

    # FEU VERT
    r25 = groupes.detecter(d25, groupes.SEUIL, avis25)
    st = stabilite(r25, plein)
    n_act = np.asarray(plein.X.sum(axis=1)).ravel()
    top20 = {plein.societes[i] for i in np.argsort(-n_act, kind="stable")[:20]}
    seuls_top = sum(set(g.membres) <= top20 for g in r.groupes)
    avec_top = sum(bool(set(g.membres) & top20) for g in r.groupes)
    x = resume(r)
    print("\n## FEU VERT\n")
    print(f"Stabilité : {st['n']} groupes calculés jusqu'au {JUSQUA} ; revus en 2026 (≥ 3 membres sur "
          f"≥ 1 marché) {pct(st['part'])}, sur ≥ 3 marchés {pct(st['part_3'])}, top 20 {pct(st['part_top20'])} ; "
          f"ensembles semblables tirés au hasard {pct(st['hasard'])} (n = {st['n_hasard']}).")
    print(f"20 sociétés les plus actives : ≥ {int(n_act[np.argsort(-n_act)[19]])} marchés chacune ; "
          f"{avec_top} groupes en contiennent au moins une.")
    lignes = [
        ("Stabilité : groupes 2025 revus en 2026 (≥ 50 % et ≥ 3 × hasard)",
         f"{pct(st['part'])} (hasard {pct(st['hasard'])})",
         st["part"] >= 0.5 and st["part"] >= 3 * st["hasard"]),
        ("Top 50 au-dessus du hasard (rapport médian ≥ 3, ≥ 90 % à p ≤ 0,05)",
         f"rapport {x['top50_rapport']:.1f}, {pct(x['top50_p'])}",
         x["top50_rapport"] >= 3 and x["top50_p"] >= 0.9),
        ("Aucun groupe formé uniquement des 20 plus actives", f"{seuls_top} groupe(s)", seuls_top == 0),
        ("Nombre de groupes (20–500) et taille (médiane 3–7, ≥ 90 % de 3 à 7)",
         f"{x['groupes']} groupes, médiane {x['mediane']}, {pct(x['part_3_7'])} de 3 à 7, max {x['max']}",
         20 <= x["groupes"] <= 500 and 3 <= x["mediane"] <= 7 and x["part_3_7"] >= 0.9),
    ]
    print("\n| critère | valeur mesurée | |")
    print("|---|---|---|")
    for critere, valeur, v in lignes:
        print(f"| {critere} | {valeur} | **{ok(v)}** |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
