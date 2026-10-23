"""Jeu de test : des marchés déjà attribués, renvoyés à l'API comme des AO tout neufs.

    python -m copilote.rejouer_ao                       choisir (si ce n'est pas fait) puis envoyer
    python -m copilote.rejouer_ao choisir [--n 20] [--graine 0] [--renouveler]
    python -m copilote.rejouer_ao envoyer [--url http://127.0.0.1:8000]
    python -m copilote.rejouer_ao groupes               calcule les groupes « jusqua-… » de chaque AO rejoué
    python -m copilote.rejouer_ao etat

Le jeton de service se lit dans la variable JETON_SERVICE (jamais affiché, jamais rangé). Par défaut
l'envoi n'est accepté que vers la machine locale : ces AO n'ont rien à faire dans la base d'un
serveur qui sert de vrais clients (--distant pour l'imposer).

Deux endroits, deux rôles, dans copilote_calcul.db :
  rejeu_ao      ce que le moteur peut savoir : l'annonce et la date de coupure (voir rejeu.py) ;
  rejeu_verite  ce qui s'est réellement passé : concurrents, taux, prix de référence, gagnant, et la
                référence réelle du marché. Seul CE module la lit (tests/test_rejeu.py le vérifie) ;
                elle est destinée à la page d'analyse, pour le bloc « Ce qui s'est réellement passé ».

L'annonce envoyée ne contient ni la référence réelle, ni aucun résultat. Elle est la même à chaque
envoi ; seules les dates (publication, date limite) sont posées à l'envoi : la date limite est
toujours dans le futur, sans quoi l'AO disparaîtrait de la page « En cours ».
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import statistics
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from construire_base import cle_plate, offre_de

from . import bases, groupes, rejeu
from .donnees import Unite, charger_unites, rejouer_attribution

NOMBRE = 20
ALLOTIS = 3
ANNEE = "2026"
MIN_OFFRES = 3                  # offres chiffrées par marché ou lot : sinon l'ouverture des plis n'a rien à rejouer
MAX_LOTS = 6
CLIENT = "Test"
URL_DEFAUT = os.environ.get("SITE_URL", "http://127.0.0.1:8000")
LOCAL = ("localhost", "127.0.0.1", "::1")

SCHEMA_VERITE = """
CREATE TABLE IF NOT EXISTS rejeu_verite (
  ref_annonce TEXT NOT NULL, lot TEXT NOT NULL,            -- lot '' pour un marché ordinaire
  ref_reel TEXT NOT NULL, reference_reelle TEXT, date_ouverture TEXT, acheteur TEXT,
  estimation REAL, prix_reference_stocke REAL, prix_reference REAL, moyenne_offres REAL, nb_offres INTEGER,
  cle_gagnant TEXT, nom_gagnant TEXT, taux_gagnant REAL,   -- l'attributaire réel
  cle_gagnant_regle TEXT,                                  -- celui que la règle de la commission, rejouée, désigne
  concurrents TEXT NOT NULL,                               -- JSON : nom, cle, statut, montant, taux, rang
  cree_le TEXT NOT NULL, PRIMARY KEY (ref_annonce, lot));
"""


# --------------------------------------------------------------------------- candidats

@dataclass
class Candidat:
    ref: str
    reference: str
    acheteur: str
    objet: str
    date: str                                   # date d'ouverture réelle, AAAAMMJJ
    caution: float | None
    nb_lots: int
    unites: list[Unite]
    qualifications: list[dict]                  # [{"libelle": "B.1", "classe": "3"}], format de l'AO
    lots: list[dict] = field(default_factory=list)
    classe: int | None = None                   # la plus haute des classes demandées
    montant: float = 0.0                        # estimation (somme des lots pour un marché alloti)

    @property
    def cle_acheteur(self) -> str:
        return bases.cle_acheteur(self.acheteur)

    @property
    def mois(self) -> str:
        return self.date[:6]


_CODE = re.compile(r"^\s*([A-Z]{1,2}\.\d+)")


def _qualification(libelle: str | None, classe: str | None) -> dict:
    """« B.1- Travaux de terrassements… » + « 3 » → {"libelle": "B.1", "classe": "3"} : le format de l'AO."""
    m = _CODE.match(libelle or "")
    return {"libelle": m.group(1) if m else (libelle or "").strip() or None, "classe": (classe or "").strip() or None}


