"""Acheteurs : leur type, et le rapprochement quand l'AO nomme un acheteur que l'historique ne connaît pas.

La clé d'un acheteur est `bases.cle_acheteur()` (la même que serveur.py). Un AO dont la clé n'existe
pas dans l'historique n'a, tel quel, aucun marché « du même acheteur » : on cherche alors, parmi les
acheteurs de l'historique, ceux dont le nom est proche — en ignorant les mots génériques (DIRECTEUR,
PROVINCIAL, DE, LA…) — et on range les propositions dans copilote_retours.db, table
`correspondances_acheteurs`, qu'un humain peut corriger (statuts proposee / validee / refusee).

Règle d'emploi dans le score :
- une correspondance « validee » compte toujours ; « refusee » jamais ;
- une « proposee » compte seulement si son score atteint `seuil_auto` (95 : à 90, « Commune SAADLA » se rapprochait
  de « Commune SAADA » au backtest 2026, deux communes différentes) ET qu'elle est sans ambiguïté
  (aucune autre proposition à moins de `ecart_ambigu` points) ; la réponse de l'API le dit.

Ligne de commande :  python -m copilote.acheteurs proposer | mesurer | evaluer | lister [statut]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime

from rapidfuzz import fuzz, process

from .bases import cle_acheteur
from .nature import plat

SCHEMA_CORRESPONDANCES = """
CREATE TABLE IF NOT EXISTS correspondances_acheteurs (
  cle_ao TEXT NOT NULL,                 -- clé de l'acheteur tel que l'annonce l'écrit
  nom_ao TEXT,
  cle_historique TEXT NOT NULL,         -- clé d'un acheteur de pv_travaux.db
  nom_historique TEXT,
  score REAL,                           -- 0 à 100 ; vide pour une saisie à la main
  statut TEXT NOT NULL DEFAULT 'proposee' CHECK (statut IN ('proposee', 'validee', 'refusee')),
  origine TEXT NOT NULL DEFAULT 'auto', -- auto | main
  propose_le TEXT,
  decide_par TEXT,
  decide_le TEXT,
  commentaire TEXT,
  PRIMARY KEY (cle_ao, cle_historique));
"""

# Mots qui ne distinguent pas un acheteur d'un autre : articles, titres, formes administratives.
GENERIQUES = frozenset("""
    de du des la le les l d et en a au aux pour sur dans par
    directeur direction provincial provinciale regional regionale delegue delegation chef gouverneur
    ministere province prefecture region commune rurale urbaine territoriale ct c t collectivite
    collectivites public publique royaume maroc
""".split())

# Types, du plus précis au plus général : le premier dont un mot figure dans le nom l'emporte.
TYPES: list[tuple[str, tuple[str, ...]]] = [
    ("societe", ("societe", "omrane", "alomrane", "sa", "sarl", "multiservices", "amenagement")),
    ("agence", ("agence", "office", "anep", "etablissement", "caisse", "centre national")),
    ("enseignement", ("universite", "faculte", "institut", "ecole", "academie", "aref", "lycee", "cmc", "cite des metiers")),
    ("direction", ("directeur", "direction", "delegue", "delegation", "ministere", "inspecteur", "inspection",
                   "secretariat", "haut commissariat", "gestionnaire", "ordonnateur", "tresorier", "receveur")),
    ("commune", ("commune", "collectivite", "arrondissement", "c t", "ct", "conseil communal", "mairie")),
    ("region", ("region", "conseil regional")),
    ("province", ("gouverneur", "province", "prefecture", "wilaya", "pachalik", "cercle", "caidat", "conseil provincial",
                  "conseil prefectoral")),
]


def type_acheteur(nom: str | None) -> str:
    """commune / region / province / direction / agence / societe / enseignement / autre."""
    mots = f" {plat(nom)} "
    for type_, indices in TYPES:
        if any(f" {i} " in mots for i in indices):
            return type_
    return "autre"


def variante(nom: str | None) -> str | None:
    """Ce qui distingue deux entités qui portent le même nom de lieu : une commune RURALE n'est pas la
    commune URBAINE du même nom, une direction PROVINCIALE n'est pas la direction RÉGIONALE. None si le
    nom ne le dit pas (alors le rapprochement reste permis)."""
    mots = set(plat(nom).split())
    if mots & {"rurale", "rural"}:
        return "rurale"
    if mots & {"urbaine", "urbain"}:
        return "urbaine"
    if type_acheteur(nom) == "direction":
        if mots & {"provincial", "provinciale"}:
            return "provinciale"
        if mots & {"regional", "regionale"}:
            return "regionale"
    return None


def noyau(nom: str | None) -> str:
    """Les mots qui distinguent l'acheteur, dans l'ordre : « DIRECTEUR PROVINCIAL DE L'EQUIPEMENT TAZA » → « equipement taza »."""
    mots = plat(nom).split()
    gardes = [m for m in mots if m not in GENERIQUES]
    return " ".join(gardes or mots)


