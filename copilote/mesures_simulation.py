"""Étape C : réglage des taux (2025) et backtest de la simulation (2026, AO REJEU, REJEU-R).

    python -m copilote.mesures_simulation ajuster [--n 300]     réglages des taux → copilote/simulation.json
    python -m copilote.mesures_simulation mesurer [--n 500]     500 marchés de 2026 (graine 1, ceux de A2)
    python -m copilote.mesures_simulation rejeux                les 20 AO REJEU (par l'API interne)
    python -m copilote.mesures_simulation tout                  mesurer + rejeux → rapports/copilote_etape_C_mesures.json

Sans fuite : pour un marché du jour d, l'historique visible s'arrête la veille (Memoire.coupe), les similaires
sont datés avant d, l'idf de l'index n'a vu que les années d'avant, les réglages viennent de l'année d'avant.
L'effet de marché est mesuré sur les unités d'avant le début du mois de la coupure.

Mesures (la vérité : les offres réellement déposées, jamais vues par le moteur) :
  a) le vrai prix de référence (sans notre offre) dans l'intervalle 80 % simulé ;
  b) l'écart absolu de la médiane simulée au vrai prix de référence, comparé à la méthode de l'étape 4
     (médiane des prix de référence des 20 marchés les plus similaires) sur les mêmes marchés ;
  c) calibration : la chance annoncée pour x = 0,85 … 1,10 et au point recommandé, contre le résultat réel de
     notre offre fictive insérée parmi les vraies offres (donnees.rejouer_attribution, règle de construire_base) ;
  d) victoires au point recommandé, contre le meilleur taux fixe de chaque tranche de nombre d'offres (choisi
     sur ces mêmes marchés : la base simple est avantagée).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from contextlib import closing

import numpy as np

from . import analyse, bases, config, mesures_probables as mp, probables, reglages, rejeu, simulateur, taux
from .donnees import CLE_FICTIVE, TRANCHES, rejouer_attribution, tranche_de

SORTIE = config.RAPPORTS / "copilote_etape_C_mesures.json"
X_CALIBRATION = (0.85, 0.90, 0.95, 1.00, 1.05, 1.10)
FIXES = [round((3200 + 10 * k) / 4000, 4) for k in range(161)]      # 0,80 à 1,20 par 0,0025
N_ETAPE4 = 20
MIN_COMPARABLES = 5


# --------------------------------------------------------------------------- un marché du backtest

def _contexte(m, mem, i, r0):
    c = m.corpus
    cible = mp.cible_du_marche(m, i)
    comp = m.composantes(cible, r0)
    notes = m.noter(comp, r0["poids"])
    retenus = m.meilleurs(notes, mp.N_SIMILAIRES)
    ctx = analyse.contexte(mem, mp._veille(c.unites[i].date), int(c.jour[i]), cible, m, notes, comp, retenus)
    return ctx, comp, notes


def etape4(m, notes, k: int) -> float:
    """La méthode de l'étape 4 : médiane des prix de référence (taux) des 20 plus similaires, sinon médiane
    globale des marchés antérieurs."""
    ref_k = m.corpus.ref_taux[:k]
    idx = m.meilleurs(notes, N_ETAPE4)
    if len(idx) >= MIN_COMPARABLES:
        return float(np.median(ref_k[idx]))
    return float(np.median(ref_k))


def dossier_leger(mem, ctx, comp, m, r0, coefs, ref: str, lot, estimation, rtaux=None) -> dict:
    """Ce dont simulateur.modele a besoin, sans passer par l'API (mêmes fonctions que analyse.analyser)."""
    a = probables.analyser(mem, ctx, coefs)
    tx = taux.calculer(mem, ctx, a["candidats"], a["groupes"], moteur=m, composantes=comp, poids=r0["poids"],
                       reglages=rtaux)
    cands = [{"cle": c["cle"], "nom": c.get("nom") or c["cle"], "p": round(float(c["p"]), 4),
              "eviction": {"p": round(float(c["eviction"]["p"]), 4), "global": round(float(c["eviction"]["global"]), 4)}}
             for c in a["candidats"]]
    conc = a["concurrents"]
    return {"ao": {"ref_annonce": ref, "lot": lot, "estimation": estimation},
            "probables": {"candidats": cands},
            "concurrents": {"decalage": round(float(conc["decalage"]), 4), "dispersion": round(float(conc["dispersion"]), 4),
                            "lambda": round(float(conc["lambda"]), 4)},
            "taux": analyse.rendre_taux(tx)}


