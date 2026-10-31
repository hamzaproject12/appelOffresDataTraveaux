"""Le calcul « autour d'une société » : qui est présent quand elle gagne, qui gagne quand elle perd.

Deux moitiés, qui doivent donner les mêmes chiffres :

    donnees()    les données brutes d'une société X (une ligne par unité où elle a déposé) ;
    calculer()   A, B, C, A ÷ B et le classement de chaque autre société Y.

Le navigateur refait calculer() en JavaScript (statique/groupes_calcul.js) à chaque changement de
réglage ; ce fichier-ci sert à l'étape 5 (concurrents probables) et au test qui compare les deux.
Tests : tests/test_autour_societe.py, sur le même format de données.

Unité = un marché ordinaire ou UN LOT. Présence = toute offre déposée, écartée comprise. Fusions
appliquées. Pour une société Y :

    A = victoires de X avec Y présente / victoires de X
    B = unités communes / unités de X
    C = unités communes / unités de Y (dans toute la base)
    A ÷ B : combien de fois Y est plus présente quand X gagne que d'habitude ;
    victoires de Y : unités communes que Y a gagnées (X perd alors) ;
    X sur les victoires de Y : victoires de Y où X était présente ÷ toutes les victoires de Y ;
    position : médiane de (taux de Y / taux de X − 1) sur les victoires de X.

Lecture de chaque Y, dans cet ordre (seuils dans REGLAGES, modifiables dans la page) :

    noyau            Y présente sur ≥ 3 victoires de X, A ÷ B ≥ 1,3, ET X présente sur au moins la
                     moitié des victoires de Y : elles gagnent à tour de rôle ;
    accompagnatrice  Y présente sur ≥ 3 victoires de X, et (A ÷ B ≥ 1,3 ou C ≥ 60 %), sans être noyau ;
    concurrent       Y gagne au moins 3 fois quand X est présente, ET A ÷ B < 1 ;
    neutre           le reste.

Le groupe est X plus les noyaux et accompagnatrices, classés par victoires accompagnées puis A ÷ B,
au plus `taille_groupe` sociétés en tout (X comprise) ; les suivantes sont « autres sociétés proches ».

Vocabulaire : « présence commune », « lecture ». Une lecture est un fait statistique, jamais une
accusation ; seul un avis humain dit si deux sociétés sont liées.
"""
from __future__ import annotations

import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict

from . import api_groupes, bases
from . import fusions as _fusions
from . import liens as _liens
from .donnees import charger_unites
from .fiche_societe import _alias, _refs_de

CALCUL = api_groupes.CALCUL
# Version du format de `donnees()` : elle entre dans l'ETag de la route, pour qu'un navigateur qui a gardé
# une réponse d'avant un changement de format ne la reçoive pas en 304 avec le nouveau script.
FORMAT = 2

# Les seuils par défaut. La page les montre dans « Réglages du calcul » et les envoie au calcul
# JavaScript ; ils voyagent dans la réponse, pour qu'il n'y ait qu'un seul endroit où les écrire.
REGLAGES = {
    "min_victoires": 3,        # noyau, accompagnatrice : victoires de X avec Y présente, au moins
    "rapport_min": 1.3,        # noyau, accompagnatrice : A ÷ B au moins
    "part_noyau": 0.5,         # noyau : part des victoires de Y où X est présente, au moins
    "c_min": 0.6,              # accompagnatrice : C au moins (autre voie que A ÷ B)
    "min_victoires_y": 3,      # concurrent direct : victoires de Y quand X est présente, au moins
    "rapport_concurrent": 1.0, # concurrent direct : A ÷ B strictement en dessous
    "taille_groupe": 9,        # nombre de sociétés du groupe, X comprise
}

LECTURES = {
    "noyau": "noyau (gagnent à tour de rôle)",
    "accompagnatrice": "accompagnatrice",
    "concurrent": "concurrent direct",
    "neutre": "neutre",
}
MAX_OBJET = 140
STATUTS = {"attributaire": "a", "admis": "m", "ecarte_dossier_administratif_technique": "e", "depose": "d"}


# --------------------------------------------------------------------------- le calcul

def _mediane(v: list[float]) -> float | None:
    return statistics.median(v) if v else None