def _jugeable(u: Unite) -> bool:
    """Une unité qu'on peut rejouer : attribuée, estimée, assez d'offres chiffrées, prix de référence calculable."""
    if not (u.cle_gagnant and u.estimation and u.estimation > 0):
        return False
    if sum(1 for c in u.concurrents if c["taux"]) < MIN_OFFRES:
        return False
    return rejouer_attribution(u).reference is not None


def _cite_des_noms(c: Candidat) -> bool:
    """Vrai si le texte de l'annonce nomme un concurrent : elle dirait alors qui a soumissionné."""
    texte = cle_plate(" ".join([c.objet, c.acheteur] + [l.get("intitule") or "" for l in c.lots]))
    return any(len(k["cle"]) >= 6 and k["cle"] in texte for u in c.unites for k in u.concurrents if k["cle"])


def candidats(db: sqlite3.Connection, annee: str = ANNEE) -> list[Candidat]:
    """Les marchés attribués de l'année qu'on peut rejouer : simples (avec classe) ou allotis (2 à 6 lots)."""
    par_ref: dict[str, list[Unite]] = defaultdict(list)
    for u in charger_unites(db, annee=annee):
        par_ref[u.ref].append(u)

    lots_de: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in db.execute("SELECT l.ref, l.lot, l.intitule, l.estimation, l.caution_provisoire, l.classes FROM lots l"
                        " JOIN marches m ON m.ref = l.ref WHERE m.principale = 1 AND m.tri_date LIKE ?"
                        " ORDER BY l.ref, l.rowid", (annee + "%",)):
        lots_de[r["ref"]].append(r)
    qualifs: dict[str, list[tuple]] = defaultdict(list)
    for r in db.execute("SELECT q.ref, q.qualification, q.classe FROM qualifications q ORDER BY q.ref, q.rowid"):
        qualifs[r["ref"]].append((r["qualification"], r["classe"]))

    sortie = []
    for m in db.execute("SELECT ref, reference, acheteur, objet, tri_date, caution_provisoire, nb_lots"
                        " FROM marches WHERE principale = 1 AND date_douteuse = 0 AND COALESCE(infructueux, 0) = 0"
                        " AND tri_date LIKE ? ORDER BY ref", (annee + "%",)):
        unites = par_ref.get(m["ref"], [])
        if not unites or not m["acheteur"] or not (m["objet"] or "").strip():
            continue
        alloti = (m["nb_lots"] or 0) > 0
        if alloti and not 2 <= m["nb_lots"] <= MAX_LOTS:
            continue
        if not all(_jugeable(u) for u in unites):
            continue
        qs = [_qualification(q, c) for q, c in qualifs.get(m["ref"], [])]
        lots = []
        if alloti:
            presents = {u.lot for u in unites}
            lots = [{"lot": str(r["lot"]), "intitule": r["intitule"], "estimation": r["estimation"],
                     "caution_provisoire": r["caution_provisoire"], "classes": r["classes"]}
                    for r in lots_de.get(m["ref"], []) if str(r["lot"]) in presents]
            if len(lots) != len(unites):
                continue
            if not qs:                                    # pas de qualification au niveau du marché : celles des lots
                vues = sorted({x.strip() for l in lots for x in (l["classes"] or "").split(",") if x.strip()})
                qs = [{"libelle": None, "classe": x} for x in vues]
        classes = [int(q["classe"]) for q in qs if (q["classe"] or "").isdigit()]
        if not alloti and not classes:
            continue                                      # un marché simple se choisit par sa classe
        c = Candidat(m["ref"], m["reference"] or "", m["acheteur"], m["objet"].strip(), m["tri_date"],
                     m["caution_provisoire"], len(lots), unites, qs, lots,
                     max(classes) if classes else None, sum(u.estimation for u in unites))
        if not _cite_des_noms(c):
            sortie.append(c)
    return sortie


