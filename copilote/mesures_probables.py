"""Ajustement et mesure des sociétés probables (étape A).

    python -m copilote.mesures_probables ajuster [--annee 2025] [--n 800]   coefficients → probables.json
    python -m copilote.mesures_probables mesurer [--annee 2026] [--n 500]   backtest sur des marchés tirés au hasard
    python -m copilote.mesures_probables rejeux                              les AO « REJEU- » (par l'API interne)
    python -m copilote.mesures_probables tout                                mesurer + rejeux → rapports/

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
N_SIMILAIRES = 50
SEUILS_CALIBRATION = (0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1.01)


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

def _couverture(marches: list[dict], coefs: dict, dispersion: float) -> float:
    dedans = []
    for x in marches:
        p = [probables.probabilite(v, coefs)[0] for v in x["x"]]
        envoyes = [q for q in p if q >= probables.REGLAGES["seuil_envoi"]]
        reste = sum(q for q in p if q < probables.REGLAGES["seuil_envoi"])
        lam = max(0.0, coefs["autres"]["constante"] + coefs["autres"]["par_offre_similaire"] * x["moy"])
        f = probables.fourchette(probables.distribution(envoyes, lam + reste, dispersion))
        dedans.append(f["bas"] <= x["vrais"] <= f["haut"])
    return float(np.mean(dedans))


def calibration(marches: list[dict], coefs: dict, points: int = 120) -> dict:
    """Table strictement croissante p de la formule → part réellement venue, par régression isotone sur
    des marchés que la régression logistique n'a pas vus ; les blocs voisins sont fusionnés jusqu'à `points`."""
    from sklearn.isotonic import IsotonicRegression
    brutes, venues = [], []
    for mm in marches:
        for x, y in zip(mm["x"], mm["y"]):
            brutes.append(probables.probabilite(x, {**coefs, "calibration": None})[2])
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
    while len(blocs) > points:                   # fusionne la paire voisine la plus légère
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


def ajuster(annee: str = "2025", n: int = 800, graine: int = 0, ecrire: bool = True) -> dict:
    """Coefficients de la formule sur les marchés pairs de l'échantillon ; λ (sociétés que rien n'annonce)
    et σ (dispersion) sur les marchés impairs, que la régression n'a pas vus."""
    from sklearn.linear_model import LogisticRegression
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
                        "moy": probables.offres_moyennes_similaires(ctx)})
        if k % 100 == 0:
            print(f"  ajuster {k} ({time.time() - t0:.0f} s)", flush=True)
    pairs, impairs = marches[::2], marches[1::2]
    X = np.array([[x[v] for v in probables.VARIABLES] for mm in pairs for x in mm["x"]])
    y = np.array([v for mm in pairs for v in mm["y"]])
    lr = LogisticRegression(C=10.0, max_iter=5000).fit(X, y)
    A = np.vstack([np.ones(len(impairs)), [mm["moy"] for mm in impairs]]).T
    a, b = np.linalg.lstsq(A, np.array([mm["autres"] for mm in impairs], dtype=float), rcond=None)[0]
    coefs = {"constante": round(float(lr.intercept_[0]), 4),
             "coefficients": {v: round(float(w), 4) for v, w in zip(probables.VARIABLES, lr.coef_[0])},
             "autres": {"constante": round(float(max(a, 0.0)), 4), "par_offre_similaire": round(float(max(b, 0.0)), 4)},
             "dispersion": 0.0}
    coefs["calibration"] = calibration(impairs, coefs)
    coefs["ajuste_sur"] = {}
    essais = {}
    for s10 in range(0, 16):
        essais[s10 / 10] = _couverture(impairs, coefs, s10 / 10)
    coefs["dispersion"] = min(essais, key=lambda s: (abs(essais[s] - 0.80), s))
    coefs["ajuste_sur"]["calibration_points"] = len(coefs["calibration"]["x"])
    coefs["origine"] = (f"régression logistique sur {len(y)} couples société × marché de {annee} "
                        f"({int(y.sum())} venues, {len(pairs)} marchés) ; λ et σ sur {len(impairs)} autres marchés ; "
                        f"ajustée le {date.today().isoformat()}")
    coefs["ajuste_sur"] = {**coefs["ajuste_sur"], "annee": annee, "marches": len(marches), "couples": int(len(y)), "venues": int(y.sum()),
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


def mesurer_un(a: dict, vrais: set[str], gagnant: str | None, mem: probables.Memoire, n: int) -> dict:
    """`a` : la sortie de probables.analyser, ou le dossier de l'API (mêmes clés)."""
    cands = a["candidats"] if "candidats" in a else a["probables"]["candidats"]
    ordre = sorted(cands, key=lambda c: (-c["p"], c["cle"]))          # calibration strictement croissante : même ordre
    top10 = {c["cle"] for c in ordre[:10]}
    top15 = {c["cle"] for c in ordre[:15]}
    par_assoc = {c["cle"] for c in cands if c["association"]}
    liste = {c["cle"] for c in cands if c["dans_liste"]} | par_assoc
    seulement_assoc = par_assoc - {c["cle"] for c in cands if c["dans_liste"]}
    base = [c for c in cands if c.get("brut")]
    base.sort(key=lambda c: (-c["brut"]["similaires"], -c["brut"]["acheteur"], c["cle"]))
    base15 = {c["cle"] for c in base[:15]}
    base10 = {c["cle"] for c in base[:10]}
    conc = a["concurrents"]
    membres_predits = {m for g in a["groupes"] for m in g["membres"]}
    vm = vrais_membres(mem, n, vrais, {})
    return {
        "vrais": len(vrais), "top10": len(vrais & top10), "top15": len(vrais & top15), "liste": len(vrais & liste),
        "taille_liste": len(liste), "base10": len(vrais & base10), "base15": len(vrais & base15),
        "gagnant": gagnant, "gagnant_liste": None if not gagnant else gagnant in liste,
        "gagnant_top15": None if not gagnant else gagnant in top15,
        "gagnant_base15": None if not gagnant else gagnant in base15,
        "bas": conc["bas"], "haut": conc["haut"], "probable": conc["probable"],
        "dans_fourchette": conc["bas"] <= len(vrais) <= conc["haut"],
        "association": len(par_assoc), "trouvees_par_association": len(vrais & seulement_assoc),
        "vrais_membres": len(vm), "membres_retrouves": len(vm & membres_predits),
        "p": {c["cle"]: c["p"] for c in cands}, "venus": sorted(vrais & {c["cle"] for c in cands}),
    }


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
        "gagnant_base15": moy(lambda x: None if x["gagnant_base15"] is None else float(x["gagnant_base15"])),
        "dans_fourchette": moy(lambda x: float(x["dans_fourchette"])),
        "sous_fourchette": moy(lambda x: float(x["vrais"] < x["bas"])),
        "sur_fourchette": moy(lambda x: float(x["vrais"] > x["haut"])),
        "largeur_fourchette": moy(lambda x: x["haut"] - x["bas"]),
        "ecart_probable": moy(lambda x: abs(x["probable"] - x["vrais"])),
        "taille_liste": moy(lambda x: x["taille_liste"]),
        "association_par_marche": moy(lambda x: x["association"]),
        "trouvees_par_association": sum(x["trouvees_par_association"] for x in lignes),
        "vrais_total": tous_vrais,
        "vrais_membres": vm,
        "membres_retrouves": round(sum(x["membres_retrouves"] for x in lignes) / vm, 4) if vm else None,
        "calibration": calib,
    }


