"""Le suivi des prévisions : une copie figée de chaque analyse d'un vrai AO, comparée au PV quand il arrive.

    python -m copilote.suivi                      les AO prévus, et leur score quand le PV est connu
    python -m copilote.suivi --detail 1044485     une prévision et sa comparaison, société par société
    python -m copilote.suivi figer 1044485 1041802:1 1041802:2
                                                  fige l'analyse de ces AO (sur la machine qui sert le site)

Une copie est enregistrée dans copilote_retours.db (table `previsions`) à la première ouverture de l'analyse
d'un vrai AO, puis au plus une fois par jour (UTC) : probabilités, nombre de concurrents, groupes, et la
version des réglages (version des coefficients + empreinte des coefficients et des réglages). Les « REJEU- »
ne sont jamais figés : leur vérité est connue d'avance (verite_rejeu.py).

Le PV : un marché de pv_travaux.db dont la référence est « c » + ref_annonce (la règle de construire_base),
lot par lot pour un AO alloti. La comparaison prend la dernière copie figée au plus tard le jour de
l'ouverture des plis (sinon la première) : on juge ce que la page annonçait avant de connaître le résultat.
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from datetime import datetime, timezone

from . import bases, probables, rejeu

SCHEMA = """
CREATE TABLE IF NOT EXISTS previsions (
  id INTEGER PRIMARY KEY,
  ref_annonce TEXT NOT NULL,
  lot TEXT NOT NULL DEFAULT '',              -- '' pour un AO non alloti
  jour TEXT NOT NULL,                        -- AAAA-MM-JJ (UTC) : une copie par jour au plus
  cree_le TEXT NOT NULL,
  version TEXT NOT NULL,                     -- version des coefficients (A, A2…)
  empreinte TEXT NOT NULL,                   -- probables.empreinte_coefficients
  reference TEXT, acheteur TEXT, objet TEXT, date_limite TEXT,
  prevision TEXT NOT NULL,                   -- JSON (voir copie)
  UNIQUE (ref_annonce, lot, jour));
CREATE INDEX IF NOT EXISTS i_previsions_ref ON previsions(ref_annonce, lot);
"""
TOP = 15


def preparer(ret) -> None:
    ret.executescript(SCHEMA)


def _jour(maintenant: datetime | None = None) -> str:
    return (maintenant or datetime.now(timezone.utc)).date().isoformat()


def copie(dossier: dict) -> dict:
    """Ce qui est figé d'un dossier d'analyse (analyse.analyser)."""
    pr, conc = dossier["probables"], dossier["concurrents"]
    return {
        "date_reference": dossier["ao"].get("date_reference"),
        "historique": dossier.get("historique"),
        "coefficients": {k: pr["coefficients"].get(k) for k in ("version", "empreinte", "origine")},
        "reglages": pr.get("reglages"),
        "candidats": [{"cle": c["cle"], "nom": c["nom"], "p": c["p"], "dans_liste": c["dans_liste"],
                       "association": bool(c["association"]), "segment": c.get("segment")}
                      for c in pr["candidats"]],
        "concurrents": {k: conc.get(k) for k in ("bas", "haut", "probable", "moyenne", "niveau", "lambda",
                                                 "dispersion", "decalage", "distribution")},
        "groupes": [{"numero": g["numero"], "chef": g["chef"], "membres": g["membres"],
                     "aussi": [a["cle"] for a in g.get("aussi", [])]} for g in dossier["groupes"]],
        **({"simulation": simulation_figee(dossier["simulation"]["modele"])} if dossier.get("simulation") else {}),
    }


def simulation_figee(modele: dict) -> dict:
    """Étape C : la courbe de chance de gagner, la zone recommandée et le prix de référence simulé, calculés par
    copilote/simulateur.py sur le modèle du dossier (même graine que la page : même résultat)."""
    from . import simulateur
    s = simulateur.simuler(modele, central=False)
    r4 = lambda v: None if v is None else round(v, 5)       # noqa: E731
    return {"graine": s["graine"], "scenarios": s["scenarios"], "grille": modele.get("grille"),
            "estimation": modele.get("estimation"), "chance": [round(c, 5) for c in s["chance"]],
            "zone": None if not s["zone"] else {k: (r4(v) if isinstance(v, float) else v) for k, v in s["zone"].items()},
            "prix_reference": {k: r4(s["prix_reference"][k]) for k in ("n", "mediane", "q10", "q90", "q25", "q75")},
            "chefs": s["chefs"], "offres_moyennes": r4(s["offres_moyennes"])}


def figer(ret, dossier: dict, maintenant: datetime | None = None) -> str:
    """Enregistre la copie du jour si elle n'existe pas. Renvoie « enregistree », « deja » ou « ignore »."""
    ao = dossier["ao"]
    if ao.get("mode") != "reel" or rejeu.est_un_rejeu(ao["ref_annonce"]):
        return "ignore"
    preparer(ret)
    pr = dossier["probables"]["coefficients"]
    m = maintenant or datetime.now(timezone.utc)
    cur = ret.execute(
        "INSERT OR IGNORE INTO previsions (ref_annonce, lot, jour, cree_le, version, empreinte, reference, acheteur,"
        " objet, date_limite, prevision) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ao["ref_annonce"], ao.get("lot") or "", _jour(m), m.isoformat(timespec="seconds"), pr.get("version") or "",
         pr.get("empreinte") or "", ao.get("reference"), ao.get("acheteur"), ao.get("objet"), ao.get("date_limite"),
         json.dumps(copie(dossier), ensure_ascii=False, separators=(",", ":"))))
    ret.commit()
    return "enregistree" if cur.rowcount else "deja"