def verite_unite(u) -> dict | None:
    """Vrai prix de référence (taux) et, pour un marché attribué, notre offre fictive à chaque x."""
    a = rejouer_attribution(u)
    if not u.estimation or a.reference is None:
        return None
    v = {"p": a.reference / u.estimation, "nb_offres": a.nb_offres, "tranche": tranche_de(a.nb_offres),
         "attribue": bool(u.cle_gagnant)}
    if v["attribue"]:
        v["gagne"] = lambda x: rejouer_attribution(u, x).cle_gagnant == CLE_FICTIVE
    return v


def mesurer_un(sortie: dict, v: dict, base4: float | None) -> dict:
    pr = sortie["prix_reference"]
    ligne = {"p_vrai": v["p"], "p_mediane": pr["mediane"], "q10": pr["q10"], "q90": pr["q90"],
             "dans80": pr["q10"] is not None and pr["q10"] <= v["p"] <= pr["q90"],
             "erreur": None if pr["mediane"] is None else abs(pr["mediane"] - v["p"]),
             "erreur_etape4": None if base4 is None else abs(base4 - v["p"]),
             "nb_offres": v["nb_offres"], "tranche": v["tranche"], "offres_simulees": sortie["offres_moyennes"],
             "zone": sortie["zone"], "attribue": v["attribue"]}
    if v["attribue"]:
        xs = sortie["grille"]
        cal = []
        for x in X_CALIBRATION:
            j = int(round((x - xs[0]) / (xs[1] - xs[0])))
            cal.append((x, sortie["chance"][j], v["gagne"](x)))
        z = sortie["zone"]
        if z:
            ligne["x_rec"] = z["x"]
            ligne["chance_rec"] = z["chance"]
            ligne["gagne_rec"] = v["gagne"](z["x"])
            cal.append(("rec", z["chance"], ligne["gagne_rec"]))
        ligne["calibration"] = cal
        ligne["fixes"] = [v["gagne"](x) for x in FIXES]
        ligne["courbe"] = sortie["chance"]                                   # pour les essais hors ligne
        ligne["gagne_grille"] = [v["gagne"](x) for x in xs]
    return ligne


# --------------------------------------------------------------------------- résumé

def _pct(v):
    return None if v is None else round(float(v), 4)