# --------------------------------------------------------------------------- backtest et rejeux

def mesurer(annee: str = "2026", n: int = 500, graine: int = 1) -> dict:
    r0 = reglages.charger()
    coefs = probables.charger_coefficients()
    m = moteur_annee(annee, sauver=True)
    mem = probables.memoire(bases.historique, bases.chemin_historique())
    lignes, t0 = [], time.time()
    for k, i in enumerate(cibles(m, annee, n, graine)):
        ctx = contexte_marche(m, mem, i, r0)
        a = probables.analyser(mem, ctx, coefs)
        u = m.corpus.unites[i]
        x = mesurer_un(a, reels(u), u.cle_gagnant, mem, ctx.n)
        x["identifiant"] = u.identifiant
        lignes.append(x)
        if k % 50 == 0:
            print(f"  mesurer {k} ({time.time() - t0:.0f} s)", flush=True)
    return {"annee": annee, "graine": graine, "duree_s": round(time.time() - t0), "resume": resumer(lignes),
            "par_tranche": {t: resumer([x for x in lignes if _tranche(x["vrais"]) == t])
                            for t in ("1-3", "4-7", "8-15", "16+")},
            "lignes": [{k: v for k, v in x.items() if k not in ("p", "venus")} for x in lignes]}


def _tranche(v: int) -> str:
    return "1-3" if v <= 3 else "4-7" if v <= 7 else "8-15" if v <= 15 else "16+"