def deja_fige(ret, ref_annonce: str, lot: str | None, maintenant: datetime | None = None) -> bool:
    preparer(ret)
    return ret.execute("SELECT 1 FROM previsions WHERE ref_annonce = ? AND lot = ? AND jour = ?",
                       (ref_annonce, lot or "", _jour(maintenant))).fetchone() is not None


def lire(ret, ref_annonce: str, lot: str | None) -> list[dict]:
    preparer(ret)
    return [{**dict(r), "prevision": json.loads(r["prevision"])} for r in ret.execute(
        "SELECT * FROM previsions WHERE ref_annonce = ? AND lot = ? ORDER BY jour", (ref_annonce, lot or ""))]


# --------------------------------------------------------------------------- le PV réel

def _par_ref(mem: probables.Memoire) -> dict[str, list[int]]:
    index = getattr(mem, "_suivi_par_ref", None)
    if index is None:
        index = {}
        for i, u in enumerate(mem.unites):
            index.setdefault(u.ref, []).append(i)
        mem._suivi_par_ref = index
    return index


def reel(mem: probables.Memoire, ref_annonce: str, lot: str | None) -> dict | None:
    """Le PV de cet AO (ou de ce lot) s'il est dans l'historique, sinon None."""
    for i in _par_ref(mem).get("c" + str(ref_annonce), []):
        u = mem.unites[i]
        if (u.lot or "") != (lot or ""):
            continue
        offres, vus = [], set()
        for c in u.concurrents:
            if c["cle"] and c["cle"] not in vus:
                vus.add(c["cle"])
                offres.append({"cle": c["cle"], "nom": c["nom"], "statut": c["statut"], "taux": c.get("taux")})
        gagnant = next((o for o in offres if o["cle"] == u.cle_gagnant), None)
        return {"ref_pv": u.ref, "lot": u.lot, "date": u.date, "nb_concurrents": len(offres),
                "concurrents": offres, "gagnant": gagnant, "prix_reference_taux": mem.taux_reference(u),
                "_indice": i}
    return None


