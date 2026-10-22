"""Étape 3 — ce que la page /copilote/groupes lit et écrit.

Lecture : copilote_calcul.db (calcul « courant »), complétée pour la fiche d'un groupe par
pv_travaux.db en lecture seule (objet, taux de chaque membre). Écriture : uniquement les avis, dans
copilote_retours.db. Rien ici ne touche aux calculs : un avis agit au recalcul suivant (groupes.lire_avis).

Vocabulaire : « groupe de présence commune », « signal ». Un groupe est un fait statistique.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

from . import bases, groupes
from .donnees import charger_unites

CALCUL = "courant"
TRIS = ("score", "taille", "marches", "recent")
REPONSES = ("oui", "non", "nsp")
MAX_COMMENTAIRE = 1000
MAX_MARCHES_FICHE = 400

QUESTION = "Ces sociétés sont-elles liées (partenaires habituels, même dirigeant, groupement) ?"


class ErreurAvis(ValueError):
    """Un avis refusé : le message est destiné à l'utilisateur."""

    def __init__(self, message: str, statut: int = 422):
        super().__init__(message)
        self.statut = statut


# --------------------------------------------------------------------------- petits outils

def normaliser(texte: str | None) -> str:
    """Minuscules, sans accents ni ponctuation : « Société d'Étude » → « societe d etude »."""
    sans = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", sans.lower()).strip()


def date_iso(d: str | None) -> str:
    """AAAAMMJJ → AAAA-MM-JJ ; une date illisible ou absente devient ''."""
    d = d or ""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if re.fullmatch(r"\d{8}", d) else ""


def _prets(calc: sqlite3.Connection) -> bool:
    """Vrai si un calcul « courant » existe : sinon la page dit de lancer le recalcul."""
    tables = {r[0] for r in calc.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"groupes_calculs", "groupes", "groupe_membres", "groupe_preuves", "groupes_paires",
            "familles", "famille_cliques", "familles_anciennes"} <= tables:
        return False
    return calc.execute("SELECT 1 FROM groupes_calculs WHERE calcul = ?", (CALCUL,)).fetchone() is not None


def _paires_de(membres: list[str]) -> list[tuple[str, str]]:
    return [groupes.paire(a, b) for i, a in enumerate(membres) for b in membres[i + 1:]]


# --------------------------------------------------------------------------- avis (lecture)

def _lignes_avis(ret: sqlite3.Connection, paires: set[tuple[str, str]] | None = None) -> list[dict]:
    """Tous les avis, du plus récent au plus ancien ; restreints à `paires` si elles sont données."""
    groupes.preparer_retours(ret)
    lignes = [dict(r) for r in ret.execute(
        "SELECT id, cle_a, cle_b, reponse, commentaire, auteur, cree_le, groupe FROM avis_groupes"
        " ORDER BY cree_le DESC, id DESC")]
    if paires is not None:
        lignes = [x for x in lignes if (x["cle_a"], x["cle_b"]) in paires]
    return lignes


def _decisifs(lignes: list[dict]) -> dict[tuple[str, str], str]:
    """Par paire, la réponse du dernier avis tranché (« je ne sais pas » ne tranche rien)."""
    out: dict[tuple[str, str], str] = {}
    for x in reversed(lignes):                    # du plus ancien au plus récent : le dernier gagne
        if x["reponse"] in ("oui", "non"):
            out[(x["cle_a"], x["cle_b"])] = x["reponse"]
    return out


def statut_avis(membres: list[str], decisifs: dict[tuple[str, str], str]) -> dict:
    """« contesté » dès qu'une paire du groupe a reçu un « non », « confirmé » si elle n'a que des « oui »."""
    paires = _paires_de(membres)
    oui = sum(decisifs.get(p) == "oui" for p in paires)
    non = sum(decisifs.get(p) == "non" for p in paires)
    statut = "conteste" if non else "confirme" if oui else None
    return {"statut": statut, "oui": oui, "non": non, "paires": len(paires)}