def rejeux() -> dict:
    from . import verite_rejeu
    lignes, erreurs = [], []
    with closing(bases.calcul()) as calc, closing(bases.retours()) as ret, closing(bases.ao_ouverts()) as ao:
        refs = [r[0] for r in calc.execute("SELECT ref_annonce FROM rejeu_ao ORDER BY ref_annonce")]
        mem = probables.memoire(bases.historique, bases.chemin_historique())
        for ref in refs:
            lots = [r[0] for r in ao.execute("SELECT lot FROM appels_lots WHERE ref_annonce = ? ORDER BY rowid", (ref,))]
            for lot in lots or [None]:
                try:
                    a = analyse.analyser(ref, lot, ouvrir_historique=bases.historique,
                                         chemin_historique=bases.chemin_historique(), ao=ao, calc=calc, ret=ret)
                except Exception as e:                      # noqa: BLE001 : on note et on continue
                    erreurs.append({"ref": ref, "lot": lot, "erreur": f"{type(e).__name__}: {e}"})
                    continue
                v = verite_rejeu.pour_lot(calc, ref, lot)
                if v is None:
                    erreurs.append({"ref": ref, "lot": lot, "erreur": "vérité absente"})
                    continue
                n = mem.coupe(rejeu.limite_pour(ref, calc))
                x = mesurer_un({**a["probables"], "concurrents": a["concurrents"], "groupes": a["groupes"]},
                               {c["cle"] for c in v["concurrents"]}, v["gagnant"]["cle"], mem, n)
                x.update(ref=ref, lot=lot)
                lignes.append(x)
                print(f"  {ref} lot {lot or '-'} : {x['top15']}/{x['vrais']} dans le top 15, "
                      f"fourchette {x['bas']}-{x['haut']}, gagnant {'oui' if x['gagnant_liste'] else 'non'}", flush=True)
    return {"resume": resumer(lignes), "erreurs": erreurs,
            "lignes": [{k: v for k, v in x.items() if k not in ("p", "venus")} for x in lignes]}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m copilote.mesures_probables")
    p.add_argument("commande", choices=("ajuster", "mesurer", "rejeux", "tout"))
    p.add_argument("--annee")
    p.add_argument("--n", type=int)
    p.add_argument("--graine", type=int, default=None)
    a = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.commande == "ajuster":
        c = ajuster(a.annee or "2025", a.n or 800, a.graine or 0)
        print(json.dumps(c, ensure_ascii=False, indent=2))
        return 0
    sortie = {}
    if a.commande in ("mesurer", "tout"):
        sortie["backtest"] = mesurer(a.annee or "2026", a.n or 500, 1 if a.graine is None else a.graine)
        print(json.dumps(sortie["backtest"]["resume"], ensure_ascii=False, indent=1))
    if a.commande in ("rejeux", "tout"):
        sortie["rejeux"] = rejeux()
        print(json.dumps(sortie["rejeux"]["resume"], ensure_ascii=False, indent=1))
        for e in sortie["rejeux"]["erreurs"]:
            print("  erreur", e)
    sortie["coefficients"] = probables.charger_coefficients()
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=1)
    print(f"→ {SORTIE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