def noms_historique(db: sqlite3.Connection, avant: str | None = None) -> list[dict]:
    """Un acheteur par clé : l'écriture la plus fréquente, et son nombre de marchés.

    `avant` (AAAAMMJJ) ne garde que les marchés datés strictement avant ce jour : sert à mesurer, sur
    2026, ce que le rapprochement aurait trouvé en ne connaissant que le passé.
    """
    par_cle: dict[str, dict] = {}
    filtre, args = (" AND tri_date < ? AND tri_date <> ''", (avant,)) if avant else ("", ())
    for r in db.execute("SELECT acheteur, COUNT(*) AS n FROM marches WHERE principale = 1"
                        " AND acheteur IS NOT NULL AND acheteur <> ''" + filtre + " GROUP BY acheteur", args):
        cle = cle_acheteur(r["acheteur"])
        if not cle:
            continue
        e = par_cle.setdefault(cle, {"cle": cle, "nom": r["acheteur"], "marches": 0, "_n": 0})
        e["marches"] += r["n"]
        if r["n"] > e["_n"]:
            e["nom"], e["_n"] = r["acheteur"], r["n"]
    for e in par_cle.values():
        e["type"] = type_acheteur(e["nom"])
        e["noyau"] = noyau(e["nom"])
        e["variante"] = variante(e["nom"])
        del e["_n"]
    return list(par_cle.values())


PLAFOND_MOTS_DIFFERENTS = 85.0


def mots_sans_equivalent(a: str, b: str, seuil: float = 80.0) -> list[str]:
    """Les mots de a ou de b qui n'ont aucun mot proche (ratio ≥ seuil) de l'autre côté."""
    ma, mb = a.split(), b.split()
    seuls = [m for m in ma if not any(fuzz.ratio(m, x) >= seuil for x in mb)]
    seuls += [m for m in mb if not any(fuzz.ratio(m, x) >= seuil for x in ma)]
    return seuls


def proposer(nom_ao: str, connus: list[dict], n: int = 5, seuil: float = 80.0,
             exclure_cle: str | None = None) -> list[dict]:
    """Les acheteurs de l'historique les plus proches du nom de l'AO, du plus au moins proche.

    La comparaison porte sur les noyaux (mots génériques ôtés, ordre conservé : mesuré meilleur que le tri des mots) ; deux acheteurs de types
    différents (une commune et une préfecture du même nom) ne se rapprochent pas, sauf si l'un des
    deux est de type « autre » ; ni une commune rurale et une commune urbaine, ni une direction
    provinciale et une direction régionale (voir variante()).
    """
    cible = noyau(nom_ao)
    type_ao = type_acheteur(nom_ao)
    if not cible:
        return []
    var_ao = variante(nom_ao)
    candidats = [c for c in connus if c["cle"] != exclure_cle
                 and (type_ao == "autre" or c["type"] == "autre" or c["type"] == type_ao)
                 and not (var_ao and c["variante"] and c["variante"] != var_ao)]
    trouves = process.extract(cible, [c["noyau"] for c in candidats], scorer=fuzz.ratio,
                              score_cutoff=seuil, limit=n)
    sortie = []
    for noyau_c, score, i in trouves:
        # Un mot qui n'a pas d'équivalent de l'autre côté (TAZA contre TANTAN) est un autre lieu, pas une
        # faute de frappe : la proposition reste visible mais ne dépasse jamais PLAFOND_MOTS_DIFFERENTS.
        differents = mots_sans_equivalent(cible, noyau_c)
        if differents:
            score = min(score, PLAFOND_MOTS_DIFFERENTS)
        sortie.append({"cle": candidats[i]["cle"], "nom": candidats[i]["nom"], "marches": candidats[i]["marches"],
                       "score": round(score, 1), "mots_differents": differents})
    return sortie


