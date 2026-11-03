"""REJEU-R : le test sur des PV récents, que le modèle n'a jamais vus. Une seule commande, quand le portail répond :

    python -m copilote.rejeu_recent                 tout : mise à jour, choix, envoi, mesure, rapport
    python -m copilote.rejeu_recent --sans-maj      sans relancer la mise à jour (PV déjà récupérés)
    python -m copilote.rejeu_recent mesurer         la mesure seule, sur les REJEU-R déjà choisis
    python -m copilote.rejeu_recent etat            ce qui est choisi, envoyé, mesuré

Étapes :
  a) maj_quotidienne.py --base travaux --jours J --relance 0 : les fiches et les PV publiés depuis `--depuis`
     (01/10/2026 par défaut), J couvrant cette date avec 30 jours de marge. Compte les nouveaux marchés avec PV.
     Portail injoignable (code 3) : on s'arrête, rien n'est changé.
  b) Les coefficients ne sont PAS réappris : la commande vérifie que les deux versions mesurées (A et A2) ont
     été apprises sur des données antérieures à `--depuis` (champ appris_jusqu_au, ou ajuste_sur.annee).
  c) 30 marchés ouverts depuis `--depuis`, dont au moins 3 allotis, variés en acheteur (un seul marché par
     acheteur), famille de travaux, classe et montant : même mécanique que rejouer_ao (annonce sans résultat,
     coupure la veille de l'ouverture, vérité rangée à part dans rejeu_verite). Ils s'appellent REJEU-R-01 à
     REJEU-R-30 et sont envoyés à l'API locale sous le client « Test » (JETON_SERVICE) : /ao-ouverts les
     montre dans « AO de test », malgré le client imposé.
  d) Mesure, versions A puis A2, sur ces marchés : part des vrais soumissionnaires dans le top 15, vrai gagnant
     dans le top 30, vrai nombre dans la fourchette 80 %, erreur médiane sur le nombre, membres de groupe
     retrouvés. La mesure lit les annonces dans une base en mémoire faite de rejeu_ao : elle ne dépend pas du
     serveur. → rapports/copilote_rejeu_recent.json et .md
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from . import bases, config, mesures_probables, nature, probables, rejeu
from . import rejouer_ao as ra

PREFIXE = "REJEU-R-"
DEPUIS = "2026-10-01"
NOMBRE = 30
ALLOTIS = 3
SORTIE_JSON = config.RAPPORTS / "copilote_rejeu_recent.json"
SORTIE_MD = config.RAPPORTS / "copilote_rejeu_recent.md"
ICI = Path(__file__).resolve().parent.parent

SCHEMA_AO = """
CREATE TABLE appels_ouverts (ref_annonce TEXT PRIMARY KEY, org_acronyme TEXT, reference TEXT, objet TEXT,
  acheteur TEXT, cle_acheteur TEXT, publie_le TEXT, date_limite TEXT, estimation REAL, caution_provisoire REAL,
  lieu TEXT, url TEXT, source TEXT, recu_le TEXT, maj_le TEXT, brut TEXT);
CREATE TABLE appels_clients (ref_annonce TEXT, client TEXT, ajoute_le TEXT, PRIMARY KEY (ref_annonce, client));
CREATE TABLE appels_lots (ref_annonce TEXT, lot TEXT, intitule TEXT, estimation REAL, caution_provisoire REAL,
  PRIMARY KEY (ref_annonce, lot));
CREATE TABLE appels_qualifications (ref_annonce TEXT, libelle TEXT, classe TEXT);
"""
CRITERES = (("rappel_top15", "vrais soumissionnaires dans le top 15", "pct"),
            ("gagnant_top30", "vrai gagnant dans le top 30", "pct"),
            ("dans_fourchette", "vrai nombre dans la fourchette 80 %", "pct"),
            ("ecart_median", "erreur médiane sur le nombre de concurrents", "nb"),
            ("largeur_fourchette", "largeur moyenne de la fourchette", "nb"),
            ("membres_retrouves", "membres de groupe retrouvés", "pct"))


class Arret(RuntimeError):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- b) coefficients figés

def appris_jusqu_au(coefs: dict) -> str | None:
    if coefs.get("appris_jusqu_au"):
        return coefs["appris_jusqu_au"]
    annee = (coefs.get("ajuste_sur") or {}).get("annee")
    return f"{annee}-12-31" if annee else None


def verifier_coefficients(depuis: str, coefs: dict[str, dict]) -> dict[str, str]:
    """Chaque version doit avoir été apprise sur des données antérieures à `depuis`. Rien n'est réappris."""
    dates = {}
    for nom, c in coefs.items():
        d = appris_jusqu_au(c)
        if not d:
            raise Arret(f"version {nom} : date d'apprentissage inconnue — on ne mesure pas une version dont on ne "
                        "sait pas si elle a vu les marchés testés.")
        if d >= depuis:
            raise Arret(f"version {nom} apprise jusqu'au {d}, après le {depuis} : les marchés testés auraient servi "
                        "à l'apprentissage. Remettre les coefficients appris avant cette date.")
        dates[nom] = d
    return dates