def choisir_copie(copies: list[dict], r: dict | None) -> dict | None:
    """La dernière copie figée au plus tard le jour de l'ouverture des plis, sinon la première."""
    if not copies:
        return None
    if r and r.get("date"):
        jour = f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:8]}"
        avant = [c for c in copies if c["jour"] <= jour]
        if avant:
            return avant[-1]
    return copies[0]


def zone_reelle(u, sim: dict | None) -> dict | None:
    """Notre offre au point recommandé de la copie figée, insérée parmi les vraies offres du PV."""
    from .donnees import CLE_FICTIVE, rejouer_attribution
    if not sim or not sim.get("zone") or not u.estimation or not u.cle_gagnant:
        return None
    x = sim["zone"]["x"]
    return {"x": x, "chance_annoncee": sim["zone"]["chance"],
            "gagnee": rejouer_attribution(u, x).cle_gagnant == CLE_FICTIVE}


def comparer(prev: dict, r: dict) -> dict:
    """Prévu / réel : sociétés venues, rang, nombre de concurrents, gagnant et son groupe."""
    cands = sorted(prev["candidats"], key=lambda c: (-c["p"], c["cle"]))
    rang = {c["cle"]: k for k, c in enumerate(cands, 1)}
    par_cle = {c["cle"]: c for c in cands}
    groupe_de = {m: g["numero"] for g in prev["groupes"] for m in g["membres"]}
    venues = {o["cle"] for o in r["concurrents"]}
    top = {c["cle"] for c in cands[:TOP]}
    prevue = lambda k: k in par_cle and (par_cle[k]["dans_liste"] or par_cle[k]["association"])  # noqa: E731
    societes = [{**o, "prevue": prevue(o["cle"]), "rang": rang.get(o["cle"]),
                 "p": par_cle[o["cle"]]["p"] if o["cle"] in par_cle else None, "groupe": groupe_de.get(o["cle"])}
                for o in r["concurrents"]]
    absentes = [{"cle": c["cle"], "nom": c["nom"], "p": c["p"], "rang": rang[c["cle"]], "groupe": groupe_de.get(c["cle"])}
                for c in cands if prevue(c["cle"]) and c["cle"] not in venues]
    conc = prev["concurrents"]
    n = r["nb_concurrents"]
    g = r["gagnant"]
    return {
        "societes": societes, "prevues_absentes": absentes,
        "venues_prevues": sum(1 for s in societes if s["prevue"]), "venues": n,
        "dans_top15": len(venues & top),
        "nb_concurrents": n, "fourchette": [conc["bas"], conc["haut"]], "probable": conc["probable"],
        "dans_fourchette": conc["bas"] <= n <= conc["haut"], "ecart": abs(conc["probable"] - n),
        "gagnant": None if not g else {**g, "prevu": prevue(g["cle"]), "rang": rang.get(g["cle"]),
                                       "groupe": groupe_de.get(g["cle"])},
        "groupes": [{"numero": gr["numero"], "chef": gr["chef"], "membres": len(gr["membres"]),
                     "venus": [m for m in gr["membres"] if m in venues]} for gr in prev["groupes"]],
        "prix_reference": _prix_compare(prev.get("simulation"), r.get("prix_reference_taux")),
        "zone": r.get("zone"),
    }


def _prix_compare(sim: dict | None, vrai: float | None) -> dict | None:
    if not sim or vrai is None or sim["prix_reference"]["mediane"] is None:
        return None
    p = sim["prix_reference"]
    return {"vrai": vrai, "mediane": p["mediane"], "q10": p["q10"], "q90": p["q90"],
            "dans80": p["q10"] <= vrai <= p["q90"], "ecart": vrai - p["mediane"]}