# ------------------------------------------------------------------ table retours
def preparer(ret: sqlite3.Connection) -> None:
    ret.executescript(SCHEMA_CORRESPONDANCES)
    ret.commit()


def _maintenant() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def enregistrer_propositions(ret: sqlite3.Connection, cle_ao: str, nom_ao: str, propositions: list[dict]) -> int:
    """Range les propositions sans jamais toucher à une ligne existante (une décision humaine reste)."""
    preparer(ret)
    ajoutees = 0
    for p in propositions:
        cur = ret.execute(
            "INSERT OR IGNORE INTO correspondances_acheteurs (cle_ao, nom_ao, cle_historique, nom_historique,"
            " score, statut, origine, propose_le) VALUES (?,?,?,?,?, 'proposee', 'auto', ?)",
            (cle_ao, nom_ao, p["cle"], p["nom"], p["score"], _maintenant()))
        ajoutees += cur.rowcount
    ret.commit()
    return ajoutees


def decider(ret: sqlite3.Connection, cle_ao: str, cle_historique: str, statut: str, auteur: str,
            commentaire: str = "", nom_ao: str | None = None, nom_historique: str | None = None) -> None:
    """Valide ou refuse une correspondance (la crée si elle est saisie à la main)."""
    if statut not in ("proposee", "validee", "refusee"):
        raise ValueError("statut inconnu")
    preparer(ret)
    existe = ret.execute("SELECT 1 FROM correspondances_acheteurs WHERE cle_ao = ? AND cle_historique = ?",
                         (cle_ao, cle_historique)).fetchone()
    if existe:
        ret.execute("UPDATE correspondances_acheteurs SET statut = ?, decide_par = ?, decide_le = ?,"
                    " commentaire = ? WHERE cle_ao = ? AND cle_historique = ?",
                    (statut, auteur, _maintenant(), commentaire, cle_ao, cle_historique))
    else:
        ret.execute("INSERT INTO correspondances_acheteurs (cle_ao, nom_ao, cle_historique, nom_historique, score,"
                    " statut, origine, propose_le, decide_par, decide_le, commentaire)"
                    " VALUES (?,?,?,?,NULL,?, 'main', ?, ?, ?, ?)",
                    (cle_ao, nom_ao, cle_historique, nom_historique, statut, _maintenant(), auteur,
                     _maintenant(), commentaire))
    ret.commit()


def lister(ret: sqlite3.Connection, statut: str | None = None, cle_ao: str | None = None) -> list[dict]:
    preparer(ret)
    sql, args = "SELECT * FROM correspondances_acheteurs", []
    filtres = []
    if statut:
        filtres.append("statut = ?"); args.append(statut)
    if cle_ao:
        filtres.append("cle_ao = ?"); args.append(cle_ao)
    if filtres:
        sql += " WHERE " + " AND ".join(filtres)
    sql += " ORDER BY cle_ao, score DESC"
    return [dict(r) for r in ret.execute(sql, args)]