# --------------------------------------------------------------------------- a) mise à jour

def compter_marches(hist: sqlite3.Connection, depuis: str) -> dict:
    """Marchés principaux ouverts depuis `depuis`, et ceux qui ont des offres (un PV)."""
    d = depuis.replace("-", "")
    r = hist.execute("SELECT COUNT(*) n, SUM(EXISTS (SELECT 1 FROM concurrents c WHERE c.ref = m.ref)) avec_pv"
                     " FROM marches m WHERE m.principale = 1 AND m.tri_date >= ?", (d,)).fetchone()
    return {"marches": r[0] or 0, "avec_pv": r[1] or 0}


def lancer_maj(depuis: str, aujourdhui: date | None = None, lancer=subprocess.call) -> int:
    jours = ((aujourdhui or date.today()) - date.fromisoformat(depuis)).days + 30
    commande = [sys.executable, "-u", str(ICI / "maj_quotidienne.py"), "--base", "travaux",
                "--jours", str(max(jours, 30)), "--relance", "0"]
    print("-> " + " ".join(commande[2:]), flush=True)
    return lancer(commande, cwd=ICI)


# --------------------------------------------------------------------------- c) le choix

def candidats_recents(hist: sqlite3.Connection, depuis: str, aujourdhui: date | None = None) -> list[ra.Candidat]:
    d = depuis.replace("-", "")
    fin = (aujourdhui or date.today()).year
    sortie = []
    for annee in range(int(depuis[:4]), fin + 1):
        sortie += [c for c in ra.candidats(hist, str(annee)) if c.date >= d]
    return sortie


def famille(c: ra.Candidat) -> str:
    texte = " ".join([c.objet] + [l.get("intitule") or "" for l in c.lots])
    return nature.classer(texte)[0] or "?"


class _TirageVarie(ra._Tirage):
    """Le tirage de rejouer_ao (un acheteur au plus une fois), les familles les moins représentées d'abord."""

    def __init__(self, graine: int):
        super().__init__(graine)
        self.familles: Counter = Counter()

    def prendre(self, pool, *filtres):
        deja = {p.ref for p in self.pris}
        libres = [c for c in pool if c.cle_acheteur not in self.acheteurs and c.ref not in deja]
        for f in filtres:
            ok = [c for c in libres if f(c)]
            if ok:
                c = min(ok, key=lambda c: (self.familles[famille(c)], self.mois[c.mois], ra._hash(self.graine, c.ref)))
                self.pris.append(c)
                self.acheteurs.add(c.cle_acheteur)
                self.mois[c.mois] += 1
                self.familles[famille(c)] += 1
                return c
        return None


def choisir(cands: list[ra.Candidat], n: int = NOMBRE, nb_allotis: int = ALLOTIS, graine: int = 0) -> list[ra.Candidat]:
    """Comme rejouer_ao.choisir (allotis d'abord, puis classes et terciles de montant), en variant aussi la famille."""
    return ra.choisir(cands, n, nb_allotis, graine, tirage=_TirageVarie)


