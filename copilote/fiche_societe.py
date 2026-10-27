"""La fiche d'une société : qui est présent quand elle gagne, qui elle accompagne, qui la bat.

La question de départ : « quand cette société gagne, quelles sociétés sont présentes pour
l'accompagner ? » On y répond par quatre sections, dans cet ordre :

    A  gagne_avec    les sociétés présentes lors de ses victoires ;
    B  accompagne    les sociétés qui gagnent alors qu'elle est présente et perd ;
    C  concurrents   celles qui gagnent le plus souvent quand elle perd ;
    D  a_verifier    les présences communes restantes, sans résultat commun à montrer.

Une même société peut tenir plusieurs rôles : ce sont trois questions différentes posées aux mêmes
marchés, pas trois classements du même palmarès.

Tout est compté à la demande, sur les seuls marchés de la société (quelques centaines au plus) :

    marchés où elle a déposé  →  charger_unites (pv_travaux.db, lecture seule)  →  unités

Une unité est un marché ordinaire ou UN LOT : une offre de lot ne concourt que dans son lot.
Présence = toute offre déposée, écartée comprise. Les fusions de clés sont appliquées.

Chaque ligne d'une section porte les chiffres d'une autre société ; ses marchés en preuve sont
rangés à part, dans « preuves » (une société peut paraître dans plusieurs sections).

Ce qui vient du calcul hebdomadaire (copilote_calcul.db) et n'est jamais recalculé ici : l'excès
par rapport au hasard, la valeur p, le niveau du lien (groupes_liens) et le nombre de marchés de
chaque autre société (groupes_societes). Tout le reste est compté sur l'historique du jour. Rien
dans ce fichier ne touche à la détection des groupes.

Vocabulaire : « présence commune », « lien probable », « signal ». Un lien est un fait statistique,
jamais une accusation ; seul un avis humain dit si deux sociétés sont liées.
"""
from __future__ import annotations

import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import api_groupes, bases
from . import fusions as _fusions
from . import liens as _liens
from .donnees import Unite, charger_unites

CALCUL = api_groupes.CALCUL
MIN_VICTOIRES = 2               # il faut 2 victoires accompagnées pour entrer dans A ou B
MAX_LIGNES = 25                 # lignes par section
MAX_PREUVES = 8                 # marchés en preuve par société (la réponse entière reste légère)

# Les libellés des sections sont écrits ici, et non dans la page, pour que le contrôle de
# vocabulaire neutre des tests les voie passer par l'API.
SECTIONS = {
    "gagne_avec": (
        "Présentes quand elle gagne",
        f"Les sociétés qui avaient déposé une offre sur les marchés qu’elle a gagnés, à partir de "
        f"{MIN_VICTOIRES} victoires accompagnées. « Gagne avec elle » compte les marchés et lots "
        f"partagés que l’autre société a gagnés."),
    "accompagne": (
        "Sociétés qu’elle accompagne",
        f"Le même calcul à l’envers : l’autre société gagne, celle-ci est présente et perd, au "
        f"moins {MIN_VICTOIRES} fois."),
    "concurrents": (
        "Concurrents directs",
        "Les sociétés qui gagnent le plus souvent quand elle perd. Une même société peut aussi "
        "figurer dans les sections précédentes : c’est une autre question posée aux mêmes marchés."),
    "a_verifier": (
        "Autres présences à vérifier",
        "Des présences communes plus fréquentes que le hasard ne le voudrait, mais sans assez de "
        "victoires communes pour en dire autre chose. À vérifier, rien de plus."),
}


# --------------------------------------------------------------------------- comptage

@dataclass
class _Compte:
    """Ce qu'on a vu d'une autre société sur les marchés de la société dont on fait la fiche."""
    refs: set[str] = field(default_factory=set)              # marchés communs
    unites: int = 0                                          # marchés et lots communs
    victoires_x: int = 0                                     # victoires de la société, l'autre présente
    victoires_y: int = 0                                     # unités communes gagnées par l'autre
    ecarts_x: list[float] = field(default_factory=list)      # offre de l'autre / offre gagnante − 1
    ecarts_y: list[float] = field(default_factory=list)      # offre de la société / offre gagnante − 1
    vues: list[tuple] = field(default_factory=list)          # (unité, son taux, le taux de l'autre, statut)


def _alias(cle: str, fusions: dict[str, str]) -> list[str]:
    """La clé et toutes celles que les fusions ramènent à elle (l'historique garde les anciennes)."""
    return sorted({b for b, garde in fusions.items() if garde == cle} | {cle})