def rapprocher(cle_ao: str, nom_ao: str, connus: list[dict], ret: sqlite3.Connection | None,
               seuil_auto: float = 95.0, ecart_ambigu: float = 3.0, enregistrer: bool = True) -> dict:
    """Comment l'acheteur d'un AO se relie à l'historique.

    Renvoie {"statut": "direct" | "correspondance" | "aucun", "cles": [clés utilisables], "propositions": [...]}.
    `direct` : la clé existe telle quelle. `correspondance` : au moins une correspondance validée ou
    proposée assez sûre. `aucun` : rien d'utilisable (les propositions restent visibles, à valider).
    """
    cles_connues = {c["cle"] for c in connus}
    if cle_ao in cles_connues:
        return {"statut": "direct", "cles": [cle_ao], "propositions": []}
    propositions = proposer(nom_ao, connus, exclure_cle=cle_ao)
    if ret is None:
        etat = {p["cle"]: "proposee" for p in propositions}
    else:
        preparer(ret)
        if enregistrer and propositions:
            enregistrer_propositions(ret, cle_ao, nom_ao, propositions)
        etat = {r["cle_historique"]: r["statut"] for r in lister(ret, cle_ao=cle_ao)}
    noms = {c["cle"]: c for c in connus}
    scores = {p["cle"]: p["score"] for p in propositions}
    lignes = []
    for cle, statut in etat.items():
        if cle in noms:
            lignes.append({"cle": cle, "nom": noms[cle]["nom"], "marches": noms[cle]["marches"],
                           "score": scores.get(cle), "statut": statut})
    lignes.sort(key=lambda x: (x["statut"] != "validee", -(x["score"] or 0)))
    cles = [x["cle"] for x in lignes if x["statut"] == "validee"]
    # Une proposition non validée sert seulement si elle est sûre ET seule de son niveau : deux
    # propositions presque aussi bonnes, on n'en choisit pas une au hasard.
    ouvertes = [x for x in lignes if x["statut"] == "proposee"]
    if not cles and ouvertes and (ouvertes[0]["score"] or 0) >= seuil_auto:
        suivant = ouvertes[1]["score"] if len(ouvertes) > 1 else None
        if suivant is None or ouvertes[0]["score"] - suivant >= ecart_ambigu:
            cles.append(ouvertes[0]["cle"])
    return {"statut": "correspondance" if cles else "aucun", "cles": cles, "propositions": lignes}


# ------------------------------------------------------------------ mesure
def mesurer_ao_ouverts(ao: sqlite3.Connection, hist: sqlite3.Connection, ret: sqlite3.Connection | None) -> dict:
    """Combien d'AO ouverts n'ont pas d'acheteur reconnu (sans rien écrire)."""
    connus = noms_historique(hist)
    lignes = ao.execute("SELECT ref_annonce, acheteur, cle_acheteur FROM appels_ouverts"
                        " WHERE ref_annonce NOT LIKE 'REJEU-%'").fetchall()
    sortie = {"ao": len(lignes), "direct": 0, "correspondance": 0, "aucun": 0, "sans_acheteur": 0, "exemples_aucun": []}
    for r in lignes:
        nom = r["acheteur"] or ""
        cle = r["cle_acheteur"] or cle_acheteur(nom)
        if not cle:
            sortie["sans_acheteur"] += 1
            continue
        res = rapprocher(cle, nom, connus, ret, enregistrer=False)
        sortie[res["statut"]] += 1
        if res["statut"] == "aucun" and len(sortie["exemples_aucun"]) < 10:
            sortie["exemples_aucun"].append(nom)
    return sortie


def mesurer_sur_historique(hist: sqlite3.Connection, annee: str = "2026") -> dict:
    """Ce que le rapprochement donnerait pour les acheteurs de l'année `annee` s'ils étaient des AO :
    on ne connaît que les marchés d'avant l'année. Un marché dont l'acheteur n'existe pas encore sous sa
    clé est un cas « sans acheteur reconnu » ; on compte ensuite ceux que le rapprochement retrouve.
    Sans rien écrire.
    """
    connus = noms_historique(hist, avant=f"{annee}0101")
    cles = {c["cle"] for c in connus}
    marches = hist.execute("SELECT acheteur FROM marches WHERE principale = 1 AND tri_date >= ? AND tri_date < ?"
                           " AND acheteur IS NOT NULL AND acheteur <> ''", (f"{annee}0101", f"{int(annee) + 1}0101")).fetchall()
    sortie = {"marches": len(marches), "direct": 0, "correspondance": 0, "aucun_avec_proposition": 0,
              "aucun_sans_proposition": 0, "acheteurs_inconnus": 0, "exemples": []}
    memo: dict[str, dict] = {}
    for (nom,) in marches:
        cle = cle_acheteur(nom)
        if cle in cles:
            sortie["direct"] += 1
            continue
        if cle not in memo:
            memo[cle] = rapprocher(cle, nom, connus, None)
            sortie["acheteurs_inconnus"] += 1
            if len(sortie["exemples"]) < 400:
                p = memo[cle]["propositions"]
                sortie["exemples"].append({"nom": nom, "statut": memo[cle]["statut"],
                                           "meilleure": (p[0]["nom"], p[0]["score"]) if p else None})
        res = memo[cle]
        if res["statut"] == "correspondance":
            sortie["correspondance"] += 1
        elif res["propositions"]:
            sortie["aucun_avec_proposition"] += 1
        else:
            sortie["aucun_sans_proposition"] += 1
    return sortie


