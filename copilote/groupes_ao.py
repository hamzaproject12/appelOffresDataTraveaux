"""Les sociétés probables d'un AO réparties en groupes de présence commune, et la stratégie passée de
chaque groupe.

Pour chaque société probable (p ≥ seuil_allies), le calcul « autour d'une société » (autour_societe.py,
les règles de la page des groupes : noyau, accompagnatrice, concurrent direct) est refait sur les seules
unités d'avant la coupure de l'AO, totaux compris. Ses alliées sont les noyaux et accompagnatrices de son
groupe de 9 au plus, retenues si elles sont dans la liste des probables, ou si elles sont « noyau » ou
accompagnatrice avec C ≥ 60 % (ajoutées alors « probables par association », p = B × p(société)).
Les ajouts par association sont désactivés depuis le 03/11/2026 (reglages.json, probables.association = false) :
seules les alliées de la liste comptent alors, dans les groupes comme dans le nombre de concurrents.

Version A2 — groupes EN ÉTOILE (`former`) : un groupe = un chef de file + ses alliées directes, sans
chaînage (A→B→C ne met plus C avec A).

    1. score de chef = p(venir) × taux de victoire lissé sur les 300 marchés les plus similaires,
       (victoires + 0,25) / (présences + 2) ;
    2. on parcourt les sociétés par score décroissant : une société qui a des alliées devient chef de
       file, sauf si elle est l'alliée d'un chef déjà retenu ET qu'elle a moins de 2 alliées directes
       dans la liste de l'AO (elle est alors simple accompagnatrice). Deux chefs alliés restent chacun à
       la tête de son groupe : « travaille aussi avec X » ;
    3. une accompagnatrice de plusieurs chefs va sous le chef le plus probable ; les autres groupes la
       rappellent : « accompagne aussi X ».

Version A (`former_union`, gardée pour les mesures avant / après) :

    a) l'alliée est déjà dans la liste des probables : le lien compte tel quel ;
    b) elle n'y est pas mais elle est « noyau », ou accompagnatrice avec C ≥ 60 % : elle est ajoutée
       « probable par association », avec p = B × p(société), B = part des unités de la société où
       l'alliée était présente (P(alliée présente | la société soumissionne)) ;
    c) les liens sont fusionnés par union-find, du plus fort au plus faible (victoires accompagnées,
       puis A ÷ B) : une société n'est que dans un groupe, celui de son lien le plus fort ; une fusion
       qui dépasserait `taille_groupe` sociétés est refusée. Chef de file : le membre qui a le plus de
       victoires où au moins un autre membre était présent ;
    d) les sociétés de la liste sans alliée restent « individuelles ».

Stratégie d'un groupe : sur les unités d'avant la coupure où au moins deux membres ont déposé, qui a
gagné, le taux de victoire du groupe, la position médiane de chaque membre par rapport à l'offre
gagnante et au prix de référence, et les défaites (groupes.issues : gagnant extérieur, son taux, son
écart avec la meilleure offre du groupe). La table groupes_defaites ne sert pas ici : elle est rangée
par groupe du calcul hebdomadaire et ne connaît pas la coupure de l'AO.

Vocabulaire : « groupe de présence commune », « lecture ». Un fait statistique, pas une accusation.
"""
from __future__ import annotations

import statistics
from collections import Counter

from . import autour_societe, groupes
from .probables import Memoire, eviction

MAX_DERNIERES = 12


def autour(mem: Memoire, n: int, cle: str, taille_groupe: int = 9) -> dict:
    """autour_societe.calculer sur les unités visibles (0..n-1), totaux de la même période."""
    unites = [mem.unites[i] for i in Memoire.avant(mem.par_societe.get(cle, []), n)]
    d = autour_societe.donnees_unites(
        cle, unites, lambda c: Memoire.compte(mem.par_societe.get(c, []), n),
        lambda c: Memoire.compte(mem.victoires.get(c, []), n), mem.nom)
    return autour_societe.calculer(d, {"taille_groupe": taille_groupe})


class _Ensembles:
    def __init__(self):
        self.parent: dict[str, str] = {}
        self.taille: dict[str, int] = {}

    def trouver(self, a: str) -> str:
        self.parent.setdefault(a, a)
        self.taille.setdefault(a, 1)
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def unir(self, a: str, b: str, maximum: int) -> bool:
        ra, rb = self.trouver(a), self.trouver(b)
        if ra == rb or self.taille[ra] + self.taille[rb] > maximum:
            return False
        if (self.taille[ra], rb) < (self.taille[rb], ra):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.taille[ra] += self.taille[rb]
        return True


