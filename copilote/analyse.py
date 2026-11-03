"""GET /api/copilote/analyse/{ref_annonce}?lot= : le dossier d'analyse d'un AO (étape A).

L'AO, ses marchés similaires (étape 4, mêmes calculs que /api/copilote/similaires), les sociétés probables
(probables.py), leurs groupes de présence commune et la stratégie passée de chaque groupe (groupes_ao.py),
et la loi du nombre de concurrents.

Un AO « REJEU- » ne voit que l'historique d'avant sa coupure : la limite vient de rejeu.limite_pour, la
même que celle des marchés similaires. Ce module ne lit jamais ce qui s'est réellement passé : la page
le demande à part (verite_rejeu.py).
"""
from __future__ import annotations

import sqlite3
from datetime import date

from . import api_similaires, probables, rejeu, simulateur, taux

TOP_SIMILAIRES = 20


def _r(x, n: int = 4):
    return None if x is None else round(float(x), n)


def _candidat(c: dict) -> dict:
    return {
        "cle": c["cle"], "nom": c["nom"], "p": _r(c["p"]),
        "p_formule": _r(c["p_formule"]), "dans_liste": c["dans_liste"], "segment": c.get("segment"),
        "accompagne_aussi": c.get("accompagne_aussi", []),
        "association": None if not c["association"] else {
            **c["association"], "p": _r(c["association"]["p"]), "B": _r(c["association"]["B"]),
            "C": _r(c["association"]["C"]), "p_via": _r(c["association"]["p_via"])},
        "x": None if c["x"] is None else {k: _r(v) for k, v in c["x"].items()},
        "contributions": None if c["contributions"] is None else {k: _r(v, 3) for k, v in c["contributions"].items()},
        "constante": _r(c.get("constante"), 3),
        "brut": c["brut"],
        "eviction": {"p": _r(c["eviction"]["p"]), "ecartees": c["eviction"]["ecartees"],
                     "offres": c["eviction"]["offres"], "global": _r(c["eviction"]["global"])},
    }


def _groupe(g: dict) -> dict:
    s = g["strategie"]
    return {
        "numero": g["numero"], "chef": g["chef"], "membres": g["membres"],
        "aussi": g.get("aussi", []), "travaille_avec": g.get("travaille_avec", []),
        "score_chef": None if not g.get("score_chef") else {k: _r(v) for k, v in g["score_chef"].items()},
        "victoires_accompagnees": g["victoires_accompagnees"],
        "liens": [{k: (_r(v) if isinstance(v, float) else v) for k, v in l.items()} for l in g["liens"]],
        "strategie": {
            **{k: s[k] for k in ("unites", "jugees", "gagnees", "gains", "defaites", "defaites_ecartes")},
            "taux_victoire": _r(s["taux_victoire"]),
            "membres": [{k: (_r(v) if isinstance(v, float) else v) for k, v in m.items()} for m in s["membres"]],
            "exterieur": {**{k: _r(v) for k, v in s["exterieur"].items() if k != "gagnants"},
                          "gagnants": s["exterieur"]["gagnants"]},
            "dernieres": [{**d, "taux_gagnant": _r(d["taux_gagnant"])} for d in s["dernieres"]]},
    }


def contexte(mem: probables.Memoire, limite: str | None, jour_ref: int, cible, moteur, notes, composantes,
             retenus) -> probables.Contexte:
    """Le contexte de probables.py à partir de ce qu'a calculé le moteur de similarité."""
    n = mem.coupe(limite)
    sims = [probables.Similaire(moteur.corpus.unites[int(i)], float(notes[int(i)]),
                                float(composantes.valeurs["recence"][int(i)])) for i in retenus]
    proches = ([moteur.corpus.unites[int(i)] for i in moteur.meilleurs(notes, probables.REGLAGES["proches"])]
               if len(notes) else [])
    if limite is not None:
        # Garde-fou : rien de ce que lit le calcul n'est daté d'après la coupure.
        assert all(s.unite.date <= limite for s in sims), "similaire postérieur à la coupure"
        assert all(u.date <= limite for u in proches), "marché proche postérieur à la coupure"
        assert n == 0 or mem.dates[n - 1] <= limite
    return probables.Contexte(n=n, jour=jour_ref, acheteur_cles=frozenset(cible.acheteur_cles),
                              codes=frozenset(cible.quals), similaires=sims, proches=proches,
                              region=cible.region, famille=cible.famille, estimation=cible.estimation)