def enregistrer(calc: sqlite3.Connection, choisis: list[ra.Candidat], renouveler: bool = False) -> list[str]:
    """Range les REJEU-R dans rejeu_ao et rejeu_verite, sans toucher aux autres REJEU-."""
    ra.preparer(calc)
    maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
    refs = [f"{PREFIXE}{i:02d}" for i in range(1, len(choisis) + 1)]
    with calc:
        if renouveler:
            calc.execute("DELETE FROM rejeu_ao WHERE ref_annonce LIKE ?", (PREFIXE + "%",))
            calc.execute("DELETE FROM rejeu_verite WHERE ref_annonce LIKE ?", (PREFIXE + "%",))
        for c, ref in zip(choisis, refs):
            calc.execute("INSERT OR REPLACE INTO rejeu_ao (ref_annonce, date_coupure, annonce, cree_le) VALUES (?,?,?,?)",
                         (ref, rejeu.veille(c.date), json.dumps(ra.annonce(c, ref), ensure_ascii=False), maintenant))
            calc.execute("DELETE FROM rejeu_verite WHERE ref_annonce = ?", (ref,))
            calc.executemany("INSERT INTO rejeu_verite VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             ra.verite(c, ref, maintenant))
    return refs


def deja_choisis(calc: sqlite3.Connection) -> list[str]:
    ra.preparer(calc)
    return [r[0] for r in calc.execute("SELECT ref_annonce FROM rejeu_ao WHERE ref_annonce LIKE ? ORDER BY ref_annonce",
                                       (PREFIXE + "%",))]


def annonces(calc: sqlite3.Connection) -> list[dict]:
    return [a for a in ra.annonces_a_envoyer(calc) if a["ref_annonce"].startswith(PREFIXE)]


def envoyer(calc: sqlite3.Connection, url: str, jeton: str, envoi=ra.poster) -> dict:
    """Les seuls REJEU-R, à l'API locale (jamais à un serveur distant), sous le client « Test »."""
    if not jeton:
        raise ra.ErreurEnvoi("JETON_SERVICE n'est pas défini dans l'environnement.")
    if not ra.est_local(url):
        raise ra.ErreurEnvoi(f"{url} n'est pas une adresse locale : les REJEU-R ne vont qu'au site local.")
    a = annonces(calc)
    if not a:
        raise ra.ErreurEnvoi("Aucun REJEU-R à envoyer.")
    statut, reponse = envoi(url, jeton, {"clients": [ra.CLIENT], "appels_ouverts": a})
    if statut != 200:
        raise ra.ErreurEnvoi(f"Réponse {statut} : {json.dumps(reponse, ensure_ascii=False)[:300]}")
    refus = {x.get("ref_annonce") for x in reponse.get("rejetes", [])}
    with calc:
        for x in a:
            if x["ref_annonce"] not in refus:
                calc.execute("UPDATE rejeu_ao SET envoye_le = ? WHERE ref_annonce = ?",
                             (datetime.now(timezone.utc).isoformat(timespec="seconds"), x["ref_annonce"]))
    return reponse


# --------------------------------------------------------------------------- d) la mesure

def ao_en_memoire(calc: sqlite3.Connection):
    """Une base « ao_ouverts » en mémoire, faite des annonces REJEU-R : la mesure n'attend pas le serveur."""
    def ouvrir() -> sqlite3.Connection:
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA_AO)
        for a in annonces(calc):
            db.execute("INSERT INTO appels_ouverts (ref_annonce, reference, objet, acheteur, cle_acheteur, publie_le,"
                       " date_limite, estimation, caution_provisoire, source, maj_le) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (a["ref_annonce"], a["reference"], a["objet"], a["acheteur"], bases.cle_acheteur(a["acheteur"]),
                        a["publie_le"], a["date_limite"], a["estimation"], a["caution_provisoire"], a["source"],
                        a["publie_le"]))
            db.execute("INSERT INTO appels_clients VALUES (?,?,?)", (a["ref_annonce"], ra.CLIENT, a["publie_le"]))
            for l in a.get("lots", []):
                db.execute("INSERT INTO appels_lots VALUES (?,?,?,?,?)",
                           (a["ref_annonce"], l["lot"], l["intitule"], l["estimation"], l["caution_provisoire"]))
            for q in a.get("qualifications", []):
                db.execute("INSERT INTO appels_qualifications VALUES (?,?,?)", (a["ref_annonce"], q["libelle"], q["classe"]))
        db.commit()
        return db
    return ouvrir


def mesurer(calc: sqlite3.Connection, coefs: dict[str, dict] | None = None) -> dict:
    return mesures_probables.rejeux(prefixe=PREFIXE, coefs=coefs or mesures_probables.versions(), exclure=None,
                                    ouvrir_ao=ao_en_memoire(calc))


def mesurer_simulation(calc: sqlite3.Connection) -> dict:
    """Étape C sur les REJEU-R : prix de référence, calibration, zone recommandée contre taux fixes."""
    from . import mesures_simulation
    r = mesures_simulation.rejeux(prefixe=PREFIXE, exclure=None, ouvrir_ao=ao_en_memoire(calc))
    r.pop("_lignes", None)
    return r