def _force(arete: tuple) -> tuple:
    x, y, l = arete
    return (-l["victoires_x_avec"], -(l["rapport"] or 0), x, y)


def _resume_lien(x: str, y: str, l: dict) -> dict:
    return {"de": x, "vers": y, "lecture": l["lecture"], "victoires_avec": l["victoires_x_avec"],
            "communes": l["communes"], "A": l["A"], "B": l["B"], "C": l["C"], "rapport": l["rapport"],
            "part_x_sur_y": l["part_x_sur_y"]}


def former_union(mem: Memoire, n: int, tous: list[dict], r: dict) -> dict:
    """Version A : groupes par union-find. Groupes et individuelles. Modifie les candidats de `tous` (association, p) ; renvoie aussi les
    sociétés ajoutées par association qui n'étaient pas candidates (« ajoutees »)."""
    par_cle = {c["cle"]: c for c in tous}
    liste = {c["cle"] for c in tous if c["dans_liste"]}
    aretes: list[tuple] = []
    association: dict[str, tuple] = {}             # cle -> (p, via, ligne)
    alliees_de: dict[str, int] = {}
    for x in [c for c in tous if c["p"] >= r["seuil_allies"]]:
        res = autour(mem, n, x["cle"], int(r["taille_groupe"]))
        lignes = {l["cle"]: l for l in res["lignes"]}
        alliees_de[x["cle"]] = len(res["groupe"]) - 1
        for y in res["groupe"][1:]:
            l = lignes[y]
            if y in liste:
                aretes.append((x["cle"], y, l))
            elif r.get("association", True) and (
                    l["lecture"] == "noyau" or (l["C"] is not None and l["C"] >= r["c_association"])):
                p = (l["B"] or 0.0) * x["p"]
                if y not in association or p > association[y][0]:
                    association[y] = (p, x["cle"], l)
                aretes.append((x["cle"], y, l))

    ens = _Ensembles()
    for x, y, _ in sorted(aretes, key=_force):
        ens.unir(x, y, int(r["taille_groupe"]))
    composantes: dict[str, list[str]] = {}
    for cle in ens.parent:
        composantes.setdefault(ens.trouver(cle), []).append(cle)
    dans_groupe = {c for membres in composantes.values() if len(membres) >= 2 for c in membres}

    # Les sociétés par association qui ont trouvé leur groupe : marquées, ou ajoutées.
    ajoutees = []
    for y, (p, via, l) in association.items():
        if y not in dans_groupe:
            continue
        info = {"via": via, "nom_via": mem.nom(via), "p": p, "B": l["B"], "C": l["C"], "lecture": l["lecture"],
                "p_via": par_cle[via]["p"]}
        c = par_cle.get(y)
        if c is None:
            c = {"cle": y, "nom": mem.nom(y), "p": p, "p_formule": None, "x": None, "brut": None,
                 "contributions": None, "eviction": eviction(mem, y, n), "dans_liste": False}
            par_cle[y] = c
            ajoutees.append(c)
        c["association"] = info
        c["p"] = max(c["p"], p)

    meilleur_lien: dict[tuple, dict] = {}
    for x, y, l in sorted(aretes, key=_force):
        cle = tuple(sorted((x, y)))
        if cle not in meilleur_lien and ens.trouver(x) == ens.trouver(y):
            meilleur_lien[cle] = _resume_lien(x, y, l)

    sortie = []
    for membres in composantes.values():
        if len(membres) < 2:
            continue
        ens_m = set(membres)
        accompagnees = {m: sum(1 for i in Memoire.avant(mem.victoires.get(m, []), n)
                               if len(mem.presents[i] & ens_m) >= 2) for m in membres}
        chef = min(membres, key=lambda m: (-accompagnees[m], -par_cle[m]["p"], m))
        ordre = sorted(membres, key=lambda m: (m != chef, -par_cle[m]["p"], m))
        sortie.append({
            "chef": chef, "membres": ordre,
            "victoires_accompagnees": accompagnees,
            "liens": [v for k, v in meilleur_lien.items() if k[0] in ens_m],
            "strategie": strategie(mem, n, ordre),
            "p_moyenne": sum(par_cle[m]["p"] for m in membres) / len(membres)})
    sortie.sort(key=lambda g: (-sum(par_cle[m]["p"] for m in g["membres"]), g["chef"]))
    for k, g in enumerate(sortie, 1):
        g["numero"] = k
    individuelles = [c["cle"] for c in tous if c["dans_liste"] and c["cle"] not in dans_groupe]
    return {"groupes": sortie, "individuelles": individuelles, "ajoutees": ajoutees,
            "alliees_cherchees": alliees_de}