def _noms(calc: sqlite3.Connection) -> dict[str, str]:
    return {r["cle"]: r["nom"] for r in calc.execute(
        "SELECT cle, nom FROM groupe_membres WHERE calcul = ? GROUP BY cle", (CALCUL,))}


def _regrouper(lignes: list[dict]) -> list[dict]:
    """Un avis donné pour tout un groupe est écrit une fois par paire ; on le relit comme un seul avis.

    Les lignes d'un même envoi partagent auteur, date, réponse, commentaire et groupe. Elles ne
    sont fusionnées que si elles couvrent toutes les paires du groupe : deux avis de paire donnés
    la même seconde ne sont jamais pris pour un avis de groupe.
    """
    blocs: dict[tuple, list[dict]] = defaultdict(list)
    for x in lignes:
        blocs[(x["auteur"], x["cree_le"], x["reponse"], x["commentaire"], x["groupe"])].append(x)
    sortie, vus = [], set()
    for x in lignes:                              # on garde l'ordre d'arrivée (le plus récent d'abord)
        cle = (x["auteur"], x["cree_le"], x["reponse"], x["commentaire"], x["groupe"])
        if cle in vus:
            continue
        vus.add(cle)
        bloc = blocs[cle]
        membres = (x["groupe"] or "").split("|")
        entier = (len(membres) >= 3
                  and {(b["cle_a"], b["cle_b"]) for b in bloc} == set(_paires_de(membres)))
        sortie.append({**x, "tout_le_groupe": entier, "paires": [(b["cle_a"], b["cle_b"]) for b in bloc]})
    return sortie


def _mettre_en_forme(avis: list[dict], noms: dict[str, str]) -> list[dict]:
    def nom(c):
        return noms.get(c, c)
    return [{
        "date": a["cree_le"], "auteur": a["auteur"], "reponse": a["reponse"],
        "commentaire": a["commentaire"] or "", "tout_le_groupe": a["tout_le_groupe"],
        "paires": [{"a": {"cle": p[0], "nom": nom(p[0])}, "b": {"cle": p[1], "nom": nom(p[1])}}
                   for p in a["paires"]]} for a in avis]


def derniers_avis(calc: sqlite3.Connection, ret: sqlite3.Connection, limite: int = 50) -> list[dict]:
    return _mettre_en_forme(_regrouper(_lignes_avis(ret))[:limite], _noms(calc))


# --------------------------------------------------------------------------- liste

def _statut_famille(statuts: list[str | None]) -> str | None:
    """Une famille est « contestée » dès qu'une de ses cliques l'est, « confirmée » si aucune ne l'est."""
    return "conteste" if "conteste" in statuts else "confirme" if "confirme" in statuts else None