def resumer(lignes: list[dict]) -> dict:
    ok = [l for l in lignes if l["p_mediane"] is not None]
    att = [l for l in ok if l["attribue"] and "calibration" in l]
    sortie = {
        "n": len(lignes), "avec_reference": len(ok), "attribues": len(att),
        "dans80": _pct(np.mean([l["dans80"] for l in ok])) if ok else None,
        "sous80": _pct(np.mean([l["p_vrai"] < l["q10"] for l in ok])) if ok else None,
        "sur80": _pct(np.mean([l["p_vrai"] > l["q90"] for l in ok])) if ok else None,
        "largeur80": _pct(np.mean([l["q90"] - l["q10"] for l in ok])) if ok else None,
        "mae": _pct(np.mean([l["erreur"] for l in ok])) if ok else None,
        "erreur_mediane": _pct(np.median([l["erreur"] for l in ok])) if ok else None,
        "mae_etape4": _pct(np.mean([l["erreur_etape4"] for l in ok if l["erreur_etape4"] is not None])) if ok else None,
        "erreur_mediane_etape4": _pct(np.median([l["erreur_etape4"] for l in ok if l["erreur_etape4"] is not None])) if ok else None,
    }
    # c) calibration par tranche de 10 points de chance annoncée
    cal = [(c, g) for l in att for _, c, g in l["calibration"]]
    blocs = []
    for k in range(10):
        de, a = k / 10, (k + 1) / 10
        x = [(c, g) for c, g in cal if de <= c < a or (k == 9 and c == 1.0)]
        if x:
            blocs.append({"de": de, "a": a, "n": len(x), "annoncee": _pct(np.mean([c for c, _ in x])),
                          "reelle": _pct(np.mean([g for _, g in x])),
                          "ecart_points": round(100 * (np.mean([c for c, _ in x]) - np.mean([g for _, g in x])), 1)})
    sortie["calibration"] = blocs
    rec = [(c, g) for l in att for x, c, g in l["calibration"] if x == "rec"]
    sortie["calibration_rec"] = {"n": len(rec), "annoncee": _pct(np.mean([c for c, _ in rec])) if rec else None,
                                 "reelle": _pct(np.mean([g for _, g in rec])) if rec else None}
    # d) zone recommandée contre taux fixes, par tranche
    tranches = {}
    for nom, _, _ in TRANCHES + [("tout", 0, 0)]:
        sous = [l for l in att if "gagne_rec" in l and (nom == "tout" or l["tranche"] == nom)]
        if not sous:
            tranches[nom] = {"n": 0}
            continue
        fix = np.mean([l["fixes"] for l in sous], axis=0)
        jb = int(np.argmax(fix))
        tranches[nom] = {"n": len(sous), "zone": _pct(np.mean([l["gagne_rec"] for l in sous])),
                         "chance_annoncee": _pct(np.mean([l["chance_rec"] for l in sous])),
                         "x_rec_median": _pct(np.median([l["x_rec"] for l in sous])),
                         "meilleur_fixe": FIXES[jb], "victoires_meilleur_fixe": _pct(fix[jb]),
                         "fixe_095": _pct(fix[FIXES.index(0.95)])}
    sortie["zone_contre_fixes"] = tranches
    return sortie


def _rendu(lignes: list[dict]) -> dict:
    return {"resume": resumer(lignes),
            "lignes": [{k: v for k, v in l.items() if k not in ("fixes", "courbe", "gagne_grille")} for l in lignes]}


def garder_courbes(chemin: str, **jeux) -> None:
    """Les courbes et les victoires réelles sur toute la grille (essais de règles de zone sans tout relancer)."""
    import pickle
    with open(chemin, "wb") as f:
        pickle.dump({nom: [{k: l.get(k) for k in ("courbe", "gagne_grille", "tranche", "nb_offres", "identifiant")}
                           for l in lignes if "courbe" in l] for nom, lignes in jeux.items()}, f)


# --------------------------------------------------------------------------- backtest

def mesurer(annee: str = "2026", n: int = 500, graine: int = 1, rtaux: dict | None = None,
            coefs: dict | None = None, scenarios: int = simulateur.SCENARIOS, rsim: dict | None = None) -> dict:
    r0 = reglages.charger()
    coefs = coefs or probables.charger_coefficients()
    m = mp.moteur_annee(annee, sauver=True)
    mem = probables.memoire(bases.historique, bases.chemin_historique())
    lignes = []
    t0 = time.time()
    for k, i in enumerate(mp.cibles(m, annee, n, graine)):
        u = m.corpus.unites[i]
        v = verite_unite(u)
        if v is None:
            continue
        ctx, comp, notes = _contexte(m, mem, i, r0)
        d = dossier_leger(mem, ctx, comp, m, r0, coefs, u.ref, u.lot, u.estimation, rtaux)
        mo = simulateur.modele(d, scenarios=scenarios, reglages=rsim)
        s = simulateur.simuler(mo, central=False)
        x = mesurer_un(s, v, etape4(m, notes, m.corpus.avant(int(m.corpus.jour[i]))))
        x.update(identifiant=u.identifiant, groupes=len(mo["groupes"]), societes=len(mo["societes"]))
        lignes.append(x)
        if k % 25 == 0:
            print(f"  mesurer {k} ({time.time() - t0:.0f} s)", flush=True)
    return {"annee": annee, "graine": graine, "duree_s": round(time.time() - t0), **_rendu(lignes), "_lignes": lignes}