def _rapport_simulation(sim: dict) -> list[str]:
    r = sim["resume"]
    f = lambda v: _fmt(v, "pct")                                                    # noqa: E731
    pts = lambda v: "—" if v is None else f"{100 * v:.2f} pt".replace(".", ",")   # noqa: E731
    lignes = ["", "## Étape C — simulation de l'ouverture des plis", "",
              f"{r.get('avec_reference', 0)} marchés ou lots avec un prix de référence, {r.get('attribues', 0)} attribués.", "",
              "| critère | mesuré | seuil |", "|---|---:|---|",
              f"| vrai prix de référence dans l'intervalle 80 % | {f(r.get('dans80'))} | 70 à 90 % |",
              f"| écart moyen de la médiane au vrai prix de référence | {pts(r.get('mae'))} | "
              f"< méthode de l'étape 4 : {pts(r.get('mae_etape4'))} |",
              f"| écart médian | {pts(r.get('erreur_mediane'))} | étape 4 : {pts(r.get('erreur_mediane_etape4'))} |", "",
              "Calibration de la chance annoncée :", "",
              "| annoncée | n | moyenne annoncée | réelle | écart |", "|---|---:|---:|---:|---:|"]
    for b in r.get("calibration", []):
        ecart = f"{b['ecart_points']:+.1f} pt".replace(".", ",")
        lignes.append(f"| {round(100 * b['de'])}–{round(100 * b['a'])} % | {b['n']} | {f(b['annoncee'])} | "
                      f"{f(b['reelle'])} | {ecart} |")
    lignes += ["", "Zone recommandée contre le meilleur taux fixe :", "",
               "| offres | n | zone | meilleur taux fixe | ses victoires |", "|---|---:|---:|---:|---:|"]
    for t, v in r.get("zone_contre_fixes", {}).items():
        if v.get("n"):
            lignes.append(f"| {t} | {v['n']} | {f(v['zone'])} | {str(v['meilleur_fixe']).replace('.', ',')} | "
                          f"{f(v['victoires_meilleur_fixe'])} |")
    return lignes


def _fmt(v, genre: str) -> str:
    if v is None:
        return "—"
    return f"{100 * v:.1f} %".replace(".", ",") if genre == "pct" else f"{v:.1f}".replace(".", ",")


def rapport_md(res: dict, infos: dict) -> str:
    v = res["versions"]
    lignes = ["# REJEU-R — test sur des PV récents, jamais vus par le modèle", "",
              f"Produit le {date.today().strftime('%d/%m/%Y')} par `python -m copilote.rejeu_recent`. "
              f"Marchés ouverts depuis le {infos['depuis']}, coupure la veille de chaque ouverture, vérité cachée au moteur.",
              f"Coefficients non réappris : {', '.join(f'{k} appris jusqu’au {d}' for k, d in infos['coefficients'].items())}.",
              ""]
    if infos.get("maj"):
        m = infos["maj"]
        lignes += [f"Mise à jour : {m['avant']['avec_pv']} → {m['apres']['avec_pv']} marchés avec PV depuis le "
                   f"{infos['depuis']} (**{m['apres']['avec_pv'] - m['avant']['avec_pv']} nouveaux**).", ""]
    lignes += [f"{infos['choisis']} AO choisis ({infos['allotis']} allotis), {v[next(iter(v))]['resume'].get('n', 0)} "
               "marchés ou lots mesurés.", "", "| critère | A (avant) | A2 (après) |", "|---|---:|---:|"]
    for cle, libelle, genre in CRITERES:
        lignes.append(f"| {libelle} | {_fmt(v.get('A', {}).get('resume', {}).get(cle), genre)} | "
                      f"{_fmt(v.get('A2', {}).get('resume', {}).get(cle), genre)} |")
    if res.get("erreurs"):
        lignes += ["", "Erreurs :"] + [f"- {e}" for e in res["erreurs"]]
    if res.get("simulation"):
        lignes += _rapport_simulation(res["simulation"])
        if res["simulation"].get("erreurs"):
            lignes += ["", "Erreurs (simulation) :"] + [f"- {e}" for e in res["simulation"]["erreurs"]]
    return "\n".join(lignes) + "\n"


def ecrire(res: dict, infos: dict) -> None:
    config.RAPPORTS.mkdir(parents=True, exist_ok=True)
    with open(SORTIE_JSON, "w", encoding="utf-8") as f:
        json.dump({"infos": infos, **res}, f, ensure_ascii=False, indent=1)
    SORTIE_MD.write_text(rapport_md(res, infos), encoding="utf-8")


# --------------------------------------------------------------------------- ligne de commande