def _familles(calc: sqlite3.Connection, ret: sqlite3.Connection) -> list[dict]:
    """Toutes les familles du calcul courant, avec leurs cliques résumées (une requête par table)."""
    noms = _noms(calc)
    decisifs = _decisifs(_lignes_avis(ret))
    cliques: dict[int, dict] = {}
    membres: dict[int, list[str]] = defaultdict(list)
    for r in calc.execute("SELECT groupe, cle FROM groupe_membres WHERE calcul = ?", (CALCUL,)):
        membres[r["groupe"]].append(r["cle"])
    for g in calc.execute(
            "SELECT id, score, taille, chef_de_file, structure, marches_3, marches_complet, acheteurs, regions,"
            " domaines, premier, dernier FROM groupes WHERE calcul = ?", (CALCUL,)):
        cles = sorted(membres[g["id"]], key=lambda c: normaliser(noms.get(c, c)))
        cliques[g["id"]] = {
            "id": g["id"], "score": g["score"], "taille": g["taille"], "structure": g["structure"],
            "marches": g["marches_3"], "marches_complet": g["marches_complet"], "acheteurs": g["acheteurs"],
            "regions": g["regions"], "domaines": g["domaines"],
            "premier": date_iso(g["premier"]), "dernier": date_iso(g["dernier"]),
            "membres": [{"cle": c, "nom": noms.get(c, c)} for c in cles],
            "avis": statut_avis(cles, decisifs)}
    par_famille: dict[str, list[int]] = defaultdict(list)
    for r in calc.execute("SELECT famille, groupe FROM famille_cliques WHERE calcul = ?", (CALCUL,)):
        par_famille[r["famille"]].append(r["groupe"])

    def personne(cle: str) -> dict:
        return {"cle": cle, "nom": noms.get(cle, cle)}

    sortie = []
    for f in calc.execute("SELECT * FROM familles WHERE calcul = ?", (CALCUL,)):
        liste = sorted((cliques[i] for i in par_famille[f["id"]]), key=lambda c: (-c["score"], c["id"]))
        permanents = sorted(json.loads(f["permanents"]), key=lambda c: normaliser(noms.get(c, c)))
        occasionnels = sorted(json.loads(f["occasionnels"]), key=lambda c: normaliser(noms.get(c, c)))
        sortie.append({
            "id": f["id"], "numero": f["numero"], "score": f["score"], "taille": f["taille"],
            "permanents": [personne(c) for c in permanents],
            "occasionnels": [personne(c) for c in occasionnels],
            "meilleure_clique": f["meilleure_clique"], "cliques": liste,
            "marches": max(c["marches"] for c in liste),
            "premier": min((c["premier"] for c in liste if c["premier"]), default=""),
            "dernier": max((c["dernier"] for c in liste), default=""),
            "avis": _statut_famille([c["avis"]["statut"] for c in liste])})
    return sortie


def lister(calc: sqlite3.Connection, ret: sqlite3.Connection, q: str = "", tri: str = "score") -> dict:
    """La liste des familles : l'unité affichée. Les cliques de chaque famille y sont résumées."""
    if not _prets(calc):
        return {"calcul": None, "total": 0, "familles": [], "cliques": 0}
    meta = dict(calc.execute("SELECT calcul, jusqua, cree_le, marches, societes, groupes FROM groupes_calculs"
                             " WHERE calcul = ?", (CALCUL,)).fetchone())
    mots = normaliser(q).split()
    sortie = []
    for f in _familles(calc, ret):
        # Tous les mots de la recherche doivent se trouver dans le nom d'un même membre.
        trouves = [m["cle"] for m in f["permanents"] + f["occasionnels"]
                   if all(mot in normaliser(m["nom"]) for mot in mots)] if mots else []
        if mots and not trouves:
            continue
        sortie.append({**f, "correspond": trouves})
    cle_tri = {"score": lambda f: (-f["score"], -f["marches"], f["numero"]),
               "taille": lambda f: (-f["taille"], -f["score"], f["numero"]),
               "marches": lambda f: (-f["marches"], -f["score"], f["numero"]),
               "recent": lambda f: (-f["score"], f["numero"])}
    sortie.sort(key=cle_tri[tri if tri in TRIS else "score"])
    if tri == "recent":
        sortie.sort(key=lambda f: f["dernier"], reverse=True)       # stable : à date égale, le meilleur score
    return {"calcul": meta, "total": len(sortie), "familles": sortie,
            "cliques": sum(len(f["cliques"]) for f in sortie)}


def famille(calc: sqlite3.Connection, ret: sqlite3.Connection, empreinte: str) -> dict | None:
    """Une famille par son identifiant. Un lien ancien dont la composition a changé mène à la famille actuelle
    qui partage le plus de membres avec l'ancienne ; `redirigee_depuis` et `message` le disent."""
    if not _prets(calc):
        return None
    toutes = _familles(calc, ret)
    f = next((x for x in toutes if x["id"] == empreinte), None)
    if f is not None:
        return {**f, "redirigee_depuis": None, "message": None}
    ancienne = calc.execute("SELECT permanents, occasionnels FROM familles_anciennes WHERE id = ?",
                            (empreinte,)).fetchone()
    if ancienne is None:
        return None
    avant = set(json.loads(ancienne["permanents"])) | set(json.loads(ancienne["occasionnels"]))

    def proximite(x: dict) -> tuple:
        """Sociétés en commun, puis part du total, puis score : la plus grande l'emporte."""
        maintenant = {m["cle"] for m in x["permanents"] + x["occasionnels"]}
        commun = len(avant & maintenant)
        return (commun, commun / len(avant | maintenant), x["score"])

    meilleur = max(toutes, key=proximite, default=None)
    if meilleur is None or proximite(meilleur)[0] == 0:
        return None
    return {**meilleur, "redirigee_depuis": empreinte,
            "message": f"La composition de cette famille a changé depuis ce lien : voici la famille actuelle qui "
                       f"partage le plus de sociétés avec elle ({proximite(meilleur)[0]} sur {len(avant)})."}