def _mediane(v: list[float]) -> float | None:
    return float(statistics.median(v)) if v else None


def _taux(u, cle: str) -> float | None:
    return next((c["taux"] for c in u.concurrents if c["cle"] == cle and c["taux"]), None)


def strategie(mem: Memoire, n: int, membres: list[str]) -> dict:
    """Ce que le groupe a fait sur les unités visibles où au moins deux membres ont déposé."""
    ens = set(membres)
    compte: Counter = Counter()
    for m in membres:
        for i in Memoire.avant(mem.par_societe.get(m, []), n):
            compte[i] += 1
    indices = sorted(i for i, k in compte.items() if k >= 2)
    unites = [mem.unites[i] for i in indices]
    noms = {m: mem.nom(m) for m in membres}
    resume, perdues = groupes.issues(unites, ens, mem, noms)

    par_membre = []
    for m in membres:
        vs_gagnant, vs_reference, presences = [], [], 0
        for u in unites:
            t = _taux(u, m)
            if not any(c["cle"] == m for c in u.concurrents):
                continue
            presences += 1
            if t is None:
                continue
            if u.cle_gagnant and u.cle_gagnant != m:
                tg = _taux(u, u.cle_gagnant)
                if tg:
                    vs_gagnant.append(t / tg - 1)
            tr = mem.taux_reference(u)
            if tr:
                vs_reference.append(t / tr - 1)
        par_membre.append({"cle": m, "presences": presences, "victoires": resume["gains"].get(m, 0),
                           "position_gagnant": _mediane(vs_gagnant), "n_gagnant": len(vs_gagnant),
                           "position_reference": _mediane(vs_reference), "n_reference": len(vs_reference)})

    dernieres = []
    for u in reversed(unites[-MAX_DERNIERES:]):
        presents = [m for m in membres if m in {c["cle"] for c in u.concurrents}]
        dernieres.append({"ref": u.ref, "lot": u.lot, "date": u.date, "acheteur": u.acheteur or "",
                          "gagnant": u.cle_gagnant, "nom_gagnant": next(
                              (c["nom"] for c in u.concurrents if c["cle"] == u.cle_gagnant), u.cle_gagnant),
                          "taux_gagnant": _taux(u, u.cle_gagnant) if u.cle_gagnant else None,
                          "membres_presents": presents, "offres": len({c["cle"] for c in u.concurrents})})
    return {
        "unites": len(unites), "jugees": resume["unites"], "gagnees": resume["gagnees"],
        "taux_victoire": resume["taux"], "gains": resume["gains"], "membres": par_membre,
        "defaites": resume["perdues"], "defaites_ecartes": resume["perdues_ecartes"],
        "exterieur": {
            "taux_median": _mediane([x["taux_gagnant"] for x in perdues if x["taux_gagnant"] is not None]),
            "ecart_meilleure": resume["ecart_meilleure"], "ecart_reference": resume["ecart_reference"],
            "gagnants": resume["gagnants_exterieurs"]},
        "dernieres": dernieres}


# --------------------------------------------------------------------------- version A2 : groupes en étoile

def taux_victoire_similaires(c: dict) -> float:
    """Victoires sur les marchés les plus similaires où elle est présente, lissées : (v + 0,25) / (n + 2)."""
    b = c.get("brut") or {}
    return (b.get("victoires_proches", 0) + 0.25) / (b.get("proches", 0) + 2)


