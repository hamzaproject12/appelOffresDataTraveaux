"""Ce que la page /copilote/groupes lit et écrit.

Lecture : copilote_calcul.db (calcul « courant »), complétée pour la fiche d'un groupe par
pv_travaux.db en lecture seule (objet, taux de chaque membre) et, pour la recherche, par sa table
`societes`. Écriture : uniquement les avis, dans copilote_retours.db. Rien ici ne touche aux calculs :
un avis agit sur les groupes au recalcul suivant (groupes.lire_avis), et tout de suite sur la fiche
d'une société.

Vocabulaire : « groupe de présence commune », « signal », « partenaire de présence ». Un groupe est
un fait statistique.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

from . import bases, groupes
from . import fusions as _fusions
from . import liens as _liens
from .donnees import charger_unites

CALCUL = "courant"
TRIS = ("score", "taille", "marches", "recent")
FILTRES = ("fort", "moyen", "faible", "confirme")
FILTRE_DEFAUT = ("fort", "moyen", "confirme")
REPONSES = ("oui", "non", "nsp")
MAX_COMMENTAIRE = 1000
MAX_MARCHES_FICHE = 400
TAILLE_PAGE = 25
MAX_RECHERCHE = 20

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
    """Vrai si un calcul « courant » de cette version existe : sinon la page dit de lancer le recalcul."""
    if not groupes.a_jour(calc):
        return False
    try:
        return calc.execute("SELECT 1 FROM groupes_calculs WHERE calcul = ?", (CALCUL,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _paires_de(membres: list[str]) -> list[tuple[str, str]]:
    return [groupes.paire(a, b) for i, a in enumerate(membres) for b in membres[i + 1:]]


def _marques(n: int) -> str:
    return ",".join("?" * n)


def _noms(calc: sqlite3.Connection, cles=None) -> dict[str, str]:
    """Nom de chaque société du calcul (toutes, ou seulement `cles`)."""
    if cles is None:
        return {r["cle"]: r["nom"] for r in calc.execute(
            "SELECT cle, nom FROM groupes_societes WHERE calcul = ?", (CALCUL,))}
    cles = list(set(cles))
    sortie = {}
    for k in range(0, len(cles), 500):
        morceau = cles[k:k + 500]
        sortie.update({r["cle"]: r["nom"] for r in calc.execute(
            f"SELECT cle, nom FROM groupes_societes WHERE calcul = ? AND cle IN ({_marques(len(morceau))})",
            (CALCUL, *morceau))})
    return sortie


def _personne(cle: str, noms: dict[str, str]) -> dict:
    return {"cle": cle, "nom": noms.get(cle, cle)}


def _meta(calc: sqlite3.Connection) -> dict:
    r = calc.execute("SELECT calcul, jusqua, cree_le, marches, societes, groupes, details FROM groupes_calculs"
                     " WHERE calcul = ?", (CALCUL,)).fetchone()
    meta = {k: r[k] for k in ("calcul", "jusqua", "cree_le", "marches", "societes", "groupes")}
    meta["familles"] = json.loads(r["details"] or "{}").get("familles")
    return meta


def reglages_page() -> dict:
    rg = _liens.reglages_groupes()
    return {"actives": rg["actives"], "accompagne": rg["accompagne"], "partenaires": rg["partenaires"],
            "niveaux": rg["niveaux"], "mutuel": {k: v for k, v in rg["mutuel"].items() if not k.startswith("_")}}


def entete(calc: sqlite3.Connection) -> dict:
    """« N sociétés actives (5 marchés ou plus), dont M ont au moins un partenaire détecté »."""
    actives = _liens.reglages_groupes()["actives"]
    r = calc.execute(
        "SELECT COUNT(*) n, SUM(fort + moyen + faible > 0) avec, SUM(fort + moyen > 0) moyen_fort,"
        " SUM(fort > 0) fort FROM groupes_societes WHERE calcul = ? AND marches >= ?", (CALCUL, actives)).fetchone()
    toutes = calc.execute("SELECT COUNT(*) FROM groupes_societes WHERE calcul = ?", (CALCUL,)).fetchone()[0]
    return {"seuil_actives": actives, "actives": r["n"] or 0, "avec_partenaire": r["avec"] or 0,
            "avec_moyen_ou_fort": r["moyen_fort"] or 0, "avec_fort": r["fort"] or 0, "societes": toutes}


# --------------------------------------------------------------------------- avis (lecture)

def _lignes_avis(ret: sqlite3.Connection, paires: set[tuple[str, str]] | None = None,
                 cle: str | None = None) -> list[dict]:
    """Les avis, du plus récent au plus ancien ; restreints à `paires`, ou à ceux qui touchent `cle`."""
    groupes.preparer_retours(ret)
    sql = "SELECT id, cle_a, cle_b, reponse, commentaire, auteur, cree_le, groupe FROM avis_groupes"
    args: tuple = ()
    if cle is not None:
        sql += " WHERE cle_a = ? OR cle_b = ?"
        args = (cle, cle)
    lignes = [dict(r) for r in ret.execute(sql + " ORDER BY cree_le DESC, id DESC", args)]
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
    lignes = _regrouper(_lignes_avis(ret))[:limite]
    cles = {c for a in lignes for p in a["paires"] for c in p}
    noms = _noms(calc, cles) if _prets(calc) else {}
    return _mettre_en_forme(lignes, noms)


# --------------------------------------------------------------------------- recherche de sociétés

_annuaire: dict = {}
_verrou = threading.Lock()


def _charger_annuaire(calc: sqlite3.Connection) -> list[dict]:
    """Toutes les sociétés : celles du calcul, et toutes celles de la table `societes` de pv_travaux.db
    (fusions suivies, l'ancien nom reste cherchable). Gardé en mémoire tant que rien ne change."""
    meta = calc.execute("SELECT cree_le FROM groupes_calculs WHERE calcul = ?", (CALCUL,)).fetchone()
    try:
        chemin = bases.chemin_historique()
        st = chemin.stat()
        cle_hist = (str(chemin), st.st_mtime_ns, st.st_size)
    except OSError:
        cle_hist = None
    cle = (meta[0] if meta else None, cle_hist)
    with _verrou:
        if _annuaire.get("cle") == cle:
            return _annuaire["lignes"]
        par_cle: dict[str, dict] = {}
        for r in calc.execute("SELECT cle, nom, marches, fort, moyen, faible, groupes FROM groupes_societes"
                              " WHERE calcul = ?", (CALCUL,)):
            par_cle[r["cle"]] = {**dict(r), "noms": {normaliser(r["nom"])}}
        if cle_hist is not None:
            try:
                table_fusions = _fusions.charger()
                db = bases.historique()
                try:
                    for r in db.execute("SELECT cle, nom FROM societes"):
                        c = _fusions.appliquer(r["cle"], table_fusions)
                        x = par_cle.get(c)
                        if x is None:
                            x = par_cle[c] = {"cle": c, "nom": r["nom"], "marches": 0, "fort": 0, "moyen": 0,
                                              "faible": 0, "groupes": 0, "noms": set()}
                        x["noms"].add(normaliser(r["nom"]))
                finally:
                    db.close()
            except (FileNotFoundError, sqlite3.Error):
                pass                              # base de test sans table societes : le calcul suffit
        lignes = sorted(par_cle.values(), key=lambda x: (-x["marches"], x["nom"]))
        _annuaire.update(cle=cle, lignes=lignes)
        return lignes


def chercher_societes(calc: sqlite3.Connection, q: str, limite: int = MAX_RECHERCHE) -> dict:
    """Sociétés dont un nom contient tous les mots de `q`. Nom exact d'abord, puis début de nom, puis activité."""
    mots = normaliser(q).split()
    if not mots or not _prets(calc):
        return {"total": 0, "societes": []}
    phrase = " ".join(mots)
    trouves = []
    for x in _charger_annuaire(calc):
        rang = None
        for nom in x["noms"]:
            if all(m in nom for m in mots):
                r = 0 if nom == phrase else 1 if nom.startswith(phrase) else 2
                rang = r if rang is None else min(rang, r)
        if rang is not None:
            trouves.append((rang, x))
    trouves.sort(key=lambda t: t[0])              # tri stable : à rang égal, l'ordre d'activité
    return {"total": len(trouves), "societes": [
        {k: x[k] for k in ("cle", "nom", "marches", "fort", "moyen", "faible", "groupes")}
        for _, x in trouves[:limite]]}


# --------------------------------------------------------------------------- liste des familles

def _familles_avec_paire(calc: sqlite3.Connection, a: str, b: str) -> set[str]:
    return {r[0] for r in calc.execute(
        "SELECT DISTINCT fc.famille FROM groupe_membres m1 JOIN groupe_membres m2"
        " ON m2.calcul = m1.calcul AND m2.groupe = m1.groupe"
        " JOIN famille_cliques fc ON fc.calcul = m1.calcul AND fc.groupe = m1.groupe"
        " WHERE m1.calcul = ? AND m1.cle = ? AND m2.cle = ?", (CALCUL, a, b))}


def _etat_avis_familles(calc: sqlite3.Connection, ret: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """Familles qui ont au moins une paire confirmée (« oui ») et celles qui en ont une contestée (« non »)."""
    oui, non = set(), set()
    for (a, b), reponse in _decisifs(_lignes_avis(ret)).items():
        (oui if reponse == "oui" else non).update(_familles_avec_paire(calc, a, b))
    return oui, non


def _resume_famille(f: sqlite3.Row, noms: dict[str, str], oui: set[str], non: set[str]) -> dict:
    permanents = sorted(json.loads(f["permanents"]), key=lambda c: normaliser(noms.get(c, c)))
    occasionnels = sorted(json.loads(f["occasionnels"]), key=lambda c: normaliser(noms.get(c, c)))
    niveaux = set(filter(None, (f["niveaux"] or "").split(",")))
    if f["id"] in oui:
        niveaux.add("confirme")
    meilleur = next((n for n in _liens.NIVEAUX if n in niveaux), "sans niveau")
    return {
        "id": f["id"], "numero": f["numero"], "score": f["score"], "taille": f["taille"],
        "permanents": [_personne(c, noms) for c in permanents],
        "occasionnels": [_personne(c, noms) for c in occasionnels],
        "nb_cliques": f["cliques"], "meilleure_clique": f["meilleure_clique"], "marches": f["marches"],
        "premier": date_iso(f["premier"]), "dernier": date_iso(f["dernier"]),
        "niveaux": sorted(niveaux), "niveau": meilleur,
        "avis": "conteste" if f["id"] in non else "confirme" if f["id"] in oui else None}


def lister(calc: sqlite3.Connection, ret: sqlite3.Connection, q: str = "", tri: str = "score",
           niveaux: tuple[str, ...] | list[str] | None = None, page: int = 1, taille: int = TAILLE_PAGE) -> dict:
    """Une page de familles, filtrée par niveau et par société, avec un compteur par niveau.

    Une famille passe le filtre si l'une de ses cliques a ce niveau (celui de sa paire la plus faible),
    ou, pour « confirmé », si l'une de ses paires a reçu un avis oui."""
    if not _prets(calc):
        return {"calcul": None, "total": 0, "familles": [], "compteurs": {}, "page": 1, "pages": 0,
                "societes": [], "entete": None}
    voulus = set(niveaux) if niveaux else set(FILTRE_DEFAUT)
    oui, non = _etat_avis_familles(calc, ret)
    lignes = calc.execute("SELECT * FROM familles WHERE calcul = ?", (CALCUL,)).fetchall()

    societes, correspond = [], {}
    if normaliser(q):
        trouvees = chercher_societes(calc, q, limite=10_000)
        societes = trouvees["societes"][:MAX_RECHERCHE]
        cherchees = {s["cle"] for s in trouvees["societes"]}
        gardees = []
        for f in lignes:
            membres = set(json.loads(f["permanents"])) | set(json.loads(f["occasionnels"]))
            communs = membres & cherchees
            if communs:
                correspond[f["id"]] = sorted(communs)
                gardees.append(f)
        lignes = gardees

    def niveaux_de(f) -> set[str]:
        n = set(filter(None, (f["niveaux"] or "").split(",")))
        return n | {"confirme"} if f["id"] in oui else n

    compteurs = {n: 0 for n in FILTRES}
    for f in lignes:
        for n in niveaux_de(f) & set(FILTRES):
            compteurs[n] += 1
    lignes = [f for f in lignes if niveaux_de(f) & voulus]
    cle_tri = {"score": lambda f: (-f["score"], -f["marches"], f["numero"]),
               "taille": lambda f: (-f["taille"], -f["score"], f["numero"]),
               "marches": lambda f: (-f["marches"], -f["score"], f["numero"]),
               "recent": lambda f: (-f["score"], f["numero"])}
    lignes.sort(key=cle_tri[tri if tri in TRIS else "score"])
    if tri == "recent":
        lignes.sort(key=lambda f: f["dernier"], reverse=True)       # stable : à date égale, le meilleur score
    total = len(lignes)
    pages = max(1, -(-total // taille))
    page = min(max(1, page), pages)
    morceau = lignes[(page - 1) * taille: page * taille]
    noms = _noms(calc, {c for f in morceau for c in json.loads(f["permanents"]) + json.loads(f["occasionnels"])})
    familles = [{**_resume_famille(f, noms, oui, non), "correspond": correspond.get(f["id"], [])} for f in morceau]
    return {"calcul": _meta(calc), "entete": entete(calc), "total": total, "page": page, "pages": pages,
            "taille_page": taille, "niveaux": sorted(voulus), "compteurs": compteurs,
            "familles": familles, "societes": societes, "reglages": reglages_page()}


def _cliques_de(calc: sqlite3.Connection, ids: list[int], decisifs: dict) -> list[dict]:
    if not ids:
        return []
    membres: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for r in calc.execute(f"SELECT groupe, cle, nom FROM groupe_membres WHERE calcul = ? AND groupe IN"
                          f" ({_marques(len(ids))})", (CALCUL, *ids)):
        membres[r["groupe"]].append((r["cle"], r["nom"]))
    sortie = []
    for g in calc.execute(
            f"SELECT id, score, taille, niveau, confirme, structure, marches_3, marches_complet, acheteurs,"
            f" regions, domaines, taux_victoire, premier, dernier FROM groupes WHERE calcul = ? AND id IN"
            f" ({_marques(len(ids))})", (CALCUL, *ids)):
        ms = sorted(membres[g["id"]], key=lambda x: normaliser(x[1]))
        sortie.append({
            "id": g["id"], "score": g["score"], "taille": g["taille"], "niveau": g["niveau"],
            "structure": g["structure"], "marches": g["marches_3"], "marches_complet": g["marches_complet"],
            "acheteurs": g["acheteurs"], "regions": g["regions"], "domaines": g["domaines"],
            "taux_victoire": g["taux_victoire"],
            "premier": date_iso(g["premier"]), "dernier": date_iso(g["dernier"]),
            "membres": [{"cle": c, "nom": n} for c, n in ms],
            "avis": statut_avis([c for c, _ in ms], decisifs)})
    return sorted(sortie, key=lambda c: (-c["score"], c["id"]))


def famille(calc: sqlite3.Connection, ret: sqlite3.Connection, empreinte: str) -> dict | None:
    """Une famille par son identifiant. Un lien ancien dont la composition a changé mène à la famille actuelle
    qui partage le plus de membres avec l'ancienne ; `redirigee_depuis` et `message` le disent."""
    if not _prets(calc):
        return None
    f = calc.execute("SELECT * FROM familles WHERE calcul = ? AND id = ?", (CALCUL, empreinte)).fetchone()
    redirigee, message = None, None
    if f is None:
        ancienne = calc.execute("SELECT permanents, occasionnels FROM familles_anciennes WHERE id = ?",
                                (empreinte,)).fetchone()
        if ancienne is None:
            return None
        avant = set(json.loads(ancienne["permanents"])) | set(json.loads(ancienne["occasionnels"]))

        def proximite(x) -> tuple:
            """Sociétés en commun, puis part du total, puis score : la plus grande l'emporte."""
            maintenant = set(json.loads(x["permanents"])) | set(json.loads(x["occasionnels"]))
            commun = len(avant & maintenant)
            return (commun, commun / len(avant | maintenant), x["score"])

        f = max(calc.execute("SELECT * FROM familles WHERE calcul = ?", (CALCUL,)), key=proximite, default=None)
        if f is None or proximite(f)[0] == 0:
            return None
        redirigee = empreinte
        message = (f"La composition de cette famille a changé depuis ce lien : voici la famille actuelle qui "
                   f"partage le plus de sociétés avec elle ({proximite(f)[0]} sur {len(avant)}).")
    oui, non = _etat_avis_familles(calc, ret)
    noms = _noms(calc, json.loads(f["permanents"]) + json.loads(f["occasionnels"]))
    ids = [r[0] for r in calc.execute("SELECT groupe FROM famille_cliques WHERE calcul = ? AND famille = ?",
                                      (CALCUL, f["id"]))]
    cliques = _cliques_de(calc, ids, _decisifs(_lignes_avis(ret)))
    return {**_resume_famille(f, noms, oui, non), "cliques": cliques,
            "redirigee_depuis": redirigee, "message": message}


# --------------------------------------------------------------------------- explication du score

def _pct(x: float | None, chiffres: int = 0) -> str:
    return "—" if x is None else f"{x * 100:.{chiffres}f}".replace(".", ",") + " %"


def _n(x: float, chiffres: int = 1) -> str:
    return f"{x:.{chiffres}f}".replace(".", ",")


def _pts(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f}".replace(".", ",").replace("-", "−")


def _p(x: float) -> str:
    return "moins de 0,0001" if x < 0.0001 else _n(x, 4)


def expliquer(d: dict, paires: list[dict]) -> list[dict]:
    """Une ligne par composante du score : points obtenus, points possibles, et une phrase simple.

    Les nombres viennent tous du calcul stocké (groupes.details) : rien n'est recalculé ici.
    """
    comp = d.get("composantes", {})
    poids = d.get("poids") or groupes.poids_effectifs()
    taille, force = d["taille"], d["force"]
    mutuelle = d.get("force_mutuelle")
    faible = min((min(p["part_a"], p["part_b"]) for p in paires if p["part_a"] is not None), default=None)
    p_val, rapport, attendu = d["p_hasard"], d["rapport_hasard"], d["attendu_hasard"]
    domaines = d.get("domaines") or ""
    pos = d.get("position") or {}

    phrase_force = (f"Pour chaque paire du groupe, on prend le sens le plus fort : la société la plus suivie retrouve "
                    f"l’autre sur {_pct(force)} de ses marchés en moyenne. Présence dans les deux sens (la plus petite "
                    f"des deux parts) : {_pct(mutuelle)} en moyenne, {_pct(faible)} pour la paire la moins liée.")
    if taille == 2:
        phrase_volume = f"{d['marches_3']} marchés réunissent les deux sociétés."
    else:
        phrase_volume = (f"{d['marches_3']} marchés réunissent au moins 3 membres du groupe, dont "
                         f"{d['marches_complet']} avec les {taille} membres au complet.")
    if p_val <= 0.05:
        verdict = "Cet écart est trop net pour s'expliquer facilement par le hasard."
    else:
        verdict = "L'écart n'est pas assez net pour écarter le hasard : la moitié des points est retirée."
    if d.get("methode_hasard") == "poisson":
        phrase_hasard = (f"Si chacune gardait ses acheteurs et son nombre de marchés, le hasard les réunirait sur "
                         f"{_n(attendu)} marchés. Ici : {d['marches_3']}, soit un excès de {_n(rapport)} "
                         f"(probabilité d’un tel écart par hasard : {_p(p_val)}). {verdict}")
        methode_hasard = ("Attendu = somme, sur les acheteurs, de (marchés de A chez l’acheteur × marchés de B chez "
                          "l’acheteur ÷ marchés de l’acheteur) ; probabilité de Poisson. Excès de 1 = 0 point, "
                          "10 et plus = tous les points.")
    else:
        phrase_hasard = (f"Des sociétés d'activité comparable, tirées au hasard chez les mêmes acheteurs sur la même "
                         f"période, ne se croiseraient que sur {_n(attendu)} marchés de ce type en moyenne. Ici : "
                         f"{d['marches_3']}, soit {_n(rapport)} fois plus (probabilité d'un tel écart par hasard : "
                         f"{_n(p_val, 3)}). {verdict}")
        methode_hasard = ("200 tirages : chaque membre est remplacé par une société d'activité comparable, active chez "
                          "les mêmes acheteurs. Rapport de 1 = 0 point, 10 et plus = tous les points.")
    phrase_contextes = (f"Ces marchés viennent de {d['acheteurs']} acheteur(s), {d['regions']} région(s) et "
                        f"{len(domaines)} domaine(s) de qualification. Plus c'est varié, moins la présence commune "
                        f"s'explique par un seul chantier, un seul acheteur ou une seule région.")
    if d.get("hors_profil_evaluables"):
        phrase_hors = (f"{_pct(d['part_hors_profil'])} des présences sur ces marchés sont dans un domaine qui fait "
                       f"moins de 10 % de l'activité habituelle de la société (sur "
                       f"{d['hors_profil_evaluables']} présences évaluables). Un chiffre bas est normal.")
    else:
        phrase_hors = ("Non mesurable ici : trop peu de marchés avec une qualification connue pour juger du profil "
                       "des sociétés.")
    quand = {"chef de file": "Un membre remporte la plupart des victoires du groupe.",
             "rotation": "Les victoires se répartissent entre plusieurs membres.",
             "peu de victoires": "Trop peu de victoires pour dire comment elles se répartissent."}[d["structure"]]
    phrase_gains = (f"Sur {d['unites']} marchés ou lots jugés où un membre a déposé une offre, un membre du groupe "
                    f"l'a emporté {d['gains']} fois ({_pct(d['part_gains'])}). {quand} Ce critère décrit le "
                    f"comportement du groupe : il ne compte pas dans le score, un groupe qui gagne peu n’est pas "
                    f"moins lié.")
    if pos.get("membres_gagnant") is not None and pos.get("autres_gagnant") is not None:
        phrase_gains += (f" Quand un membre gagne, les offres non gagnantes des autres membres sont en médiane à "
                         f"{_pts(pos['membres_gagnant'])} point(s) de taux du gagnant, contre "
                         f"{_pts(pos['autres_gagnant'])} pour les autres soumissionnaires.")

    lignes = [
        ("force", "Présence commune", phrase_force, "Moyenne, sur les paires du groupe, de la plus grande des deux "
         "parts de marchés partagés. 30 % donne 0 point, 100 % donne le maximum."),
        ("volume", "Nombre de marchés", phrase_volume, "Échelle logarithmique : 1 marché ≈ 18 % des points, "
         "10 marchés ≈ 61 %, 50 marchés et plus = tous les points."),
        ("hasard", "Comparaison au hasard", phrase_hasard, methode_hasard),
        ("contextes", "Variété des contextes", phrase_contextes, "Acheteurs (moitié des points, 5 et plus = complet), "
         "régions (quart, 3 et plus) et domaines (quart, 3 et plus)."),
        ("hors_profil", "Présence hors profil", phrase_hors, "Part des présences hors des domaines habituels, "
         "20 % et plus = tous les points."),
        ("gains", "Qui gagne (descriptif)", phrase_gains, "Part des marchés ou lots gagnés par un membre. "
         "Poids 0 : affiché pour décrire le groupe, sans effet sur le score."),
    ]
    return [{"cle": cle, "libelle": lib, "points": round(poids[cle] * comp.get(cle, 0.0), 1),
             "max": round(poids[cle], 1), "phrase": phrase, "methode": methode}
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
                f"SELECT ref, objet FROM marches WHERE principale = 1 AND ref IN ({_marques(len(refs))})",
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


def _defaites(calc: sqlite3.Connection, ou: str, args: tuple, noms: dict[str, str]) -> list[dict]:
    """Les marchés ou lots perdus (groupes_defaites), du plus récent au plus ancien."""
    return [{
        "ref": r["ref"], "lot": r["lot"], "date": date_iso(r["date"]), "acheteur": r["acheteur"],
        "gagnant": {"cle": r["gagnant_cle"], "nom": r["gagnant_nom"]},
        "taux_gagnant": r["taux_gagnant"], "taux_reference": r["taux_reference"],
        "meilleur_membre": _personne(r["meilleur_membre"], noms) if r["meilleur_membre"] else None,
        "taux_meilleur": r["taux_meilleur"], "ecart_meilleure": r["ecart_meilleure"],
        "ecart_reference": r["ecart_reference"], "membres_ecartes": bool(r["membres_ecartes"])}
        for r in calc.execute(f"SELECT * FROM groupes_defaites WHERE calcul = ? AND {ou}"
                              f" ORDER BY date DESC, ref, lot", (CALCUL, *args))]


def _victoires(v: dict | None, noms: dict[str, str]) -> dict | None:
    """Le résumé « qui gagne » tel que le calcul l'a rangé, avec les noms."""
    if not v:
        return None
    return {k: v.get(k) for k in ("unites", "gagnees", "taux", "perdues", "perdues_ecartes",
                                  "ecart_meilleure", "ecart_reference")} | {
        "gains": [{**_personne(c, noms), "fois": n} for c, n in sorted(v.get("gains", {}).items(), key=lambda x: -x[1])],
        "gagnants_exterieurs": v.get("gagnants_exterieurs", [])}


def _paire_sortie(r: sqlite3.Row | None, a: str, b: str, noms: dict[str, str], hist: list[dict]) -> dict:
    tranche = next((x for x in hist if x["reponse"] in ("oui", "non")), None)     # hist : le plus récent d'abord
    return {
        "a": _personne(a, noms), "b": _personne(b, noms),
        "communs": r["communs"] if r else None, "part_a": r["part_a"] if r else None,
        "part_b": r["part_b"] if r else None, "exces": r["exces"] if r else None,
        "attendu": r["attendu"] if r else None, "p": r["p"] if r else None,
        "niveau": r["niveau"] if r else None, "mutuel": bool(r["mutuel"]) if r else False,
        "origine": r["origine"] if r else "",
        "avis": ({"reponse": tranche["reponse"], "auteur": tranche["auteur"], "date": tranche["cree_le"]}
                 if tranche else None), "nb_avis": len(hist)}


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

    marques = _marques(len(cles))
    lignes_paires = {(r["cle_a"], r["cle_b"]): r for r in calc.execute(
        f"SELECT * FROM groupes_liens WHERE calcul = ? AND cle_a IN ({marques}) AND cle_b IN ({marques})",
        (CALCUL, *cles, *cles))}
    lignes = _lignes_avis(ret, set(_paires_de(cles)))
    decisifs = _decisifs(lignes)
    par_paire = defaultdict(list)
    for x in lignes:
        par_paire[(x["cle_a"], x["cle_b"])].append(x)
    paires = [_paire_sortie(lignes_paires.get((a, b)), a, b, noms, par_paire.get((a, b), []))
              for (a, b) in _paires_de(cles)]

    preuves = calc.execute("SELECT * FROM groupe_preuves WHERE calcul = ? AND groupe = ? ORDER BY date DESC",
                           (CALCUL, gid)).fetchall()
    marches, avertissement = _marches_communs(preuves[:MAX_MARCHES_FICHE], membres, noms)

    return {
        "id": g["id"], "famille": g["famille"], "famille_id": appartenance["famille"] if appartenance else None,
        "score": g["score"], "taille": g["taille"], "niveau": g["niveau"], "confirme": bool(g["confirme"]),
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
        "victoires": _victoires(d.get("victoires"), noms),
        "defaites": _defaites(calc, "type = 'groupe' AND groupe = ?", (gid,), noms),
        "marches_communs": marches, "marches_total": len(preuves),
        "avertissement": avertissement,
        "historique": _mettre_en_forme(_regrouper(lignes), noms)}


# --------------------------------------------------------------------------- fiche d'une société

def fiche_societe(calc: sqlite3.Connection, ret: sqlite3.Connection, cle: str) -> dict | None:
    """Une société et ses partenaires de présence : les paires qui ont un niveau ou un avis, triées par excès."""
    if not _prets(calc):
        return None
    rg = _liens.reglages_groupes()
    s = calc.execute("SELECT * FROM groupes_societes WHERE calcul = ? AND cle = ?", (CALCUL, cle)).fetchone()
    if s is None:
        connue = next((x for x in _charger_annuaire(calc) if x["cle"] == cle), None)
        if connue is None:
            return None
        s = {"cle": cle, "nom": connue["nom"], "marches": 0, "fort": 0, "moyen": 0, "faible": 0, "groupes": 0}
    else:
        s = dict(s)

    hist = _lignes_avis(ret, cle=cle)
    par_paire = defaultdict(list)
    for x in hist:
        par_paire[(x["cle_a"], x["cle_b"])].append(x)
    avec_avis = set(par_paire)

    examinees = calc.execute("SELECT (SELECT COUNT(*) FROM groupes_liens WHERE calcul = ? AND cle_a = ?)"
                             " + (SELECT COUNT(*) FROM groupes_liens WHERE calcul = ? AND cle_b = ?)",
                             (CALCUL, cle, CALCUL, cle)).fetchone()[0]
    lignes = {}
    for col in ("cle_a", "cle_b"):
        for r in calc.execute(f"SELECT * FROM groupes_liens WHERE calcul = ? AND {col} = ?"
                              f" AND (niveau IS NOT NULL OR origine LIKE 'avis%')", (CALCUL, cle)):
            lignes[(r["cle_a"], r["cle_b"])] = r
    for a, b in avec_avis - set(lignes):           # un avis donné depuis le dernier calcul
        r = calc.execute("SELECT * FROM groupes_liens WHERE calcul = ? AND cle_a = ? AND cle_b = ?",
                         (CALCUL, a, b)).fetchone()
        lignes[(a, b)] = r

    # Les 30 places vont d'abord aux paires avec un avis, puis aux niveaux les plus forts : trié par excès seul,
    # un lien faible sur 3 marchés (attendu minuscule, excès énorme) chasserait les liens forts. Affichage par excès.
    def priorite(kv):
        r = kv[1]
        return (kv[0] not in avec_avis, -_liens.RANG.get(r["niveau"] if r else None, 0),
                -(r["exces"] if r else 0), kv[0])
    choisies = sorted(sorted(lignes.items(), key=priorite)[:rg["partenaires"]],
                      key=lambda kv: (-(kv[1]["exces"] if kv[1] else 0), kv[0]))
    autres = {b if a == cle else a for (a, b), _ in choisies}
    noms = _noms(calc, autres | {cle})
    noms.setdefault(cle, s["nom"])
    marches_de = {r["cle"]: r["marches"] for r in calc.execute(
        f"SELECT cle, marches FROM groupes_societes WHERE calcul = ? AND cle IN ({_marques(len(autres))})",
        (CALCUL, *autres))} if autres else {}

    partenaires = []
    gagnants_cles = set()
    preuves_de = {}
    for (a, b), r in choisies:
        preuves_de[(a, b)] = calc.execute(
            "SELECT ref, date, acheteur, gagnants FROM lien_preuves WHERE calcul = ? AND cle_a = ? AND cle_b = ?"
            " ORDER BY date DESC", (CALCUL, a, b)).fetchall()
        gagnants_cles.update(g for p in preuves_de[(a, b)] for g in (p["gagnants"] or "").split(",") if g)
    noms_gagnants = _noms(calc, gagnants_cles)
    for (a, b), r in choisies:
        autre = b if a == cle else a
        moi_a = a == cle
        p = _paire_sortie(r, a, b, noms, par_paire.get((a, b), []))
        part_soc = (r["part_a"] if moi_a else r["part_b"]) if r else None       # mes marchés où l'autre est là
        part_aut = (r["part_b"] if moi_a else r["part_a"]) if r else None       # ses marchés où je suis là
        seuil = rg["accompagne"]
        partenaires.append({
            "partenaire": {"cle": autre, "nom": noms.get(autre, autre), "marches": marches_de.get(autre)},
            "cle_a": a, "cle_b": b,
            "communs": p["communs"], "part_societe": part_soc, "part_partenaire": part_aut,
            "exces": p["exces"], "attendu": p["attendu"], "p": p["p"], "niveau": p["niveau"],
            "mutuel": p["mutuel"], "origine": p["origine"],
            "deux_sens": part_soc is not None and min(part_soc, part_aut) >= seuil,
            "accompagne_societe": part_aut is not None and part_aut >= seuil,     # l'autre suit la société
            "accompagne_partenaire": part_soc is not None and part_soc >= seuil,  # la société suit l'autre
            "ensemble": {
                "unites": r["unites"] if r else None,
                "gains_societe": (r["gains_a"] if moi_a else r["gains_b"]) if r else None,
                "gains_partenaire": (r["gains_b"] if moi_a else r["gains_a"]) if r else None,
                "gains_autres": r["gains_autres"] if r else None,
                "taux_victoire": r["taux_victoire"] if r else None,
                "ecart_meilleure": r["ecart_meilleure"] if r else None,
                "ecart_reference": r["ecart_reference"] if r else None},
            "preuves": [{"ref": x["ref"], "date": date_iso(x["date"]), "acheteur": x["acheteur"],
                         "gagnants": [_personne(g, noms_gagnants) for g in (x["gagnants"] or "").split(",") if g]}
                        for x in preuves_de[(a, b)]],
            "avis": p["avis"], "nb_avis": p["nb_avis"]})

    groupes_soc = calc.execute(
        "SELECT g.id, g.score, g.niveau, g.taille, g.marches_3, fc.famille FROM groupe_membres m"
        " JOIN groupes g ON g.calcul = m.calcul AND g.id = m.groupe"
        " LEFT JOIN famille_cliques fc ON fc.calcul = m.calcul AND fc.groupe = m.groupe"
        " WHERE m.calcul = ? AND m.cle = ? ORDER BY g.score DESC, g.id LIMIT 50", (CALCUL, cle)).fetchall()
    assez = s["marches"] >= rg["actives"]
    return {
        "societe": {k: s[k] for k in ("cle", "nom", "marches", "fort", "moyen", "faible", "groupes")},
        "assez_de_marches": assez, "seuil_actives": rg["actives"],
        "message": None if assez else (f"Pas assez de marchés pour conclure : {s['marches']} marché(s), il en faut "
                                       f"au moins {rg['actives']}. Les partenaires éventuels sont affichés à titre "
                                       f"indicatif."),
        "partenaires": partenaires, "paires_examinees": examinees,
        "partenaires_avec_niveau": s["fort"] + s["moyen"] + s["faible"],
        "groupes": [{"id": g["id"], "score": g["score"], "niveau": g["niveau"], "taille": g["taille"],
                     "marches": g["marches_3"], "famille": g["famille"]} for g in groupes_soc],
        "question": QUESTION, "reglages": reglages_page(),
        "historique": _mettre_en_forme(_regrouper(hist), _noms(calc, {c for x in hist for c in (x["cle_a"], x["cle_b"])}))}


# --------------------------------------------------------------------------- avis (écriture)

def enregistrer_avis(calc: sqlite3.Connection, ret: sqlite3.Connection, auteur: str, reponse: str,
                     commentaire: str = "", groupe: int | None = None,
                     cle_a: str | None = None, cle_b: str | None = None, ajout: bool = False) -> dict:
    """Écrit un avis (une paire) ou le même avis pour toutes les paires d'un groupe.

    L'auteur vient de la session, jamais de la requête. Une paire peut réunir deux sociétés quelconques
    du calcul (« Ajouter une société liée » : `ajout`, réponse oui et commentaire obligatoires) ; une paire
    qui ne se trouve pas dans le groupe indiqué est refusée.
    """
    if reponse not in REPONSES:
        raise ErreurAvis("Réponse inconnue : oui, non ou nsp.")
    commentaire = (commentaire or "").strip()
    if len(commentaire) > MAX_COMMENTAIRE:
        raise ErreurAvis(f"Commentaire trop long ({MAX_COMMENTAIRE} caractères au plus).")
    if ajout and (reponse != "oui" or not commentaire):
        raise ErreurAvis("Pour ajouter une société liée, dites d'où vient l'information dans le commentaire.")
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
        connues = {r[0] for r in calc.execute("SELECT cle FROM groupes_societes WHERE calcul = ? AND cle IN (?, ?)",
                                              (CALCUL, a, b))}
        if a not in connues or b not in connues:
            raise ErreurAvis("Société inconnue : elle n'a aucun marché dans l'historique.", 404)
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
        if ajout:
            raise ErreurAvis("Pour ajouter une société liée, choisissez deux sociétés.")
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