# --------------------------------------------------------------------------- explication du score

def _pct(x: float | None, chiffres: int = 0) -> str:
    return "—" if x is None else f"{x * 100:.{chiffres}f}".replace(".", ",") + " %"


def _n(x: float, chiffres: int = 1) -> str:
    return f"{x:.{chiffres}f}".replace(".", ",")


def _pts(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f}".replace(".", ",").replace("-", "−")


def expliquer(d: dict, paires: list[dict]) -> list[dict]:
    """Une ligne par composante du score : points obtenus, points possibles, et une phrase simple.

    Les nombres viennent tous du calcul stocké (groupes.details) : rien n'est recalculé ici.
    """
    comp = d.get("composantes", {})
    taille, force = d["taille"], d["force"]
    faible = min((min(p["part_a"], p["part_b"]) for p in paires), default=None)
    p_val, rapport, attendu = d["p_hasard"], d["rapport_hasard"], d["attendu_hasard"]
    domaines = d.get("domaines") or ""
    pos = d.get("position") or {}

    phrase_force = (f"Quand une société du groupe dépose une offre, une autre est présente dans "
                    f"{_pct(force)} des cas en moyenne ; pour la paire la moins liée, {_pct(faible)}.")
    phrase_volume = (f"{d['marches_3']} marchés réunissent au moins 3 membres du groupe, dont "
                     f"{d['marches_complet']} avec les {taille} membres au complet.")
    if p_val <= 0.05:
        verdict = "Cet écart est trop net pour s'expliquer facilement par le hasard."
    else:
        verdict = "L'écart n'est pas assez net pour écarter le hasard : la moitié des points est retirée."
    phrase_hasard = (f"Des sociétés d'activité comparable, tirées au hasard chez les mêmes acheteurs sur la même "
                     f"période, ne se croiseraient que sur {_n(attendu)} marchés de ce type en moyenne. Ici : "
                     f"{d['marches_3']}, soit {_n(rapport)} fois plus (probabilité d'un tel écart par hasard : "
                     f"{_n(p_val, 3)}). {verdict}")
    phrase_contextes = (f"Ces marchés viennent de {d['acheteurs']} acheteur(s), {d['regions']} région(s) et "
                        f"{len(domaines)} domaine(s) de qualification. Plus c'est varié, moins la présence commune "
                        f"s'explique par un seul chantier, un seul acheteur ou une seule région.")
    if d.get("hors_profil_evaluables"):
        phrase_hors = (f"{_pct(d['part_hors_profil'])} des présences sur ces marchés sont dans un domaine qui fait "
                       f"moins de 10 % de l'activité habituelle de la société (sur "
                       f"{d['hors_profil_evaluables']} présences évaluables). Un chiffre bas est normal.")
    else:
        phrase_hors = ("Non mesurable ici : trop peu de marchés avec une qualification connue pour juger du profil "
                       "des sociétés. Cette composante ne pèse que 5 points.")
    quand = {"chef de file": "Un membre domine les victoires du groupe.",
             "rotation": "Les victoires se répartissent entre plusieurs membres.",
             "peu de victoires": "Trop peu de victoires pour dire comment elles se répartissent."}[d["structure"]]
    phrase_gains = (f"Sur {d['unites']} marchés ou lots où un membre a déposé une offre, un membre du groupe "
                    f"l'a emporté {d['gains']} fois ({_pct(d['part_gains'])}). {quand}")
    if pos.get("membres_gagnant") is not None and pos.get("autres_gagnant") is not None:
        phrase_gains += (f" Quand un membre gagne, les offres non gagnantes des autres membres sont en médiane à "
                         f"{_pts(pos['membres_gagnant'])} point(s) de taux du gagnant, contre "
                         f"{_pts(pos['autres_gagnant'])} pour les autres soumissionnaires.")

    lignes = [
        ("force", "Présence mutuelle", phrase_force, "Moyenne, sur les paires du groupe, de la plus petite des deux "
         "parts de marchés partagés. 30 % donne 0 point, 100 % donne le maximum."),
        ("volume", "Nombre de marchés", phrase_volume, "Échelle logarithmique : 1 marché ≈ 18 % des points, "
         "10 marchés ≈ 61 %, 50 marchés et plus = tous les points."),
        ("hasard", "Comparaison au hasard", phrase_hasard, "200 tirages : chaque membre est remplacé par une société "
         "d'activité comparable, active chez les mêmes acheteurs. Rapport de 1 = 0 point, 10 et plus = tous les points."),
        ("contextes", "Variété des contextes", phrase_contextes, "Acheteurs (moitié des points, 5 et plus = complet), "
         "régions (quart, 3 et plus) et domaines (quart, 3 et plus)."),
        ("hors_profil", "Présence hors profil", phrase_hors, "Part des présences hors des domaines habituels, "
         "20 % et plus = tous les points."),
        ("gains", "Victoires", phrase_gains, "Part des marchés ou lots gagnés par un membre, 60 % et plus = tous les points."),
    ]
    return [{"cle": cle, "libelle": lib, "points": round(groupes.POIDS[cle] * comp.get(cle, 0.0), 1),
             "max": groupes.POIDS[cle], "phrase": phrase, "methode": methode}
            for cle, lib, phrase, methode in lignes]


