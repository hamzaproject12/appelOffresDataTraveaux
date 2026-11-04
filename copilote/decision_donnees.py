"""Ce que le serveur fournit à l'étape ⑥ « Décision » (le calcul lui-même est dans le navigateur) et ce qui est figé
avec les nouvelles prévisions.

    GET /api/copilote/decision/{ref}?lot=   →  donnees(...) : notre profil, les qualifications de l'AO, l'historique de
                                               l'acheteur, le plan de charge, le délai, la soumission déjà enregistrée,
                                               les seuils (reglages.json) et les phrases.
"""
from __future__ import annotations

from datetime import date

from . import decision, eligibilite, entreprise, soumissions


def exigences_ao(ao, ref_annonce: str) -> tuple[list, list[dict]]:
    """Les lignes brutes (libellé, classe) de l'annonce et les exigences qu'on en tire."""
    lignes = [(r["libelle"], r["classe"]) for r in ao.execute(
        "SELECT libelle, classe FROM appels_qualifications WHERE ref_annonce = ? ORDER BY rowid", (ref_annonce,))]
    return lignes, eligibilite.exigences_de(lignes)


def stats_acheteur(hist, cles: list[str], avant: str | None) -> dict:
    """Marchés de cet acheteur dans l'historique (avant le jour de référence), dont les infructueux."""
    if not cles:
        return {"n": 0, "infructueux": {"n": 0, "total": 0, "taux": 0.0}}
    marques = ",".join("?" * len(cles))
    sql = ("SELECT COUNT(*), COALESCE(SUM(COALESCE(infructueux, 0)), 0) FROM marches"
           f" WHERE principale = 1 AND date_douteuse = 0 AND cle_acheteur(acheteur) IN ({marques})")
    args = list(cles)
    if avant:
        sql += " AND tri_date < ?"
        args.append(avant.replace("-", ""))
    total, inf = hist.execute(sql, args).fetchone()
    return {"n": total, "infructueux": {"n": inf, "total": total, "taux": (inf / total) if total else 0.0}}


def donnees(dossier: dict, ao, hist, ret, aujourdhui: date | None = None) -> dict:
    """Tout ce qu'il faut à la page pour décider, en plus du dossier d'analyse."""
    a = dossier["ao"]
    profil = entreprise.lire(ret)
    lignes, exig = exigences_ao(ao, a["ref_annonce"])
    stats = stats_acheteur(hist, dossier["acheteur"].get("cles_utilisees") or [], a.get("date_reference"))
    reg = decision.charger_reglages()
    return {
        "profil": profil,
        "lignes_ao": lignes, "exigences": exig,
        "cout_defaut": entreprise.cout_par_defaut(profil, (a.get("famille") or {}).get("code")),
        "acheteur": stats,
        "plan": {"en_cours": soumissions.en_cours(ret, aujourdhui), "max": profil["plan_charge_max"]},
        "jours": soumissions.jours_restants(a.get("date_limite"), aujourdhui),
        "soumission": soumissions.lire(ret, a["ref_annonce"], a.get("lot")),
        "reglages": reg, "textes": decision.charger_textes(),
    }


def signaux(dossier: dict, prevu: float | None, stats: dict | None = None, jours: int | None = None,
            plan: dict | None = None, reg: dict | None = None) -> dict:
    """Les signaux de la décision pour le dossier d'origine (sans les modifications de la page)."""
    reg = reg or decision.charger_reglages()
    conc = dossier["concurrents"]
    s = {"prevu": prevu, "jours": jours}
    if prevu:
        s["identifies_part"] = max(0.0, min(1.0, 1.0 - (conc.get("lambda") or 0.0) / prevu))
    if stats is not None:
        s["acheteur_n"] = stats["n"]
        s["infructueux"] = stats["infructueux"]
    if plan:
        s["en_cours"], s["plan_max"] = plan.get("en_cours"), plan.get("max")
    par_cle = {c["cle"]: c for c in dossier["probables"]["candidats"]}
    fort = reg["groupe_fort"]
    s["groupes_forts"] = [{"numero": g["numero"], "nom": par_cle[g["chef"]]["nom"], "p": par_cle[g["chef"]]["p"]}
                          for g in dossier["groupes"]
                          if g["chef"] in par_cle and par_cle[g["chef"]]["p"] >= fort["p_chef"]
                          and len(g["membres"]) >= fort["membres_min"]]
    return s


def decision_figee(dossier: dict, simulation: dict | None, profil: dict, prevu: float | None = None) -> dict | None:
    """La décision qui s'ajoute à une NOUVELLE prévision figée : coût par défaut, scénario « seul », signaux tirés du
    dossier seul (l'historique de l'acheteur n'y figure pas). None si rien n'est saisi (pas de coût par défaut) ou si
    l'AO n'a pas d'estimation."""
    a = dossier["ao"]
    if not simulation or not a.get("estimation"):
        return None
    cout = entreprise.cout_par_defaut(profil, (a.get("famille") or {}).get("code"))
    if cout is None or profil.get("marge_min") is None:
        return None
    reg = decision.charger_reglages()
    E = a["estimation"]
    xs, chance = simulation["grille"], simulation["chance"]
    if not isinstance(xs, list):
        xs = [(xs["debut"] + j) / xs["denominateur"] for j in range(xs["n"])]
    ev = decision.evaluer(xs, chance, E, cout["taux"] * E, profil["marge_min"], reg)
    exig = eligibilite.exigences_de([(q["code"], q["classe"]) for q in a.get("qualifications") or []])
    elig = eligibilite.verifier(exig, entreprise.qualifs_par_code(profil)) if exig else None
    d = decision.decider(ev, elig, signaux(dossier, prevu, jours=soumissions.jours_restants(a.get("date_limite")), reg=reg), reg)
    reco = ev["reco"]
    return {"cout_taux": cout["taux"], "origine_cout": cout["origine"], "marge_min": profil["marge_min"],
            "decision": d["decision"], "confiance": d["confiance"], "raisons": [r["code"] for r in d["raisons"]],
            "x": None if not reco else reco["x"], "chance": None if not reco else reco["chance"],
            "gain": None if not reco else reco["gain"],
            "eligibilite": None if elig is None else elig["statut"],
            "eligibilite_partielle": True}
