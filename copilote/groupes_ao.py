"""Les sociétés probables d'un AO réparties en groupes de présence commune, et la stratégie passée de
chaque groupe.

Pour chaque société probable (p ≥ seuil_allies), le calcul « autour d'une société » (autour_societe.py,
les règles de la page des groupes : noyau, accompagnatrice, concurrent direct) est refait sur les seules
unités d'avant la coupure de l'AO, totaux compris. Ses alliées (noyaux et accompagnatrices du groupe
de 9 au plus) donnent des liens :

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


def former(mem: Memoire, n: int, tous: list[dict], r: dict) -> dict:
    """Groupes et individuelles. Modifie les candidats de `tous` (association, p) ; renvoie aussi les
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
            elif l["lecture"] == "noyau" or (l["C"] is not None and l["C"] >= r["c_association"]):
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