def evaluer_variantes(connus: list[dict], n: int = 300, graine: int = 5) -> dict:
    """Retrouve-t-on l'acheteur d'origine quand son nom est écrit autrement ? Auto-test sur `n` acheteurs.

    Trois écritures : « directeur »/« direction » échangés ; articles et mots-outils supprimés ; deux
    lettres remplacées. `top1` = le bon acheteur est la première proposition ; `auto` = le rapprochement
    l'utilise sans validation ; `auto_juste` = c'est le bon.
    """
    import random
    hasard = random.Random(graine)
    echantillon = hasard.sample(connus, min(n, len(connus)))

    def echange(nom):
        t = plat(nom).replace("directeur provincial", "direction provinciale").replace("directeur regional", "direction regionale")
        return t if t != plat(nom) else None

    def sans_outils(nom):
        mots = [m for m in plat(nom).split() if m not in ("de", "du", "des", "la", "le", "l", "d")]
        return " ".join(mots) if len(mots) > 1 else None

    def fautes(nom):
        lettres = list(plat(nom))
        if len(lettres) < 8:
            return None
        for _ in range(2):
            lettres[hasard.randrange(len(lettres))] = hasard.choice("abcdefghilmnorstu")
        return "".join(lettres)

    sortie = {}
    for nom_v, f in (("echange_directeur_direction", echange), ("sans_mots_outils", sans_outils), ("deux_fautes", fautes)):
        e = {"n": 0, "top1": 0, "top5": 0, "auto": 0, "auto_juste": 0}
        for c in echantillon:
            v = f(c["nom"])
            if not v or cle_acheteur(v) == c["cle"]:
                continue
            e["n"] += 1
            propositions = proposer(v, connus)
            e["top1"] += bool(propositions) and propositions[0]["cle"] == c["cle"]
            e["top5"] += any(p["cle"] == c["cle"] for p in propositions)
            r = rapprocher(cle_acheteur(v), v, connus, None)
            if r["statut"] == "correspondance":
                e["auto"] += 1
                e["auto_juste"] += c["cle"] in r["cles"]
        sortie[nom_v] = e
    return sortie


def main(argv: list[str]) -> int:
    from . import bases
    action = argv[0] if argv else "mesurer"
    if action == "lister":
        ret = bases.retours()
        for r in lister(ret, argv[1] if len(argv) > 1 else None):
            print(f"{r['statut']:8} {r['score'] or '':>5}  {r['nom_ao']}  ->  {r['nom_historique']}")
        return 0
    if action == "evaluer":
        hist = bases.historique()
        print(json.dumps({"variantes": evaluer_variantes(noms_historique(hist)),
                          "annee_2026": {k: v for k, v in mesurer_sur_historique(hist).items() if k != "exemples"}},
                         ensure_ascii=False, indent=1))
        return 0
    hist, ao, ret = bases.historique(), bases.ao_ouverts(), bases.retours()
    if action == "proposer":
        connus = noms_historique(hist)
        n = 0
        for r in ao.execute("SELECT acheteur, cle_acheteur FROM appels_ouverts WHERE ref_annonce NOT LIKE 'REJEU-%'"):
            res = rapprocher(r["cle_acheteur"] or cle_acheteur(r["acheteur"]), r["acheteur"] or "", connus, ret)
            n += res["statut"] != "direct"
        print(f"{n} acheteur(s) d'AO sans clé connue ; propositions rangées dans correspondances_acheteurs.")
        return 0
    print(mesurer_ao_ouverts(ao, hist, ret))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