def analyser(ref_annonce: str, lot: str | None, *, ouvrir_historique, chemin_historique,
             ao: sqlite3.Connection, calc: sqlite3.Connection, ret: sqlite3.Connection,
             aujourdhui: date | None = None, coefficients: dict | None = None) -> dict:
    """`coefficients` : ceux de probables.json par défaut ; probables.charger_coefficients(FICHIER_A) pour
    rejouer la version A (mesures avant / après)."""
    sim, interne = api_similaires.analyser(ref_annonce, lot, None, ouvrir_historique=ouvrir_historique,
                                           chemin_historique=chemin_historique, ao=ao, calc=calc, ret=ret,
                                           aujourdhui=aujourdhui, interne=True)
    limite = rejeu.limite_pour(ref_annonce, calc)
    mem = probables.memoire(ouvrir_historique, chemin_historique)
    ctx = contexte(mem, limite, interne["jour_ref"], interne["cible"], interne["moteur"], interne["notes"],
                   interne["composantes"], interne["retenus"])
    a = probables.analyser(mem, ctx, coefficients)
    conc = a["concurrents"]
    hist_c = conc["historique"]
    tx = taux.calculer(mem, ctx, a["candidats"], a["groupes"], moteur=interne["moteur"],
                       composantes=interne["composantes"], poids=sim["poids"])
    dossier = {
        "ao": {**sim["ao"], "coupure": interne["coupure"], "lots": interne["lots"],
               "date_limite": interne["date_limite"], "reference": interne["reference"]},
        "acheteur": sim["acheteur"], "index": sim["index"], "filtre": sim["filtre"], "poids": sim["poids"],
        "marches_lus": sim["marches_lus"], "marches_comparables": sim["marches_comparables"],
        "resume_similaires": sim["resume"],
        "similaires": sim["similaires"][:TOP_SIMILAIRES], "similaires_utilises": len(sim["similaires"]),
        "historique": {"unites_visibles": ctx.n, "derniere_date": mem.dates[ctx.n - 1] if ctx.n else None},
        "probables": {"candidats": [_candidat(c) for c in a["candidats"]], "nb_candidats": a["nb_candidats"],
                      "variables": probables.LIBELLES,
                      "coefficients": {"constante": a["coefficients"]["constante"],
                                       "coefficients": a["coefficients"]["coefficients"],
                                       "origine": a["coefficients"].get("origine", ""),
                                       "version": a["coefficients"].get("version", "A2"),
                                       "empreinte": probables.empreinte_coefficients(a["coefficients"]),
                                       "segments": sorted(a["coefficients"].get("calibration_segments") or []),
                                       "segments_formule": sorted(a["coefficients"].get("par_segment") or [])},
                      "segments": probables.SEGMENTS,
                      "reglages": a["reglages"]},
        "concurrents": {**{k: _r(v) if isinstance(v, float) else v for k, v in conc.items()
                           if k not in ("distribution", "historique")},
                        "historique": {**{k: _r(v) if isinstance(v, float) else v for k, v in hist_c.items()}},
                        "distribution": [_r(v, 6) for v in conc["distribution"]]},
        "groupes": [_groupe(g) for g in a["groupes"]],
        "individuelles": a["individuelles"],
        "taux": rendre_taux(tx),
    }
    dossier["simulation"] = {"modele": simulateur.modele(dossier),
                             "aide": "statique/simulateur.js et copilote/simulateur.py, même graine = même résultat"}
    return dossier


def _rr(d: dict, n: int = 5) -> dict:
    return {k: (_r(v, n) if isinstance(v, float) else v) for k, v in d.items()}


def rendre_taux(tx: dict) -> dict:
    """Les profils de taux.calculer, arrondis pour le JSON (le modèle du simulateur est construit sur ces
    valeurs arrondies : la page et la prévision figée partent exactement du même modèle)."""
    seg = tx["segment"]
    return {
        "societes": {k: {**_rr({x: y for x, y in p.items() if x != "sous_historique"}),
                         **({"sous_historique": p["sous_historique"]} if "sous_historique" in p else {})}
                     for k, p in tx["societes"].items()},
        "segment": {**_rr({k: v for k, v in seg.items() if k != "quantiles"}),
                    "quantiles": [_r(q, 5) for q in seg["quantiles"]]},
        "effet_marche": _rr(tx["effet_marche"]),
        "groupes": [{**_rr({k: v for k, v in g.items() if k != "membres"}),
                     "membres": [{"cle": m["cle"], "chef_present": _rr(m["chef_present"]),
                                  "chef_absent": _rr(m["chef_absent"]), "ecart": _rr(m["ecart"]),
                                  "ecart_victoires": _rr(m["ecart_victoires"]),
                                  "presence_victoires": _rr(m["presence_victoires"])} for m in g["membres"]]}
                    for g in tx["groupes"]],
        "reglages": tx["reglages"],
    }