def _roles(g: sqlite3.Row, membres: list[sqlite3.Row]) -> dict[str, str]:
    roles = {}
    for m in membres:
        if g["structure"] == "chef de file" and m["cle"] == g["chef_de_file"]:
            roles[m["cle"]] = "Chef de file probable"
        elif g["structure"] == "peu de victoires":
            roles[m["cle"]] = "Membre"
        elif m["gains_groupe"]:
            roles[m["cle"]] = "Membre qui gagne aussi" if g["structure"] == "rotation" else "Membre qui gagne"
        else:
            roles[m["cle"]] = "Membre qui ne gagne pas"
    return roles


# --------------------------------------------------------------------------- fiche d'un groupe

def _marches_communs(preuves: list[sqlite3.Row], membres: list[sqlite3.Row],
                     noms: dict[str, str]) -> tuple[list[dict], str | None]:
    """Les marchés de preuve, avec objet et taux de chaque membre lus dans pv_travaux.db (lecture seule)."""
    cles = {m["cle"] for m in membres}
    refs = [p["ref"] for p in preuves]
    par_ref, objets, avertissement = defaultdict(list), {}, None
    try:
        db = bases.historique()
        try:
            objets = {r["ref"]: r["objet"] for r in db.execute(
                f"SELECT ref, objet FROM marches WHERE principale = 1 AND ref IN ({','.join('?' * len(refs))})",
                refs)} if refs else {}
            for u in charger_unites(db, refs=refs):
                par_ref[u.ref].append(u)
        finally:
            db.close()
    except (FileNotFoundError, sqlite3.Error):
        avertissement = "L'historique n'est pas lisible : l'objet et les taux ne sont pas affichés."

    sortie = []
    for p in preuves:
        offres, gagnants = [], {}
        for u in par_ref.get(p["ref"], []):
            nom_de = {c["cle"]: c["nom"] for c in u.concurrents}
            if u.cle_gagnant:
                g = gagnants.setdefault(u.cle_gagnant, {"cle": u.cle_gagnant, "lots": [],
                                                       "nom": nom_de.get(u.cle_gagnant) or noms.get(u.cle_gagnant)
                                                              or u.cle_gagnant,
                                                       "membre": u.cle_gagnant in cles})
                if u.lot is not None:
                    g["lots"].append(u.lot)
            for c in u.concurrents:
                if c["cle"] in cles:
                    offres.append({"cle": c["cle"], "lot": u.lot, "taux": c["taux"], "statut": c["statut"],
                                   "gagne": u.cle_gagnant == c["cle"]})
        sortie.append({"ref": p["ref"], "date": date_iso(p["date"]), "acheteur": p["acheteur"],
                       "objet": (objets.get(p["ref"]) or "")[:400], "presents": p["presents"],
                       "complet": bool(p["complet"]), "gagnants": list(gagnants.values()), "offres": offres})
    sortie.sort(key=lambda m: m["date"], reverse=True)
    return sortie, avertissement