def _refs_de(hist: sqlite3.Connection, cles: list[str]) -> list[str]:
    """Les marchés où l'une de ces clés a déposé une offre (index i_c_cle)."""
    return [r[0] for r in hist.execute(
        "SELECT DISTINCT c.ref FROM concurrents c JOIN marches m ON m.ref = c.ref"
        f" WHERE m.principale = 1 AND c.cle IN ({api_groupes._marques(len(cles))})", cles)]


def _par_cles(db: sqlite3.Connection, sql: str, cles, args: tuple = ()) -> dict:
    """{1re colonne: 2e colonne} pour ces clés. `sql` contient « {} », remplacé par les « ? » du IN.

    Les clés sont lues par paquets de 400 : au-delà, SQLite refuse le nombre de paramètres.
    """
    liste = sorted(set(cles))
    sortie = {}
    for k in range(0, len(liste), 400):
        morceau = liste[k:k + 400]
        sortie.update({r[0]: r[1] for r in db.execute(
            sql.format(api_groupes._marques(len(morceau))), (*args, *morceau))})
    return sortie


def _mediane(valeurs: list[float]) -> float | None:
    return statistics.median(valeurs) if valeurs else None


def _compter(unites: list[Unite], cle: str) -> tuple[dict, dict[str, _Compte], dict[str, str]]:
    """Les unités où la société a déposé, et ce qu'elles disent de chaque autre société.

    Renvoie (ses chiffres, {clé de l'autre société: _Compte}, {clé: nom lu dans les offres}).
    """
    comptes: dict[str, _Compte] = defaultdict(_Compte)
    noms_vus: dict[str, str] = {}
    marches: set[str] = set()
    chiffres = {"unites": 0, "victoires": 0, "defaites": 0, "non_attribuees": 0}
    for u in unites:
        mien = next((c for c in u.concurrents if c["cle"] == cle), None)
        if mien is None:
            continue                                    # un autre lot du même marché : pas la sienne
        gagne = u.cle_gagnant == cle
        chiffres["unites"] += 1
        chiffres["victoires" if gagne else "defaites" if u.cle_gagnant else "non_attribuees"] += 1
        marches.add(u.ref)
        vus = set()
        for c in u.concurrents:
            autre = c["cle"]
            if not autre or autre == cle or autre in vus:
                continue                                # une clé écrite deux fois ne compte qu'une
            vus.add(autre)
            noms_vus.setdefault(autre, c["nom"])
            k = comptes[autre]
            k.refs.add(u.ref)
            k.unites += 1
            k.vues.append((u, mien["taux"], c["taux"], c["statut"]))
            if gagne:
                k.victoires_x += 1
                if c["taux"] and mien["taux"]:
                    k.ecarts_x.append(c["taux"] / mien["taux"] - 1)
            elif u.cle_gagnant == autre:
                k.victoires_y += 1
                if c["taux"] and mien["taux"]:
                    k.ecarts_y.append(mien["taux"] / c["taux"] - 1)
    chiffres["marches"] = len(marches)
    return chiffres, comptes, noms_vus


# --------------------------------------------------------------------------- victoires de chacun

_victoires_memo: dict = {}
_verrou = threading.Lock()


def victoires_par_societe(hist: sqlite3.Connection, fusions: dict[str, str]) -> Counter:
    """Marchés et lots gagnés par chaque société de l'historique, fusions appliquées.

    Deux parcours complets de l'historique : comme il est remplacé à chaque déploiement, le
    résultat est gardé en mémoire tant que le fichier ne change pas (même règle que l'index de
    similarité). Sans cela, chaque ouverture de fiche relirait 18 000 marchés.
    """
    try:
        chemin = bases.chemin_historique()
        st = chemin.stat()
        cle = (str(chemin), st.st_mtime_ns, st.st_size, tuple(sorted(fusions.items())))
    except OSError:
        cle = (time.monotonic(),)                       # chemin illisible : on ne garde rien
    with _verrou:
        if _victoires_memo.get("cle") != cle:
            total: Counter = Counter()
            for sql in ("SELECT cle_attributaire k, COUNT(*) n FROM marches WHERE principale = 1"
                        " AND IFNULL(nb_lots, 0) = 0 AND cle_attributaire IS NOT NULL GROUP BY k",
                        "SELECT l.cle_attributaire k, COUNT(*) n FROM lots l JOIN marches m ON m.ref = l.ref"
                        " WHERE m.principale = 1 AND l.cle_attributaire IS NOT NULL GROUP BY k"):
                for r in hist.execute(sql):
                    total[_fusions.appliquer(r["k"], fusions)] += r["n"]
            _victoires_memo.clear()
            _victoires_memo.update(cle=cle, valeur=total)
        return _victoires_memo["valeur"]