def etat(ret, mem: probables.Memoire, ref_annonce: str, lot: str | None) -> dict | None:
    """La réponse de GET /api/copilote/prevision/{ref}?lot= : les copies, la copie comparée, le PV et le score."""
    copies = lire(ret, ref_annonce, lot)
    if not copies:
        return None
    r = reel(mem, ref_annonce, lot)
    choisie = choisir_copie(copies, r)
    if r is not None:
        r["zone"] = zone_reelle(mem.unites[r.pop("_indice")], choisie["prevision"].get("simulation"))
    return {"ref_annonce": ref_annonce, "lot": lot,
            "copies": [{"jour": c["jour"], "cree_le": c["cree_le"], "version": c["version"],
                        "empreinte": c["empreinte"]} for c in copies],
            "comparee": {k: choisie[k] for k in ("jour", "cree_le", "version", "empreinte")},
            "prevision": choisie["prevision"], "reel": r,
            "score": comparer(choisie["prevision"], r) if r else None}


# --------------------------------------------------------------------------- ligne de commande

def lister(ret, mem: probables.Memoire) -> list[dict]:
    preparer(ret)
    sortie = []
    for r in ret.execute("SELECT ref_annonce, lot, MIN(jour) premier, MAX(jour) dernier, COUNT(*) n,"
                         " MAX(version) version, MAX(acheteur) acheteur, MAX(date_limite) date_limite"
                         " FROM previsions GROUP BY ref_annonce, lot ORDER BY premier, ref_annonce, lot"):
        e = etat(ret, mem, r["ref_annonce"], r["lot"] or None)
        sortie.append({**dict(r), "score": e["score"] if e else None})
    return sortie


def _figer_cli(refs: list[str]) -> int:
    from . import analyse
    code = 0
    with closing(bases.calcul()) as calc, closing(bases.retours()) as ret, closing(bases.ao_ouverts()) as ao:
        for brut in refs:
            ref, _, lot = brut.partition(":")
            try:
                d = analyse.analyser(ref, lot or None, ouvrir_historique=bases.historique,
                                     chemin_historique=bases.chemin_historique(), ao=ao, calc=calc, ret=ret)
            except Exception as e:                                  # noqa: BLE001
                print(f"{brut} : analyse impossible — {type(e).__name__}: {e}")
                code = 1
                continue
            print(f"{brut} : {figer(ret, d)}")
    return code


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if argv[:1] == ["figer"]:
        return _figer_cli(argv[1:])
    p = argparse.ArgumentParser(prog="python -m copilote.suivi")
    p.add_argument("--detail", metavar="REF[:LOT]")
    a = p.parse_args(argv)
    mem = probables.memoire(bases.historique, bases.chemin_historique())
    with closing(bases.retours()) as ret:
        if a.detail:
            ref, _, lot = a.detail.partition(":")
            e = etat(ret, mem, ref, lot or None)
            print(json.dumps(e, ensure_ascii=False, indent=1) if e else "Aucune prévision figée pour cet AO.")
            return 0
        lignes = lister(ret, mem)
    if not lignes:
        print("Aucune prévision figée.")
        return 0
    print(f"{'AO':<12} {'lot':<4} {'figé le':<10} {'copies':>6} {'version':<7} {'PV':<3} "
          f"{'venues prévues':>14} {'top 15':>6} {'nombre':>8} {'fourchette':>10} {'gagnant':>10}")
    for l in lignes:
        s = l["score"]
        if not s:
            print(f"{l['ref_annonce']:<12} {l['lot'] or '-':<4} {l['premier']:<10} {l['n']:>6} {l['version']:<7} non")
            continue
        g = s["gagnant"]
        gagnant = "—" if not g else (f"rang {g['rang']}" if g["rang"] else "non prévu")
        print(f"{l['ref_annonce']:<12} {l['lot'] or '-':<4} {l['premier']:<10} {l['n']:>6} {l['version']:<7} oui "
              f"{s['venues_prevues']:>6} sur {s['venues']:<5} {s['dans_top15']:>6} {s['nb_concurrents']:>8} "
              f"{s['fourchette'][0]:>4}-{s['fourchette'][1]:<3}{'✓' if s['dans_fourchette'] else '✗'} {gagnant:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