def calculer(d: dict, reglages: dict | None = None) -> dict:
    """Le calcul complet sur le format de `donnees()`. Renvoie
    {"unites_x", "victoires_x", "lignes", "groupe", "noyau", "accompagnatrices", "autres"}.

    Chaque ligne : cle, communes, victoires_x_avec, victoires_y, victoires_y_total, part_x_sur_y, A, B, C,
    rapport, position, lecture. Les lignes sont triées par A (décroissant), puis par nombre d'unités
    communes, puis par clé. « groupe » commence par X ; « noyau » aussi.
    """
    r = {**REGLAGES, **(reglages or {})}
    x = d["societe"]["cle"]
    unites = d["unites"]
    total_y = {c: s["total"] for c, s in d["societes"].items()}
    gagnees_y = {c: s.get("victoires", 0) for c, s in d["societes"].items()}
    n_x = len(unites)
    victoires_x = sum(1 for u in unites if u["gagnant"] == x)

    communes: Counter = Counter()
    avec: Counter = Counter()                  # victoires de X avec Y présente
    gagne_y: Counter = Counter()               # unités communes gagnées par Y (X présente, X perd)
    ecarts: dict[str, list[float]] = defaultdict(list)
    for u in unites:
        taux_x = next((o[2] for o in u["offres"] if o[0] == x), None)
        gagne = u["gagnant"] == x
        vus = set()
        for cle, _statut, taux in u["offres"]:
            if cle == x or cle in vus:
                continue
            vus.add(cle)
            communes[cle] += 1
            if gagne:
                avec[cle] += 1
                if taux and taux_x:
                    ecarts[cle].append(taux / taux_x - 1)
            elif u["gagnant"] == cle:
                gagne_y[cle] += 1

    lignes = []
    for cle, c in communes.items():
        a = avec[cle] / victoires_x if victoires_x else None
        b = c / n_x if n_x else None
        rapport = a / b if a is not None and b else None
        ty, vt = total_y.get(cle), gagnees_y.get(cle, 0)
        cc = c / ty if ty else None
        part = min(1.0, gagne_y[cle] / vt) if vt else None      # X présente sur ses victoires
        ligne = {"cle": cle, "communes": c, "victoires_x_avec": avec[cle], "victoires_y": gagne_y[cle],
                 "victoires_y_total": vt, "part_x_sur_y": part,
                 "A": a, "B": b, "C": cc, "rapport": rapport, "position": _mediane(ecarts[cle])}
        presente = avec[cle] >= r["min_victoires"]
        fort = rapport is not None and rapport >= r["rapport_min"]
        if presente and fort and part is not None and part >= r["part_noyau"]:
            ligne["lecture"] = "noyau"
        elif presente and (fort or (cc is not None and cc >= r["c_min"])):
            ligne["lecture"] = "accompagnatrice"
        elif gagne_y[cle] >= r["min_victoires_y"] and rapport is not None and rapport < r["rapport_concurrent"]:
            ligne["lecture"] = "concurrent"
        else:
            ligne["lecture"] = "neutre"
        lignes.append(ligne)
    lignes.sort(key=lambda l: (-(l["A"] or 0), -l["communes"], l["cle"]))

    proches = sorted((l for l in lignes if l["lecture"] in ("noyau", "accompagnatrice")),
                     key=lambda l: (-l["victoires_x_avec"], -l["rapport"], l["cle"]))
    place = max(0, int(r["taille_groupe"]) - 1)                 # X compte dans la taille
    dedans, autres = proches[:place], proches[place:]
    return {"unites_x": n_x, "victoires_x": victoires_x, "lignes": lignes,
            "groupe": [x] + [l["cle"] for l in dedans],
            "noyau": [x] + [l["cle"] for l in dedans if l["lecture"] == "noyau"],
            "accompagnatrices": [l["cle"] for l in dedans if l["lecture"] == "accompagnatrice"],
            "autres": [l["cle"] for l in autres]}


# --------------------------------------------------------------------------- les données brutes

_totaux_memo: dict = {}
_verrou = threading.Lock()


def unites_par_societe(hist: sqlite3.Connection, fusions: dict[str, str]) -> Counter:
    """Nombre d'unités (marchés ordinaires et lots) où chaque société a déposé une offre."""
    return compteurs_par_societe(hist, fusions)[0]


def compteurs_par_societe(hist: sqlite3.Connection, fusions: dict[str, str]) -> tuple[Counter, Counter]:
    """(unités où chaque société a déposé, unités qu'elle a gagnées), sur toute la base.

    Un parcours complet de l'historique (une seconde) : gardé en mémoire tant que le fichier ne
    change pas, comme victoires_par_societe. Les deux comptes viennent des mêmes unités.
    """
    try:
        chemin = bases.chemin_historique()
        st = chemin.stat()
        cle = (str(chemin), st.st_mtime_ns, st.st_size, tuple(sorted(fusions.items())))
    except OSError:
        cle = (time.monotonic(),)
    with _verrou:
        if _totaux_memo.get("cle") != cle:
            total: Counter = Counter()
            gagnees: Counter = Counter()
            for u in charger_unites(hist, fusions=fusions):
                for c in {o["cle"] for o in u.concurrents if o["cle"]}:
                    total[c] += 1
                if u.cle_gagnant:
                    gagnees[u.cle_gagnant] += 1
            _totaux_memo.clear()
            _totaux_memo.update(cle=cle, valeur=(total, gagnees))
        return _totaux_memo["valeur"]