def rejeux(prefixe: str = "REJEU-", exclure: str | None = "REJEU-R", ouvrir_ao=None,
           scenarios: int = simulateur.SCENARIOS) -> dict:
    """Les AO rejoués, par l'API interne (analyse.analyser) : la même chose que la page."""
    from . import verite_rejeu
    mem = probables.memoire(bases.historique, bases.chemin_historique())
    par_id = {u.identifiant: u for u in mem.unites}
    lignes, erreurs = [], []
    with closing(bases.calcul()) as calc, closing(bases.retours()) as ret, \
            closing((ouvrir_ao or bases.ao_ouverts)()) as ao:
        refs = [r[0] for r in calc.execute("SELECT ref_annonce FROM rejeu_ao ORDER BY ref_annonce")
                if r[0].startswith(prefixe) and not (exclure and r[0].startswith(exclure))]
        for ref in refs:
            lots = [r[0] for r in ao.execute("SELECT lot FROM appels_lots WHERE ref_annonce = ? ORDER BY rowid", (ref,))]
            for lot in lots or [None]:
                vr = verite_rejeu.pour_lot(calc, ref, lot)
                u = None if vr is None else par_id.get(
                    vr["ref_reel"] if not vr["lot"] else f"{vr['ref_reel']}#{vr['lot']}")
                v = None if u is None else verite_unite(u)
                if v is None:
                    erreurs.append({"ref": ref, "lot": lot, "erreur": "vérité absente ou sans prix de référence"})
                    continue
                try:
                    d = analyse.analyser(ref, lot, ouvrir_historique=bases.historique,
                                         chemin_historique=bases.chemin_historique(), ao=ao, calc=calc, ret=ret)
                except Exception as e:                      # noqa: BLE001 : on note et on continue
                    erreurs.append({"ref": ref, "lot": lot, "erreur": f"{type(e).__name__}: {e}"})
                    continue
                mo = d["simulation"]["modele"]
                if scenarios != mo["scenarios"]:
                    mo = {**mo, "scenarios": scenarios}
                s = simulateur.simuler(mo, central=False)
                med4 = d["resume_similaires"].get("mediane")
                x = mesurer_un(s, v, med4)
                x.update(ref=ref, lot=lot, identifiant=u.identifiant, groupes=len(mo["groupes"]))
                lignes.append(x)
                print(f"  {ref} lot {lot or '-'} : P vrai {v['p']:.3f}, médiane {s['prix_reference']['mediane']:.3f} "
                      f"[{s['prix_reference']['q10']:.3f} ; {s['prix_reference']['q90']:.3f}], zone "
                      f"{(s['zone'] or {}).get('x')}, gagnée {x.get('gagne_rec')}", flush=True)
    return {**_rendu(lignes), "erreurs": erreurs, "_lignes": lignes}


# --------------------------------------------------------------------------- réglage des taux (2025)

def _lignes_taux(annee: str, n: int, graine: int) -> list[dict]:
    """Pour chaque vrai soumissionnaire de n marchés : son vrai taux, ses offres passées (taux, similarité,
    âge, chez l'acheteur) et le segment de l'AO."""
    r0 = reglages.charger()
    rt = taux.charger_reglages()
    m = mp.moteur_annee(annee, sauver=False)
    mem = probables.memoire(bases.historique, bases.chemin_historique())
    offres = taux._offres_societe(mem)
    sortie = []
    t0 = time.time()
    for k, i in enumerate(mp.cibles(m, annee, n, graine)):
        u = m.corpus.unites[i]
        ctx, comp, _ = _contexte(m, mem, i, r0)
        sims = taux.Similarites(m, comp, r0["poids"])
        seg = taux.segment(mem, ctx, rt)
        vus = set()
        for c in u.concurrents:
            cle, t = c["cle"], c.get("taux")
            if not cle or cle in vus or not t or not (rt["taux_min"] <= t <= rt["taux_max"]):
                continue
            vus.add(cle)
            rows = []
            for j, tj in offres.get(cle, []):
                if j >= ctx.n:
                    break
                if rt["taux_min"] <= tj <= rt["taux_max"]:
                    uj = mem.unites[j]
                    rows.append((tj, max(sims.note(uj), 1.0) / 100.0, max(0, ctx.jour - mem.jours[j]),
                                 float(bases.cle_acheteur(uj.acheteur) in ctx.acheteur_cles)))
            sortie.append({"vrai": float(t), "rows": np.array(rows) if rows else np.zeros((0, 4)),
                           "seg_mu": seg["moyenne"], "seg_sigma": seg["sigma"], "marche": u.identifiant})
        if k % 50 == 0:
            print(f"  lignes {k} ({time.time() - t0:.0f} s)", flush=True)
    return sortie


