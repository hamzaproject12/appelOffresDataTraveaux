"""La page /copilote/suivi (étape D) : pour chaque AO soumissionné ou figé, ce qui était prévu comparé à ce qui s'est
passé quand le PV arrive, et le bilan de nos soumissions.

Pour un vrai AO, le PV est le marché « c » + ref_annonce de l'historique (suivi.reel). Pour un AO « REJEU- » (test), c'est
le marché réel dont il est la copie (vérité fournie par routes.py). Notre offre (le prix déposé, ou à défaut le point
recommandé figé) est insérée parmi les vraies offres avec la règle de construire_base (donnees.rejouer_attribution) :
son rang, et si elle aurait gagné.
"""
from __future__ import annotations

from . import rejeu, soumissions, suivi
from .donnees import CLE_FICTIVE, rejouer_attribution


def _unite(mem, ref_pv: str | None, lot: str | None):
    if not ref_pv:
        return None
    for i in suivi._par_ref(mem).get(ref_pv, []):
        u = mem.unites[i]
        if (u.lot or "") == (lot or ""):
            return u
    return None


def notre_rang(u, x: float | None) -> dict | None:
    """Notre offre au taux x parmi les vraies offres du PV."""
    if x is None or not u or not u.estimation:
        return None
    a = rejouer_attribution(u, x)
    return {"x": x, "rang": a.rangs.get(len(u.concurrents)), "gagne": a.cle_gagnant == CLE_FICTIVE,
            "nb_offres": a.nb_offres, "prix_reference": (a.reference / u.estimation) if a.reference else None}


def _reel(mem, verite, ref: str, lot: str | None) -> tuple[dict | None, object]:
    """(le PV résumé, l'unité de l'historique) ou (None, None) tant que le PV n'est pas là."""
    if rejeu.est_un_rejeu(ref):
        v = verite(ref, lot)
        if not v:
            return None, None
        return ({"ref_pv": v["reference_reelle"], "date": v["date_ouverture"], "nb_concurrents": v["nb_concurrents"],
                 "gagnant": v["gagnant"], "prix_reference_taux": (v["prix_reference"] / v["estimation"])
                 if v["prix_reference"] and v["estimation"] else None,
                 "concurrents": v["concurrents"]}, _unite(mem, v["ref_reel"], None))
    r = suivi.reel(mem, ref, lot)
    if r is None:
        return None, None
    i = r.pop("_indice")
    return r, mem.unites[i]


def tableau(ret, verite, mem) -> dict:
    """`verite(ref, lot)` : la vérité d'un AO de test (verite_rejeu.pour_lot), fournie par routes.py."""
    soumissions.preparer(ret)
    suivi.preparer(ret)
    prises = {(s["ref_annonce"], s["lot"]): s for s in soumissions.lister(ret)}
    figees = {}
    for r in ret.execute("SELECT ref_annonce, lot, MIN(jour) premier, COUNT(*) n, MAX(objet) objet, MAX(acheteur) acheteur,"
                         " MAX(date_limite) date_limite FROM previsions GROUP BY ref_annonce, lot"):
        figees[(r["ref_annonce"], r["lot"])] = dict(r)
    lignes = []
    for cle in sorted(set(prises) | set(figees), key=lambda k: (prises[k]["soumis_le"] if k in prises else "", k), reverse=True):
        ref, lot = cle
        s, f = prises.get(cle), figees.get(cle)
        e = suivi.etat(ret, mem, ref, lot or None) if f else None
        prevision = e["prevision"] if e else None
        fige = None
        if f:
            dec = (prevision or {}).get("decision")
            z = ((prevision or {}).get("simulation") or {}).get("zone") or {}
            fige = {"copies": f["n"], "premier_jour": f["premier"], "decision": dec,
                    "x": (dec or {}).get("x", z.get("x")), "chance": (dec or {}).get("chance", z.get("chance"))}
        reel, u = _reel(mem, verite, ref, lot or None)
        notre, source = None, None
        if reel and u:
            if s:
                notre, source = notre_rang(u, s["prix_depose"] / s["estimation"]), "prix déposé"
            elif fige and fige["x"]:
                notre, source = notre_rang(u, fige["x"]), "point recommandé figé"
        info = s or f
        lignes.append({
            "ref_annonce": ref, "lot": lot or None, "rejeu": rejeu.est_un_rejeu(ref),
            "objet": (info or {}).get("objet"), "acheteur": (info or {}).get("acheteur"),
            "date_limite": (info or {}).get("date_limite"),
            "soumission": s, "figee": fige,
            "prevu": None if not prevision else {
                "nombre": {k: prevision["concurrents"].get(k) for k in ("bas", "haut", "probable")},
                "prix_reference": ((prevision.get("simulation") or {}).get("prix_reference") or {}).get("mediane"),
                "zone": (prevision.get("simulation") or {}).get("zone")},
            "reel": None if not reel else {
                "ref_pv": reel["ref_pv"], "date": reel["date"], "nb_concurrents": reel["nb_concurrents"],
                "gagnant": reel["gagnant"], "prix_reference_taux": reel["prix_reference_taux"],
                "societes": [{"nom": c["nom"], "taux": c.get("taux"), "statut": c.get("statut")} for c in reel["concurrents"]]},
            "comparaison": None if not e or not e.get("score") else {
                k: e["score"][k] for k in ("venues_prevues", "venues", "dans_fourchette", "fourchette", "prix_reference")},
            "notre": None if not notre else {**notre, "source": source},
        })
    return {"lignes": lignes, "bilan": bilan(lignes)}


def bilan(lignes: list[dict]) -> dict:
    """Le bilan de nos soumissions réelles dont le PV est connu (les AO de test sont comptés à part)."""
    jouees = [l for l in lignes if l["soumission"] and not l["rejeu"]]
    jugees = [l for l in jouees if l["notre"] and l["notre"]["source"] == "prix déposé"]
    gagnees = [l for l in jugees if l["notre"]["gagne"]]
    marges = [(l["soumission"]["prix_depose"] - l["soumission"]["cout"]) / l["soumission"]["cout"] for l in gagnees]
    gain = sum(l["soumission"]["prix_depose"] - l["soumission"]["cout"] for l in gagnees)
    figees = [l for l in lignes if l["figee"] and not l["soumission"] and not l["rejeu"] and l["notre"]]
    return {
        "soumissions": len(jouees), "en_attente": len(jouees) - len(jugees), "jugees": len(jugees),
        "gagnees": len(gagnees), "taux_victoire": (len(gagnees) / len(jugees)) if jugees else None,
        "marge_moyenne": (sum(marges) / len(marges)) if marges else None, "gain_dh": gain if jugees else None,
        "figees_jugees": len(figees), "figees_gagnees": sum(1 for l in figees if l["notre"]["gagne"]),
    }