def donnees(hist: sqlite3.Connection, calc: sqlite3.Connection, ret: sqlite3.Connection, cle: str,
            fusions: dict[str, str] | None = None) -> dict | None:
    """Les données brutes de la société `cle`, ou None si elle est inconnue du calcul des groupes."""
    if not api_groupes._prets(calc):
        return None
    if fusions is None:
        fusions = _fusions.charger()
    connue = calc.execute("SELECT nom FROM groupes_societes WHERE calcul = ? AND cle = ?", (CALCUL, cle)).fetchone()
    if connue is None:
        return None

    refs = _refs_de(hist, _alias(cle, fusions))
    infos = {r["ref"]: r for r in hist.execute(
        "SELECT ref, reference, objet FROM marches WHERE ref IN (%s)" % api_groupes._marques(len(refs)), refs)} \
        if len(refs) < 900 else _infos_par_paquets(hist, refs)
    unites, vus_noms, presentes = [], {}, set()
    for u in charger_unites(hist, fusions=fusions, refs=refs):
        if not any(o["cle"] == cle for o in u.concurrents):
            continue                                       # un autre lot du même marché
        offres, deja = [], set()
        for o in u.concurrents:
            if not o["cle"] or o["cle"] in deja:
                continue                                   # une clé écrite deux fois ne compte qu'une
            deja.add(o["cle"])
            vus_noms.setdefault(o["cle"], o["nom"])
            offres.append([o["cle"], STATUTS.get(o["statut"], "?"),
                           None if o["taux"] is None else round(o["taux"], 6)])
        presentes |= deja
        i = infos.get(u.ref)
        unites.append({
            "id": u.identifiant, "ref": u.ref, "lot": u.lot, "date": api_groupes.date_iso(u.date),
            "acheteur": u.acheteur or "", "reference": (i["reference"] if i else "") or "",
            "objet": ((i["objet"] if i else "") or "")[:MAX_OBJET],
            "estimation": u.estimation, "gagnant": u.cle_gagnant or None, "offres": offres})
    unites.sort(key=lambda u: (u["date"], u["id"]), reverse=True)

    noms = {**vus_noms, **api_groupes._noms(calc, presentes | {cle})}
    totaux, gagnees = compteurs_par_societe(hist, fusions)
    # « victoires » : toutes celles de la société dans la base ; la présence de X sur elles se lit dans les
    # unités de X (celles que Y a gagnées et où X a déposé), il n'y a donc rien d'autre à envoyer.
    societes = {c: {"nom": noms.get(c, c), "total": totaux.get(c, 0), "victoires": gagnees.get(c, 0)}
                for c in presentes | {cle}}

    # Les liens du calcul hebdomadaire et les avis, par paire (clé de l'autre société).
    liens: dict[str, dict] = {}
    for col, autre in (("cle_a", "cle_b"), ("cle_b", "cle_a")):
        for r in calc.execute(f"SELECT {autre} autre, niveau, exces, p, communs FROM groupes_liens"
                              f" WHERE calcul = ? AND {col} = ?", (CALCUL, cle)):
            if r["autre"] in presentes:
                liens[r["autre"]] = {"niveau": r["niveau"], "exces": r["exces"], "p": r["p"]}
    avis: dict[str, dict] = {}
    for x in api_groupes._lignes_avis(ret, cle=cle):         # du plus récent au plus ancien
        autre = x["cle_b"] if x["cle_a"] == cle else x["cle_a"]
        a = avis.setdefault(autre, {"reponse": None, "auteur": None, "date": None, "nb": 0})
        a["nb"] += 1
        if a["reponse"] is None and x["reponse"] in ("oui", "non"):
            a.update(reponse=x["reponse"], auteur=x["auteur"], date=x["cree_le"])
    # Une société sur laquelle un avis a été donné sans qu'elles aient de marché commun : la page doit
    # pouvoir la nommer. Le calcul ne la regarde pas (il ne lit que les offres des unités de la société).
    hors = set(avis) - set(societes)
    noms.update(api_groupes._noms(calc, hors))
    societes.update({c: {"nom": noms.get(c, c), "total": totaux.get(c, 0), "victoires": gagnees.get(c, 0)}
                     for c in hors})
    return {"societe": {"cle": cle, "nom": noms.get(cle, connue["nom"])}, "unites": unites,
            "societes": societes, "liens": liens, "avis": avis,
            "reglages": dict(REGLAGES), "lectures": dict(LECTURES),
            "question": api_groupes.QUESTION,
            "calcul": api_groupes._meta(calc)["cree_le"]}


def _infos(hist: sqlite3.Connection, refs: list[str]) -> dict:
    """{ref: (reference, objet)} ; par paquets de 400, SQLite limite le nombre de paramètres."""
    sortie = {}
    for k in range(0, len(refs), 400):
        morceau = refs[k:k + 400]
        for r in hist.execute("SELECT ref, reference, objet FROM marches WHERE ref IN (%s)"
                              % api_groupes._marques(len(morceau)), morceau):
            sortie[r["ref"]] = r
    return sortie