# --------------------------------------------------------------------------- choix

def _hash(graine: int, ref: str) -> str:
    return hashlib.sha256(f"{graine}:{ref}".encode()).hexdigest()


class _Tirage:
    """Tirage déterministe : un acheteur au plus une fois, les mois les moins représentés d'abord."""

    def __init__(self, graine: int):
        self.graine, self.acheteurs, self.mois, self.pris = graine, set(), Counter(), []

    def prendre(self, pool: list[Candidat], *filtres) -> Candidat | None:
        """Le premier filtre qui trouve quelqu'un l'emporte ; les suivants sont des assouplissements."""
        deja = {p.ref for p in self.pris}
        libres = [c for c in pool if c.cle_acheteur not in self.acheteurs and c.ref not in deja]
        for f in filtres:
            ok = [c for c in libres if f(c)]
            if ok:
                c = min(ok, key=lambda c: (self.mois[c.mois], _hash(self.graine, c.ref)))
                self.pris.append(c)
                self.acheteurs.add(c.cle_acheteur)
                self.mois[c.mois] += 1
                return c
        return None


def choisir(cands: list[Candidat], n: int = NOMBRE, nb_allotis: int = ALLOTIS, graine: int = 0) -> list[Candidat]:
    """n marchés variés en acheteur, classe, montant et mois, dont nb_allotis allotis. Même graine, même choix."""
    simples = [c for c in cands if c.nb_lots == 0]
    allotis = [c for c in cands if c.nb_lots > 0]
    t = _Tirage(graine)

    # Allotis d'abord (les plus rares) : 2 lots, 3 à 4 lots, 5 à 6 lots, tour à tour.
    tranches = [(2, 2), (3, 4), (5, MAX_LOTS)]
    for k in range(nb_allotis):
        bas, haut = tranches[k % len(tranches)]
        t.prendre(allotis, lambda c: bas <= c.nb_lots <= haut, lambda c: True)

    # Simples : autant de marchés par classe, puis un tiers de chaque tranche de montant dans chaque classe.
    n_simples = n - nb_allotis
    coupes = statistics.quantiles([c.montant for c in simples], n=3)
    tercile = lambda c: 0 if c.montant <= coupes[0] else 1 if c.montant <= coupes[1] else 2
    quotas = Counter({classe: n_simples // 5 for classe in range(1, 6)})
    for classe in [4, 3, 5, 2, 1][: n_simples - sum(quotas.values())]:
        quotas[classe] += 1
    for classe in sorted(quotas):
        for k in range(quotas[classe]):
            voulu = (k + classe) % 3
            t.prendre(simples,
                      lambda c: c.classe == classe and tercile(c) == voulu,
                      lambda c: c.classe == classe,
                      lambda c: tercile(c) == voulu,
                      lambda c: True)
    return sorted(t.pris, key=lambda c: _hash(graine, c.ref))


# --------------------------------------------------------------------------- annonce et vérité

def annonce(c: Candidat, ref_annonce: str) -> dict:
    """L'annonce au format du POST /api/v1/appels_ouverts, sans date limite ni date de publication (posées à l'envoi)."""
    a = {"ref_annonce": ref_annonce, "reference": ref_annonce, "objet": c.objet, "acheteur": c.acheteur,
         "estimation": round(c.montant, 2), "caution_provisoire": c.caution, "source": "rejeu",
         "qualifications": c.qualifications, "clients": [CLIENT]}
    if c.nb_lots:
        a["lots"] = [{"lot": l["lot"], "intitule": l["intitule"], "estimation": l["estimation"],
                      "caution_provisoire": l["caution_provisoire"]} for l in c.lots]
    return a


def verite(c: Candidat, ref_annonce: str, cree_le: str) -> list[tuple]:
    """Une ligne par marché ou lot : ce qui s'est réellement passé à l'ouverture des plis."""
    lignes = []
    for u in c.unites:
        a = rejouer_attribution(u)
        concurrents = [{"nom": k["nom"], "cle": k["cle"], "statut": k["statut"], "montant": offre_de(k),
                        "taux": k["taux"], "rang": a.rangs.get(i)} for i, k in enumerate(u.concurrents)]
        concurrents.sort(key=lambda x: (x["rang"] is None, x["rang"] or 0, x["taux"] or 9e9))
        gagnant = next((k for k in concurrents if k["cle"] == u.cle_gagnant), None)
        lignes.append((ref_annonce, u.lot or "", c.ref, c.reference, c.date, c.acheteur, u.estimation,
                       u.prix_reference_stocke, a.reference, a.moyenne, a.nb_offres, u.cle_gagnant,
                       gagnant["nom"] if gagnant else None, gagnant["taux"] if gagnant else None,
                       a.cle_gagnant, json.dumps(concurrents, ensure_ascii=False), cree_le))
    return lignes


def lire_verite(calc: sqlite3.Connection, ref_annonce: str) -> list[dict]:
    """Ce qui s'est réellement passé, une entrée par marché ou lot. RÉSERVÉ à la page d'analyse (bloc
    « Ce qui s'est réellement passé ») : le moteur ne l'appelle jamais. [] pour un AO qui n'est pas un rejeu."""
    if not rejeu.est_un_rejeu(ref_annonce):
        return []
    try:
        lignes = calc.execute("SELECT * FROM rejeu_verite WHERE ref_annonce = ? ORDER BY CAST(lot AS INTEGER), lot",
                              (ref_annonce,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{**dict(r), "concurrents": json.loads(r["concurrents"])} for r in lignes]


# --------------------------------------------------------------------------- base de calcul

def preparer(calc: sqlite3.Connection) -> None:
    calc.executescript(rejeu.SCHEMA_AO + SCHEMA_VERITE)
    calc.commit()


def enregistrer(calc: sqlite3.Connection, choisis: list[Candidat], renouveler: bool = False) -> list[str]:
    """Range l'annonce et la date de coupure (rejeu_ao) et la vérité (rejeu_verite). Renvoie les ref_annonce."""
    preparer(calc)
    maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
    refs = [f"{rejeu.PREFIXE}{i:02d}" for i in range(1, len(choisis) + 1)]
    with calc:
        if renouveler:
            calc.execute("DELETE FROM rejeu_ao")
            calc.execute("DELETE FROM rejeu_verite")
        for c, ref in zip(choisis, refs):
            calc.execute("INSERT OR REPLACE INTO rejeu_ao (ref_annonce, date_coupure, annonce, cree_le)"
                         " VALUES (?,?,?,?)", (ref, rejeu.veille(c.date), json.dumps(annonce(c, ref), ensure_ascii=False),
                                                maintenant))
            calc.execute("DELETE FROM rejeu_verite WHERE ref_annonce = ?", (ref,))
            calc.executemany("INSERT INTO rejeu_verite VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             verite(c, ref, maintenant))
    return refs


# --------------------------------------------------------------------------- envoi

class ErreurEnvoi(RuntimeError):
    pass


def est_local(url: str) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return h in LOCAL or h.endswith(".localhost")


def poster(url: str, jeton: str, corps: dict, delai: int = 60) -> tuple[int, dict]:
    """POST JSON avec le jeton Bearer. Sans proxy : un proxy d'entreprise n'a rien à faire avec 127.0.0.1."""
    requete = urllib.request.Request(
        url.rstrip("/") + "/api/v1/appels_ouverts", method="POST",
        data=json.dumps(corps, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {jeton}", "Content-Type": "application/json"})
    ouvreur = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with ouvreur.open(requete, timeout=delai) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        raise ErreurEnvoi(f"connexion impossible à {url} : {getattr(e, 'reason', e)}") from None


def annonces_a_envoyer(calc: sqlite3.Connection, maintenant: datetime | None = None) -> list[dict]:
    """Les annonces rangées, avec une date de publication d'hier et une date limite dans 20 à 49 jours."""
    maintenant = maintenant or datetime.now(timezone.utc)
    sortie = []
    for i, r in enumerate(calc.execute("SELECT annonce FROM rejeu_ao ORDER BY ref_annonce")):
        a = json.loads(r["annonce"])
        a["publie_le"] = (maintenant - timedelta(days=1)).strftime("%Y-%m-%d")
        a["date_limite"] = (maintenant + timedelta(days=20 + (i * 7) % 30)).strftime("%Y-%m-%dT10:00:00")
        sortie.append(a)
    return sortie


def envoyer(calc: sqlite3.Connection, url: str, jeton: str, distant: bool = False, envoi=poster) -> dict:
    if not jeton:
        raise ErreurEnvoi("JETON_SERVICE n'est pas défini dans l'environnement.")
    if not est_local(url) and not distant:
        raise ErreurEnvoi(f"{url} n'est pas une adresse locale : ces AO de test ne doivent pas entrer dans la base "
                          "d'un serveur réel. Ajoutez --distant si c'est voulu.")
    annonces = annonces_a_envoyer(calc)
    if not annonces:
        raise ErreurEnvoi("Aucun AO rejoué à envoyer : lancez d'abord « choisir ».")
    statut, reponse = envoi(url, jeton, {"clients": [CLIENT], "appels_ouverts": annonces})
    if statut == 401:
        raise ErreurEnvoi("Jeton refusé (401) : JETON_SERVICE ne correspond pas à celui du serveur.")
    if statut == 503:
        raise ErreurEnvoi("Le serveur n'a pas de JETON_SERVICE : le service de détection y est désactivé.")
    if statut != 200:
        raise ErreurEnvoi(f"Réponse {statut} : {json.dumps(reponse, ensure_ascii=False)[:400]}")
    refus = {x.get("ref_annonce") for x in reponse.get("rejetes", [])}
    maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with calc:
        for a in annonces:
            if a["ref_annonce"] not in refus:
                calc.execute("UPDATE rejeu_ao SET envoye_le = ? WHERE ref_annonce = ?", (maintenant, a["ref_annonce"]))
    return reponse


# --------------------------------------------------------------------------- ligne de commande

def _lignes_etat(calc: sqlite3.Connection) -> list[dict]:
    try:
        return [dict(r) | {"annonce": json.loads(r["annonce"])} for r in
                calc.execute("SELECT * FROM rejeu_ao ORDER BY ref_annonce")]
    except sqlite3.OperationalError:
        return []


def commande_choisir(args) -> int:
    calc = bases.calcul()
    try:
        preparer(calc)
        deja = _lignes_etat(calc)
        if deja and not args.renouveler:
            print(f"{len(deja)} AO rejoués déjà choisis : conservés (--renouveler pour recommencer).")
            return 0
        hist = bases.historique()
        try:
            cands = candidats(hist)
            print(f"{len(cands)} marchés rejouables en {ANNEE} "
                  f"({sum(1 for c in cands if c.nb_lots)} allotis).")
            choisis = choisir(cands, args.n, min(args.allotis, args.n), args.graine)
            refs = enregistrer(calc, choisis, renouveler=True)
        finally:
            hist.close()
    finally:
        calc.close()
    print(f"{len(refs)} AO rejoués rangés dans copilote_calcul.db (rejeu_ao, rejeu_verite) :")
    afficher(choisis, refs)
    return 0


def afficher(choisis: list[Candidat], refs: list[str]) -> None:
    print(f"{'AO':<9} {'coupure':<11} {'classe':>6} {'lots':>4} {'estimation':>12}  acheteur")
    for c, ref in zip(choisis, refs):
        print(f"{ref:<9} {rejeu.veille(c.date):<11} {c.classe or '—':>6} {c.nb_lots or '—':>4} "
              f"{c.montant / 1e6:>10.2f} M  {c.acheteur[:50]}")


def commande_envoyer(args) -> int:
    calc = bases.calcul()
    try:
        r = envoyer(calc, args.url, os.environ.get("JETON_SERVICE", ""), args.distant)
    finally:
        calc.close()
    print(f"envoyés à {args.url} : {r['recus']} reçus, {r['crees']} créés, {r['maj']} mis à jour, "
          f"{len(r['rejetes'])} rejetés.")
    for x in r["rejetes"]:
        print(f"  rejeté : {x['ref_annonce']} — {x['motif']}")
    return 1 if r["rejetes"] else 0


def commande_groupes(args) -> int:
    from .recalculer import recalculer_groupes
    calc = bases.calcul()
    try:
        dates = sorted({r["date_coupure"] for r in _lignes_etat(calc)})
        # Un calcul d'une autre version des tables ne compte pas : il sera refait (une fois par date de coupure).
        faits = {r[0] for r in calc.execute("SELECT calcul FROM groupes_calculs")} \
            if groupes.a_jour(calc) else set()
    finally:
        calc.close()
    for d in dates:
        if f"jusqua-{d}" in faits and not args.force:
            print(f"groupes « jusqua-{d} » déjà calculés.")
        else:
            recalculer_groupes(d)
    return 0


def commande_etat(args) -> int:
    calc = bases.calcul()
    try:
        lignes = _lignes_etat(calc)
    finally:
        calc.close()
    if not lignes:
        print("Aucun AO rejoué choisi.")
        return 0
    for r in lignes:
        a = r["annonce"]
        print(f"{r['ref_annonce']:<9} coupure {r['date_coupure']}  "
              f"{'envoyé ' + r['envoye_le'] if r['envoye_le'] else 'pas encore envoyé'}  "
              f"{len(a.get('lots', [])) or '—'} lots  {a['acheteur'][:40]}")
    return 0


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="python -m copilote.rejouer_ao", description=__doc__.split("\n")[0])
    sous = p.add_subparsers(dest="quoi")
    # Les mêmes options avant ou après le nom de la commande ; SUPPRESS évite qu'une commande
    # n'écrase une option donnée avant elle par sa valeur par défaut.
    for parseur, vide in [(p, False)] + [(sous.add_parser(nom), True)
                                         for nom in ("choisir", "envoyer", "groupes", "etat")]:
        def defaut(d, vide=vide):
            return argparse.SUPPRESS if vide else d
        parseur.add_argument("--url", default=defaut(URL_DEFAUT), help=f"API du site (défaut {URL_DEFAUT})")
        parseur.add_argument("--distant", action="store_true", default=defaut(False),
                             help="accepter une adresse qui n'est pas locale")
        parseur.add_argument("--n", type=int, default=defaut(NOMBRE))
        parseur.add_argument("--allotis", type=int, default=defaut(ALLOTIS))
        parseur.add_argument("--graine", type=int, default=defaut(0))
        parseur.add_argument("--renouveler", action="store_true", default=defaut(False),
                             help="refaire le choix (les anciens REJEU- restent en base du serveur)")
        parseur.add_argument("--force", action="store_true", default=defaut(False),
                             help="groupes : recalculer même ceux qui existent")
    a = p.parse_args(argv)
    if a.quoi == "groupes":
        return commande_groupes(a)
    if a.quoi == "etat":
        return commande_etat(a)
    try:
        if a.quoi in (None, "choisir"):
            commande_choisir(a)
        if a.quoi in (None, "envoyer"):
            return commande_envoyer(a)
    except ErreurEnvoi as e:
        print(f"Envoi impossible : {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