def former(mem: Memoire, n: int, tous: list[dict], r: dict) -> dict:
    """Groupes en étoile (voir l'en-tête). Modifie les candidats de `tous` (association, p) ; renvoie aussi les
    sociétés ajoutées par association qui n'étaient pas candidates (« ajoutees »)."""
    par_cle = {c["cle"]: c for c in tous}
    liste = {c["cle"] for c in tous if c["dans_liste"]}
    taille = int(r["taille_groupe"])
    alliees: dict[str, list[tuple[str, dict]]] = {}        # x -> [(y, ligne « autour de x »)]
    alliees_de: dict[str, int] = {}
    for x in [c for c in tous if c["p"] >= r["seuil_allies"]]:
        res = autour(mem, n, x["cle"], taille)
        lignes = {l["cle"]: l for l in res["lignes"]}
        alliees_de[x["cle"]] = len(res["groupe"]) - 1
        retenues = []
        for y in res["groupe"][1:]:
            l = lignes[y]
            if y in liste or (r.get("association", True) and (
                    l["lecture"] == "noyau" or (l["C"] is not None and l["C"] >= r["c_association"]))):
                retenues.append((y, l))
        alliees[x["cle"]] = retenues

    score = {x: par_cle[x]["p"] * taux_victoire_similaires(par_cle[x]) for x in alliees}
    ordre = sorted(alliees, key=lambda x: (-score[x], -par_cle[x]["p"], x))
    chefs: list[str] = []
    for x in ordre:
        if not alliees[x]:
            continue
        directes = sum(1 for y, _ in alliees[x] if y in liste and y not in chefs)   # sans les chefs déjà retenus
        absorbee = any(x in {y for y, _ in alliees[c]} for c in chefs)
        if absorbee and directes < 2:
            continue
        chefs.append(x)
    est_chef = set(chefs)

    # Chaque accompagnatrice va sous le chef le plus probable qui la compte parmi ses alliées.
    proposee: dict[str, list[str]] = {}
    for c in chefs:
        for y, _ in alliees[c]:
            if y not in est_chef:
                proposee.setdefault(y, []).append(c)
    place = {y: min(cs, key=lambda c: (-par_cle[c]["p"], -score[c], c)) for y, cs in proposee.items()}

    # Les sociétés par association placées dans un groupe : marquées, ou ajoutées.
    ajoutees = []
    for y, c in place.items():
        if y in liste:
            continue
        l = dict(alliees[c])[y]
        p = (l["B"] or 0.0) * par_cle[c]["p"]
        info = {"via": c, "nom_via": mem.nom(c), "p": p, "B": l["B"], "C": l["C"], "lecture": l["lecture"],
                "p_via": par_cle[c]["p"]}
        cand = par_cle.get(y)
        if cand is None:
            cand = {"cle": y, "nom": mem.nom(y), "p": p, "p_formule": None, "x": None, "brut": None,
                    "contributions": None, "eviction": eviction(mem, y, n), "dans_liste": False}
            par_cle[y] = cand
            ajoutees.append(cand)
        cand["association"] = info
        cand["p"] = max(cand["p"], p)

    sortie = []
    for c in chefs:
        membres = [y for y, _ in alliees[c] if place.get(y) == c]
        if not membres:
            continue
        aussi = [{"cle": y, "chef": place[y]} for y, _ in alliees[c] if y in place and place[y] != c]
        avec = sorted({y for y, _ in alliees[c] if y in est_chef and y != c}
                      | {d for d in chefs if d != c and c in {y for y, _ in alliees[d]}})
        lignes = dict(alliees[c])
        ordre_m = [c] + sorted(membres, key=lambda m: (-par_cle[m]["p"], m))
        ens_m = set(ordre_m)
        accompagnees = {m: sum(1 for i in Memoire.avant(mem.victoires.get(m, []), n)
                               if len(mem.presents[i] & ens_m) >= 2) for m in ordre_m}
        b = par_cle[c].get("brut") or {}
        sortie.append({
            "chef": c, "membres": ordre_m, "aussi": aussi, "travaille_avec": avec,
            "score_chef": {"p": par_cle[c]["p"], "taux_victoire": taux_victoire_similaires(par_cle[c]),
                           "victoires": b.get("victoires_proches", 0), "presences": b.get("proches", 0),
                           "score": score[c]},
            "victoires_accompagnees": accompagnees,
            "liens": [_resume_lien(c, y, lignes[y]) for y in membres + [a["cle"] for a in aussi]],
            "strategie": strategie(mem, n, ordre_m),
            "p_moyenne": sum(par_cle[m]["p"] for m in ordre_m) / len(ordre_m)})
    sortie.sort(key=lambda g: (-sum(par_cle[m]["p"] for m in g["membres"]), g["chef"]))
    numero = {}
    for k, g in enumerate(sortie, 1):
        g["numero"] = k
        numero[g["chef"]] = k
    for g in sortie:
        g["aussi"] = [{**a, "groupe": numero.get(a["chef"])} for a in g["aussi"]]
        g["travaille_avec"] = [{"cle": d, "groupe": numero.get(d)} for d in g["travaille_avec"]]
        for a in g["aussi"]:
            par_cle[a["cle"]].setdefault("accompagne_aussi", []).append(g["chef"])
    dans_groupe = {m for g in sortie for m in g["membres"]}
    individuelles = [c["cle"] for c in tous if c["dans_liste"] and c["cle"] not in dans_groupe]
    return {"groupes": sortie, "individuelles": individuelles, "ajoutees": ajoutees,
            "alliees_cherchees": alliees_de}