def _evaluer(lignes: list[dict], g: float, h: float, b: float, km: float, ks: float, f: float) -> dict:
    err, dedans, base = [], [], []
    for l in lignes:
        R = l["rows"]
        if len(R):
            w = R[:, 1] ** g * 0.5 ** (R[:, 2] / h) * (1 + b * R[:, 3])
            sw = w.sum()
            mb = float((w * R[:, 0]).sum() / sw)
            vb = float((w * (R[:, 0] - mb) ** 2).sum() / sw)
            ne = float(sw * sw / (w * w).sum())
            if ne > 1:
                vb *= ne / (ne - 1)
            mu = (ne * mb + km * l["seg_mu"]) / (ne + km)
            var = (ne * vb + ks * l["seg_sigma"] ** 2) / (ne + ks)
            base.append(abs(float(R[:, 0].mean()) - l["vrai"]))
        else:
            mu, var = l["seg_mu"], l["seg_sigma"] ** 2
            base.append(abs(l["seg_mu"] - l["vrai"]))
        s = max(var ** 0.5 * f, 0.01)
        err.append(abs(mu - l["vrai"]))
        dedans.append(abs(l["vrai"] - mu) <= taux.Z90 * s)
    return {"mae": float(np.mean(err)), "couverture": float(np.mean(dedans)), "mae_base": float(np.mean(base))}