# --------------------------------------------------------------------------- profils et phrases

def profil(victoires_x: int, victoires_y: int, victoires_totales_x: int) -> dict:
    """Une phrase factuelle sur la place de l'autre société, sans aucune accusation.

    victoires_x : victoires de la société où l'autre était présente ;
    victoires_y : marchés et lots communs que l'autre a gagnés ;
    victoires_totales_x : toutes les victoires de la société.
    """
    part = victoires_x / victoires_totales_x if victoires_totales_x else 0.0
    if victoires_x >= MIN_VICTOIRES and victoires_y * 2 <= victoires_x:
        return {"code": "accompagne",
                "phrase": ("présente lors de la plupart de ses victoires, gagne rarement avec elle"
                           if part >= 0.5 else
                           "présente lors d’une partie de ses victoires, gagne rarement avec elle")}
    if victoires_y >= MIN_VICTOIRES and victoires_x * 2 <= victoires_y:
        return {"code": "concurrent", "phrase": "concurrent direct"}
    if victoires_x >= MIN_VICTOIRES and victoires_y >= MIN_VICTOIRES:
        return {"code": "tour_de_role", "phrase": "gagnent à tour de rôle"}
    if victoires_y > victoires_x:
        return {"code": "concurrent", "phrase": "concurrent direct"}
    if victoires_x or victoires_y:
        return {"code": "partage", "phrase": "quelques marchés communs, résultats partagés"}
    return {"code": "sans_resultat", "phrase": "présente, sans qu’aucune des deux ne gagne ces marchés"}


def _phrase_resume(nom: str, c: dict, tete: dict | None) -> str:
    """« X a déposé sur 95 marchés… et en a gagné 13. Y était présente lors de 8 de ces 13 victoires… »"""
    unites = "" if c["unites"] == c["marches"] else f", soit {c['unites']} marchés et lots,"
    phrase = (f"{nom} a déposé une offre sur {c['marches']} marché"
              f"{'s' if c['marches'] > 1 else ''}{unites} et en a gagné {c['victoires']}.")
    if not tete or not c["victoires"]:
        return phrase
    gains = ("n’a gagné aucun des" if not tete["victoires_partenaire"]
             else "a gagné 1 des" if tete["victoires_partenaire"] == 1
             else f"a gagné {tete['victoires_partenaire']} des")
    return (f"{phrase} {tete['societe']['nom']} était présente lors de {tete['victoires_societe']} de ces "
            f"{c['victoires']} victoires, et {gains} {tete['unites_communes']} marchés et lots qu’elles partagent.")


# --------------------------------------------------------------------------- lignes et sections

def _preuves(k: _Compte, cle: str, autre: str, references: dict[str, str],
             noms: dict[str, str]) -> list[dict]:
    """Les marchés en preuve d'une société : du plus récent au plus ancien, les 8 premiers.

    Chaque ligne donne la référence, l'acheteur, la date, le gagnant et l'offre de chacune des deux
    sociétés en taux (montant / estimation).
    """
    vues = sorted(k.vues, key=lambda x: (x[0].date, x[0].ref, x[0].lot or ""), reverse=True)
    sortie = []
    for u, mien, sien, statut in vues[:MAX_PREUVES]:
        g = u.cle_gagnant
        sortie.append({
            "ref": u.ref, "reference": references.get(u.ref) or "", "lot": u.lot,
            "date": api_groupes.date_iso(u.date), "acheteur": u.acheteur or "",
            "gagnant": {"cle": g, "nom": noms.get(g, g)} if g else None,
            "taux_societe": mien, "taux_partenaire": sien, "statut_partenaire": statut or "",
            "gagne": None if not g else "societe" if g == cle else "partenaire" if g == autre else "autre"})
    return sortie