def fiche(calc: sqlite3.Connection, ret: sqlite3.Connection, gid: int) -> dict | None:
    if not _prets(calc):
        return None
    g = calc.execute("SELECT * FROM groupes WHERE calcul = ? AND id = ?", (CALCUL, gid)).fetchone()
    if g is None:
        return None
    membres = calc.execute("SELECT * FROM groupe_membres WHERE calcul = ? AND groupe = ? ORDER BY nom",
                           (CALCUL, gid)).fetchall()
    cles = [m["cle"] for m in membres]
    noms = {m["cle"]: m["nom"] for m in membres}
    appartenance = calc.execute("SELECT famille FROM famille_cliques WHERE calcul = ? AND groupe = ?",
                                (CALCUL, gid)).fetchone()
    d = json.loads(g["details"])
    roles = _roles(g, membres)

    marques = ",".join("?" * len(cles))
    lignes_paires = {(r["cle_a"], r["cle_b"]): r for r in calc.execute(
        f"SELECT * FROM groupes_paires WHERE calcul = ? AND cle_a IN ({marques}) AND cle_b IN ({marques})",
        (CALCUL, *cles, *cles))}
    lignes = _lignes_avis(ret, set(_paires_de(cles)))
    decisifs = _decisifs(lignes)
    par_paire = defaultdict(list)
    for x in lignes:
        par_paire[(x["cle_a"], x["cle_b"])].append(x)

    paires = []
    for (a, b) in _paires_de(cles):
        r = lignes_paires.get((a, b))
        hist = par_paire.get((a, b), [])
        tranche = next((x for x in hist if x["reponse"] in ("oui", "non")), None)     # hist : le plus récent d'abord
        paires.append({
            "a": {"cle": a, "nom": noms[a]}, "b": {"cle": b, "nom": noms[b]},
            "communs": r["communs"] if r else None, "part_a": r["part_a"] if r else None,
            "part_b": r["part_b"] if r else None, "origine": r["origine"] if r else "",
            "avis": ({"reponse": tranche["reponse"], "auteur": tranche["auteur"], "date": tranche["cree_le"]}
                     if tranche else None), "nb_avis": len(hist)})

    preuves = calc.execute("SELECT * FROM groupe_preuves WHERE calcul = ? AND groupe = ? ORDER BY date DESC",
                           (CALCUL, gid)).fetchall()
    marches, avertissement = _marches_communs(preuves[:MAX_MARCHES_FICHE], membres, noms)

    return {
        "id": g["id"], "famille": g["famille"], "famille_id": appartenance["famille"] if appartenance else None,
        "score": g["score"], "taille": g["taille"],
        "structure": g["structure"], "chef_de_file": g["chef_de_file"],
        "premier": date_iso(g["premier"]), "dernier": date_iso(g["dernier"]),
        "marches": g["marches_3"], "marches_complet": g["marches_complet"],
        "acheteurs": g["acheteurs"], "regions": g["regions"],
        "avis": statut_avis(cles, decisifs),
        "membres": [{
            "cle": m["cle"], "nom": m["nom"], "role": roles[m["cle"]], "marches": m["marches"],
            "presences_groupe": m["presences_groupe"], "gains_groupe": m["gains_groupe"],
            "taux_avec": m["taux_avec"], "n_avec": m["n_avec"],
            "taux_sans": m["taux_sans"], "n_sans": m["n_sans"]} for m in membres],
        "explication": expliquer(d, paires),
        "paires": paires, "question": QUESTION,
        "marches_communs": marches, "marches_total": len(preuves),
        "avertissement": avertissement,
        "historique": _mettre_en_forme(_regrouper(lignes), noms)}