def ajuster(annee: str = "2025", n: int = 300, graine: int = 0, ecrire: bool = True) -> dict:
    t0 = time.time()
    lignes = _lignes_taux(annee, n, graine)
    print(f"  {len(lignes)} offres ({time.time() - t0:.0f} s)", flush=True)
    grille = {}
    for g in (0.5, 1.0, 2.0, 3.0):
        for h in (45, 90, 120, 180, 365, 730):
            for b in (0.0, 1.0, 3.0, 5.0, 10.0, 20.0):
                for km in (0.5, 1.0, 3.0, 10.0):
                    grille[(g, h, b, km)] = _evaluer(lignes, g, h, b, km, 3.0, 1.0)["mae"]
    g, h, b, km = min(grille, key=grille.get)
    print(f"  moyenne : γ={g} demi-vie={h} bonus={b} k={km} → MAE {grille[(g, h, b, km)]:.4f}", flush=True)
    essais = {}
    for ks in (0.5, 1.0, 3.0, 10.0):
        for f10 in range(8, 21):
            essais[(ks, f10 / 10)] = _evaluer(lignes, g, h, b, km, ks, f10 / 10)
    ks, f = min(essais, key=lambda c: (abs(essais[c]["couverture"] - 0.8), c[1]))
    final = essais[(ks, f)]
    print(f"  dispersion : k={ks} facteur={f} → couverture {final['couverture']:.3f}", flush=True)
    sortie = {"taux": {"gamma": g, "demi_vie": h, "bonus_acheteur": b, "k_moyenne": km, "k_sigma": ks,
                       "facteur_sigma": f},
              "ajuste_sur": {"annee": annee, "marches": n, "offres": len(lignes), "mae": round(final["mae"], 5),
                             "mae_base_simple": round(final["mae_base"], 5),
                             "couverture_10_90": round(final["couverture"], 4),
                             "base_simple": "moyenne simple des taux passés de la société (segment si aucun)",
                             "appris_jusqu_au": f"{annee}-12-31", "duree_s": round(time.time() - t0)}}
    if ecrire:
        with open(taux.FICHIER, "w", encoding="utf-8") as fh:
            json.dump(sortie, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return sortie


# --------------------------------------------------------------------------- calibration de la chance (2025)

def table_calibration(courbes: list[dict], pas: int = 10, points: int = 21) -> dict:
    """Table croissante chance simulée → chance réelle : régression isotone sur (courbe, victoire réelle) aux x
    de la grille (un sur `pas`), lue en `points` valeurs régulières de 0 à 1, strictement croissante."""
    from sklearn.isotonic import IsotonicRegression
    c = np.array([l["courbe"][j] for l in courbes for j in range(0, len(l["courbe"]), pas)])
    g = np.array([float(l["gagne_grille"][j]) for l in courbes for j in range(0, len(l["courbe"]), pas)])
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(c, g)
    xs = [k / (points - 1) for k in range(points)]
    ys = [float(v) for v in iso.predict(xs)]
    for k in range(1, len(ys)):
        ys[k] = max(ys[k], ys[k - 1] + 1e-4)
    return {"x": xs, "y": [round(min(v, 1.0), 5) for v in ys], "couples": int(len(c)), "marches": len(courbes)}


def calibrer(chemin_courbes: str, facteur: float, mesure_2025: dict | None = None, ecrire: bool = True) -> dict:
    import pickle
    with open(chemin_courbes, "rb") as f:
        courbes = pickle.load(f)["backtest"]
    t = table_calibration(courbes)
    with open(taux.FICHIER, encoding="utf-8") as f:
        tout = json.load(f)
    tout["simulation"] = {"facteur_marche": facteur, "calibration": {"x": t["x"], "y": t["y"]},
                          "appris": {"annee": "2025", "marches": t["marches"], "couples": t["couples"],
                                     "appris_jusqu_au": "2025-12-31", **(mesure_2025 or {})}}
    if ecrire:
        with open(taux.FICHIER, "w", encoding="utf-8") as f:
            json.dump(tout, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return tout["simulation"]


# --------------------------------------------------------------------------- ligne de commande

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m copilote.mesures_simulation")
    p.add_argument("commande", choices=("ajuster", "mesurer", "rejeux", "tout", "calibrer"))
    p.add_argument("--n", type=int)
    p.add_argument("--annee")
    p.add_argument("--scenarios", type=int, default=simulateur.SCENARIOS)
    p.add_argument("--courbes", help="fichier pickle où garder les courbes (essais de règles de zone)")
    p.add_argument("--facteur", type=float, help="mesurer : facteur de l'effet de marché, sans calibration (essais)")
    p.add_argument("--graine", type=int)
    a = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.commande == "ajuster":
        print(json.dumps(ajuster(a.annee or "2025", a.n or 300), ensure_ascii=False, indent=2))
        return 0
    if a.commande == "calibrer":
        print(json.dumps(calibrer(a.courbes, a.facteur or 1.0), ensure_ascii=False, indent=2))
        return 0
    sortie = {"reglages_taux": taux.charger_reglages(), "reglages_simulation": taux.charger_simulation()}
    rsim = None if a.facteur is None else {"facteur_marche": a.facteur, "calibration": None}
    if rsim:
        sortie["reglages_simulation"] = rsim
    if a.commande in ("mesurer", "tout"):
        graine = a.graine if a.graine is not None else (1 if (a.annee or "2026") == "2026" else 0)
        sortie["backtest"] = mesurer(a.annee or "2026", a.n or 500, graine, scenarios=a.scenarios, rsim=rsim)
        print("backtest", json.dumps(sortie["backtest"]["resume"], ensure_ascii=False))
    if a.commande in ("rejeux", "tout"):
        sortie["rejeux"] = rejeux(scenarios=a.scenarios)
        print("rejeux", json.dumps(sortie["rejeux"]["resume"], ensure_ascii=False))
    if a.courbes:
        garder_courbes(a.courbes, **{k: v.pop("_lignes") for k, v in sortie.items() if isinstance(v, dict) and "_lignes" in v})
    for v in sortie.values():
        if isinstance(v, dict):
            v.pop("_lignes", None)
    config.RAPPORTS.mkdir(parents=True, exist_ok=True)
    essai = config.RAPPORTS / f"copilote_etape_C_essai_{a.annee or '2026'}_{a.facteur}.json"
    cible = SORTIE if (a.annee or "2026") == "2026" and rsim is None else essai
    with open(cible, "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=1, default=str)
    print(f"→ {cible}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