def _ligne(autre: str, k: _Compte, cle: str, chiffres: dict, lien: sqlite3.Row | None,
           noms: dict[str, str], marches_de: dict[str, int], victoires_de: Counter,
           avis: list[dict]) -> dict:
    """Une ligne de section : les mêmes faits pour les quatre sections, triés différemment."""
    a, b = (cle, autre) if cle < autre else (autre, cle)
    communs = len(k.refs)                               # recompté ici, comme les parts qui s'en servent
    marches_autre = marches_de.get(autre)
    tranche = next((x for x in avis if x["reponse"] in ("oui", "non")), None)
    return {
        "societe": {"cle": autre, "nom": noms.get(autre, autre), "marches": marches_autre,
                    "victoires": victoires_de.get(autre, 0)},
        "cle_a": a, "cle_b": b,
        "marches_communs": communs, "unites_communes": k.unites,
        "victoires_societe": k.victoires_x, "victoires_societe_total": chiffres["victoires"],
        "victoires_partenaire": k.victoires_y, "victoires_partenaire_total": victoires_de.get(autre, 0),
        # « présente sur x % des marchés de … », dans un sens puis dans l'autre.
        "part_marches_partenaire": (communs / marches_autre) if marches_autre else None,
        "part_marches_societe": (communs / chiffres["marches"]) if chiffres["marches"] else None,
        # Position médiane d'une offre par rapport à l'offre gagnante de l'autre, en part de celle-ci.
        "position_quand_elle_gagne": _mediane(k.ecarts_x),
        "position_quand_il_gagne": _mediane(k.ecarts_y),
        "exces": lien["exces"] if lien else None, "attendu": lien["attendu"] if lien else None,
        "p": lien["p"] if lien else None, "niveau": lien["niveau"] if lien else None,
        "mutuel": bool(lien["mutuel"]) if lien else False,
        "origine": lien["origine"] if lien else "",
        "profil": profil(k.victoires_x, k.victoires_y, chiffres["victoires"]),
        "avis": ({"reponse": tranche["reponse"], "auteur": tranche["auteur"], "date": tranche["cree_le"]}
                 if tranche else None), "nb_avis": len(avis),
        "preuves_total": len(k.vues)}


def _section(code: str, lignes: list[dict], ouverte: bool, avis: bool) -> dict:
    titre, intro = SECTIONS[code]
    return {"code": code, "titre": titre, "intro": intro, "ouverte": ouverte, "avis": avis,
            "lignes": lignes[:MAX_LIGNES], "total": len(lignes)}


def _trier(lignes: list[dict], *cles) -> list[dict]:
    """Tri décroissant sur ces nombres ; à égalité, par nom, pour un ordre stable d'un appel à l'autre."""
    return sorted(lignes, key=lambda x: (*(-(x[c] or 0) for c in cles), x["societe"]["nom"]))


# --------------------------------------------------------------------------- la fiche