# --------------------------------------------------------------------------- avis (écriture)

def enregistrer_avis(calc: sqlite3.Connection, ret: sqlite3.Connection, auteur: str, reponse: str,
                     commentaire: str = "", groupe: int | None = None,
                     cle_a: str | None = None, cle_b: str | None = None) -> dict:
    """Écrit un avis (une paire) ou le même avis pour toutes les paires d'un groupe.

    L'auteur vient de la session, jamais de la requête. Une société qui n'appartient à aucun groupe
    du calcul courant, ou une paire qui ne se trouve pas dans le groupe indiqué, est refusée.
    """
    if reponse not in REPONSES:
        raise ErreurAvis("Réponse inconnue : oui, non ou nsp.")
    commentaire = (commentaire or "").strip()
    if len(commentaire) > MAX_COMMENTAIRE:
        raise ErreurAvis(f"Commentaire trop long ({MAX_COMMENTAIRE} caractères au plus).")
    if not _prets(calc):
        raise ErreurAvis("Aucun calcul de groupes : lancez python -m copilote.recalculer groupes.", 409)

    signature, membres_du_groupe = None, None
    if groupe is not None:
        ligne = calc.execute("SELECT signature FROM groupes WHERE calcul = ? AND id = ?", (CALCUL, groupe)).fetchone()
        if ligne is None:
            raise ErreurAvis("Groupe introuvable.", 404)
        signature = ligne["signature"]
        membres_du_groupe = signature.split("|")

    if cle_a or cle_b:
        if not (cle_a and cle_b) or cle_a == cle_b:
            raise ErreurAvis("Une paire demande deux sociétés différentes.")
        a, b = groupes.paire(cle_a, cle_b)
        connues = {r["cle"] for r in calc.execute("SELECT DISTINCT cle FROM groupe_membres WHERE calcul = ?", (CALCUL,))}
        if a not in connues or b not in connues:
            raise ErreurAvis("Société inconnue : elle ne figure dans aucun groupe.", 404)
        if membres_du_groupe is not None and not (a in membres_du_groupe and b in membres_du_groupe):
            raise ErreurAvis("Ces deux sociétés ne font pas partie de ce groupe.")
        paires = [(a, b)]
        if signature is None:
            # Sans groupe indiqué, on garde en mémoire le plus fort des groupes qui les contiennent.
            cherche = calc.execute(
                "SELECT g.signature FROM groupes g WHERE g.calcul = ? AND g.id IN"
                " (SELECT m1.groupe FROM groupe_membres m1 JOIN groupe_membres m2"
                "  ON m1.calcul = m2.calcul AND m1.groupe = m2.groupe"
                "  WHERE m1.calcul = ? AND m1.cle = ? AND m2.cle = ?) ORDER BY g.score DESC LIMIT 1",
                (CALCUL, CALCUL, a, b)).fetchone()
            signature = cherche["signature"] if cherche else None
    elif groupe is not None:
        paires = _paires_de(membres_du_groupe)
    else:
        raise ErreurAvis("Indiquez une paire (cle_a, cle_b) ou un groupe.")

    groupes.preparer_retours(ret)
    maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ret:
        ret.executemany(
            "INSERT INTO avis_groupes (cle_a, cle_b, reponse, commentaire, auteur, cree_le, groupe)"
            " VALUES (?,?,?,?,?,?,?)",
            [(a, b, reponse, commentaire, auteur, maintenant, signature) for a, b in paires])
    return {"enregistres": len(paires), "auteur": auteur, "cree_le": maintenant, "reponse": reponse}