def tout(args, lancer=subprocess.call, envoi=ra.poster) -> int:
    coefs = mesures_probables.versions()
    infos = {"depuis": args.depuis, "coefficients": verifier_coefficients(args.depuis, coefs)}
    with closing(bases.historique()) as hist:
        avant = compter_marches(hist, args.depuis)
    if not args.sans_maj:
        code = lancer_maj(args.depuis, lancer=lancer)
        if code == 3:
            raise Arret("le portail ne répond pas : mise à jour impossible, rien n'a été changé. Relancer plus tard.", 3)
        if code != 0:
            raise Arret(f"la mise à jour a échoué (code {code}) : voir son journal ci-dessus.", code)
    with closing(bases.historique()) as hist:
        apres = compter_marches(hist, args.depuis)
        infos["maj"] = {"avant": avant, "apres": apres}
        print(f"marchés avec PV ouverts depuis le {args.depuis} : {avant['avec_pv']} -> {apres['avec_pv']} "
              f"({apres['avec_pv'] - avant['avec_pv']} nouveaux)", flush=True)
        with closing(bases.calcul()) as calc:
            if deja_choisis(calc) and not args.renouveler:
                print(f"{len(deja_choisis(calc))} REJEU-R déjà choisis : conservés (--renouveler pour refaire le choix).")
            else:
                cands = candidats_recents(hist, args.depuis)
                choisis = choisir(cands, args.n, min(args.allotis, args.n), args.graine)
                if len(choisis) < args.n:
                    print(f"ATTENTION : {len(cands)} marchés rejouables seulement, {len(choisis)} choisis sur {args.n}.")
                refs = enregistrer(calc, choisis, renouveler=True)
                ra.afficher(choisis, refs)
    return mesurer_et_ecrire(args, infos, envoi)


def mesurer_et_ecrire(args, infos: dict, envoi=ra.poster) -> int:
    with closing(bases.calcul()) as calc:
        refs = deja_choisis(calc)
        if not refs:
            raise Arret("aucun REJEU-R choisi : lancer d'abord la commande sans argument.")
        a = annonces(calc)
        infos.update(choisis=len(refs), allotis=sum(1 for x in a if x.get("lots")),
                     familles=dict(Counter(nature.classer(x["objet"])[0] or "?" for x in a)))
        infos.setdefault("coefficients", verifier_coefficients(args.depuis, mesures_probables.versions()))
        if not args.sans_envoi:
            try:
                r = envoyer(calc, args.url, os.environ.get("JETON_SERVICE", ""), envoi)
                print(f"envoyés à {args.url} : {r.get('recus')} reçus, {len(r.get('rejetes', []))} rejetés.")
            except ra.ErreurEnvoi as e:
                print(f"envoi au site local impossible ({e}). La mesure continue ; pour les voir dans /ao-ouverts, lancer "
                      "le site puis `python -m copilote.rejeu_recent envoyer`.")
        res = mesurer(calc)
        res["simulation"] = mesurer_simulation(calc)
    ecrire(res, infos)
    for nom, v in res["versions"].items():
        r = v["resume"]
        print(f"{nom} : " + ", ".join(f"{lib} {_fmt(r.get(cle), g)}" for cle, lib, g in CRITERES))
    print(f"-> {SORTIE_MD}")
    return 0


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="python -m copilote.rejeu_recent", description=__doc__.split("\n")[0])
    p.add_argument("quoi", nargs="?", default="tout", choices=("tout", "mesurer", "envoyer", "etat"))
    p.add_argument("--depuis", default=DEPUIS, help=f"marchés ouverts à partir de cette date (défaut {DEPUIS})")
    p.add_argument("--n", type=int, default=NOMBRE)
    p.add_argument("--allotis", type=int, default=ALLOTIS)
    p.add_argument("--graine", type=int, default=0)
    p.add_argument("--sans-maj", action="store_true", help="ne pas relancer la mise à jour")
    p.add_argument("--sans-envoi", action="store_true", help="ne pas envoyer les AO au site local")
    p.add_argument("--renouveler", action="store_true", help="refaire le choix des REJEU-R")
    p.add_argument("--url", default=ra.URL_DEFAUT)
    a = p.parse_args(argv)
    try:
        if a.quoi == "etat":
            with closing(bases.calcul()) as calc:
                refs = deja_choisis(calc)
                print(f"{len(refs)} REJEU-R choisis : {', '.join(refs) or '—'}")
            print(SORTIE_MD.read_text(encoding="utf-8") if SORTIE_MD.exists() else "Pas encore mesuré.")
            return 0
        if a.quoi == "envoyer":
            with closing(bases.calcul()) as calc:
                r = envoyer(calc, a.url, os.environ.get("JETON_SERVICE", ""))
            print(f"envoyés : {r.get('recus')} reçus, {len(r.get('rejetes', []))} rejetés.")
            return 0
        if a.quoi == "mesurer":
            return mesurer_et_ecrire(a, {"depuis": a.depuis})
        return tout(a)
    except Arret as e:
        print(f"ARRÊT : {e}", file=sys.stderr)
        return e.code
    except ra.ErreurEnvoi as e:
        print(f"Envoi impossible : {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