def fiche(hist: sqlite3.Connection, calc: sqlite3.Connection, ret: sqlite3.Connection, cle: str,
          fusions: dict[str, str] | None = None) -> dict | None:
    """La fiche complète d'une société, ou None si la clé est inconnue (ou le calcul absent).

    `hist` est pv_travaux.db en lecture seule, `calc` copilote_calcul.db, `ret` copilote_retours.db.
    """
    if not api_groupes._prets(calc):
        return None
    if fusions is None:
        fusions = _fusions.charger()
    rg = _liens.reglages_groupes()
    s = calc.execute("SELECT * FROM groupes_societes WHERE calcul = ? AND cle = ?", (CALCUL, cle)).fetchone()
    if s is None:
        connue = next((x for x in api_groupes._charger_annuaire(calc) if x["cle"] == cle), None)
        if connue is None:
            return None
        s = {"cle": cle, "nom": connue["nom"], "marches": 0, "fort": 0, "moyen": 0, "faible": 0, "groupes": 0}
    else:
        s = dict(s)

    # ---- ses marchés, lus une seule fois
    refs = _refs_de(hist, _alias(cle, fusions))
    references = _par_cles(hist, "SELECT ref, reference FROM marches WHERE ref IN ({})", refs)
    chiffres, comptes, noms_vus = _compter(charger_unites(hist, fusions=fusions, refs=refs), cle)

    # ---- les liens du calcul hebdomadaire (une requête par colonne : l'index les sert)
    liens: dict[str, sqlite3.Row] = {}
    for col, autre in (("cle_a", "cle_b"), ("cle_b", "cle_a")):
        for r in calc.execute(f"SELECT * FROM groupes_liens WHERE calcul = ? AND {col} = ?", (CALCUL, cle)):
            liens[r[autre]] = r

    # ---- les avis, par paire
    historique = api_groupes._lignes_avis(ret, cle=cle)
    par_paire: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for x in historique:
        par_paire[(x["cle_a"], x["cle_b"])].append(x)

    # ---- les liens sans résultat commun : un niveau du calcul, ou un avis déjà donné
    restants = {autre for autre, r in liens.items()
                if r["niveau"] is not None or (r["origine"] or "").startswith("avis")}
    restants |= {b if a == cle else a for (a, b) in par_paire}
    restants.discard(cle)
    for autre in restants - set(comptes):               # un avis donné sans aucun marché commun
        comptes[autre] = _Compte()

    # ---- les sociétés dont on parle : celles qui ont un résultat commun, plus ces liens
    retenues = {autre for autre, k in comptes.items()
                if k.victoires_x >= MIN_VICTOIRES or k.victoires_y >= 1} | restants
    noms = {**noms_vus, **api_groupes._noms(calc, retenues | {cle})}
    noms.setdefault(cle, s["nom"])
    marches_de = _par_cles(calc, "SELECT cle, marches FROM groupes_societes WHERE calcul = ?"
                           " AND cle IN ({})", retenues, (CALCUL,))
    victoires_de = victoires_par_societe(hist, fusions)

    lignes = {autre: _ligne(autre, comptes[autre], cle, chiffres, liens.get(autre), noms,
                            marches_de, victoires_de,
                            par_paire.get(tuple(sorted((cle, autre))), []))
              for autre in retenues}

    a = _trier([x for x in lignes.values() if x["victoires_societe"] >= MIN_VICTOIRES],
               "victoires_societe", "part_marches_partenaire", "marches_communs")
    b = _trier([x for x in lignes.values() if x["victoires_partenaire"] >= MIN_VICTOIRES],
               "victoires_partenaire", "part_marches_societe", "marches_communs")
    c = _trier([x for x in lignes.values() if x["victoires_partenaire"] >= 1],
               "victoires_partenaire", "marches_communs")
    deja = {x["societe"]["cle"] for x in a + b + c}
    d = sorted((x for autre, x in lignes.items() if autre not in deja and autre in restants),
               key=lambda x: (-_liens.RANG.get(x["niveau"], 0), -(x["exces"] or 0), x["societe"]["nom"]))

    sections = [_section("gagne_avec", a, True, True), _section("accompagne", b, True, True),
                _section("concurrents", c, True, False), _section("a_verifier", d, False, False)]
    # Une société peut paraître dans plusieurs sections : ses marchés en preuve ne partent qu'une fois.
    preuves = {x["societe"]["cle"]: _preuves(comptes[x["societe"]["cle"]], cle, x["societe"]["cle"],
                                             references, noms)
               for sec in sections for x in sec["lignes"]}

    assez = chiffres["marches"] >= rg["actives"]
    groupes_soc = calc.execute(
        "SELECT g.id, g.score, g.niveau, g.taille, g.marches_3, fc.famille FROM groupe_membres m"
        " JOIN groupes g ON g.calcul = m.calcul AND g.id = m.groupe"
        " LEFT JOIN famille_cliques fc ON fc.calcul = m.calcul AND fc.groupe = m.groupe"
        " WHERE m.calcul = ? AND m.cle = ? ORDER BY g.score DESC, g.id LIMIT 50", (CALCUL, cle)).fetchall()
    return {
        # « marches » est recompté sur l'historique du jour, non repris du calcul de la semaine.
        "societe": {**{k: s[k] for k in ("cle", "nom", "fort", "moyen", "faible", "groupes")},
                    "marches": chiffres["marches"],
                    "unites": chiffres["unites"], "victoires": chiffres["victoires"],
                    "defaites": chiffres["defaites"], "non_attribuees": chiffres["non_attribuees"],
                    "victoires_totales": victoires_de.get(cle, 0)},
        "assez_de_marches": assez, "seuil_actives": rg["actives"],
        "message": None if assez else (f"Pas assez de marchés pour conclure : {chiffres['marches']} marché(s), il "
                                       f"en faut au moins {rg['actives']}. Ce qui suit est indicatif."),
        "resume": {"phrase": _phrase_resume(s["nom"], chiffres, a[0] if a else None)},
        "sections": sections, "preuves": preuves,
        "groupes": [{"id": g["id"], "score": g["score"], "niveau": g["niveau"], "taille": g["taille"],
                     "marches": g["marches_3"], "famille": g["famille"]} for g in groupes_soc],
        "question": api_groupes.QUESTION, "reglages": api_groupes.reglages_page(),
        "min_victoires": MIN_VICTOIRES,
        "historique": api_groupes._mettre_en_forme(
            api_groupes._regrouper(historique),
            api_groupes._noms(calc, {c for x in historique for c in (x["cle_a"], x["cle_b"])}))}
