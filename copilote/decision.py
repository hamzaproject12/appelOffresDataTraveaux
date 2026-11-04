"""Décision GO / À ÉTUDIER / NO-GO (étape D).

Deux copies identiques : ce fichier (backtest, prévisions figées) et statique/decision.js (la page, qui recalcule à
chaque modification). tests/cas_decision.json les compare.

Tout est en part de l'estimation E (taux) : coût c = coût en DH / E, prix x, gain espéré (x − c) × chance(x) où la
chance est la chance CALIBRÉE de l'étape C2 (hypothèse : les concurrents gardent leur comportement habituel).

    plage de prix   de max(plancher ; c × (1 + marge minimale)) à plafond      (0,82 ; 1,20)
    prix recommandé le maximum du gain espéré lissé (±0,005, comme la zone de C2) dans cette plage
    zone            autour de lui, là où le gain lissé reste ≥ 90 % du maximum, dans la plage
    meilleure chance le maximum de la chance lissée à partir du plancher (le point de C2), donné pour comparaison

Les raisons sont des listes {code, ton, v} : un code, un ton (bloquant, attention, ok, info) et les chiffres qui les
fondent. Les phrases viennent de decision_textes.json, remplies avec ces seuls chiffres.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from . import reglages

TEXTES = Path(__file__).with_name("decision_textes.json")
GO, ETUDIER, NOGO = "GO", "A_ETUDIER", "NO_GO"
RANG = {GO: 0, ETUDIER: 1, NOGO: 2}
EPS = 1e-12


REGLE_SIMPLE = Path(__file__).with_name("regle_simple_2025.json")


def charger_reglages() -> dict:
    """Les seuils de reglages.json (section « decision ») et la règle simple apprise sur 2025 (regle_simple_2025.json)."""
    reg = dict(reglages.charger()["decision"])
    if REGLE_SIMPLE.exists():
        with open(REGLE_SIMPLE, encoding="utf-8") as f:
            reg["regle_simple"] = json.load(f)
    return reg


def charger_textes() -> dict:
    with open(TEXTES, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- la courbe

def lisser(valeurs: list[float], k: int) -> list[float]:
    n = len(valeurs)
    sortie = []
    for j in range(n):
        a, b = max(0, j - k), min(n - 1, j + k)
        s = 0.0
        for i in range(a, b + 1):
            s += valeurs[i]
        sortie.append(s / (b - a + 1))
    return sortie


def indice_de(xs: list[float], x: float) -> int:
    j = int(math.floor((x - xs[0]) / (xs[1] - xs[0]) + 0.5))
    return min(len(xs) - 1, max(0, j))


def _autour(lisse: list[float], jm: int, j0: int, j1: int, seuil: float) -> tuple[int, int]:
    a = jm
    while a > j0 and lisse[a - 1] >= seuil:
        a -= 1
    b = jm
    while b < j1 and lisse[b + 1] >= seuil:
        b += 1
    return a, b


def _point(xs, chance, c, E, j) -> dict:
    x = xs[j]
    return {"x": x, "indice": j, "chance": chance[j], "marge": x - c, "marge_dh": (x - c) * E,
            "marge_pct": (x - c) / c if c > 0 else None, "gain": (x - c) * chance[j],
            "gain_dh": (x - c) * chance[j] * E}


def regle_simple(xs, chance, c, E, xmin, xmax, reg, prevu):
    """La « règle simple apprise sur 2025 » : le meilleur taux fixe de la tranche de cet AO (selon le nombre prévu de
    concurrents ; toutes tranches si on ne le connaît pas), pour NOTRE coût, parmi les taux permis (de xmin à xmax). Sa chance,
    sa marge et son gain espéré sont ceux de cet AO, lus sur la même courbe que les autres lignes."""
    rs = reg.get("regle_simple")
    if not rs:
        return None
    tranche = rs["tout"]
    if prevu is not None:
        tranche = next((t for t in rs["tranches"] if t["borne"] is None or prevu < t["borne"]), rs["tranches"][-1])
    meilleur = None
    for k, xk in enumerate(rs["taux"]):
        if xk < xmin - EPS or xk > xmax + EPS:
            continue
        g = (xk - c) * tranche["w"][k]
        if meilleur is None or g > meilleur[0]:
            meilleur = (g, xk)
    if meilleur is None:
        return None
    pt = _point(xs, chance, c, E, indice_de(xs, meilleur[1]))
    pt.update(cle="fixe", libelle=tranche["libelle"], appris_sur=rs["appris_sur"], marches_appris=tranche["marches"])
    return pt


def evaluer(xs: list[float], chance: list[float], estimation: float, cout_dh: float, marge_min: float,
            reg: dict | None = None, prevu: float | None = None) -> dict:
    """La plage de prix, le prix recommandé, la zone, le point de meilleure chance et le tableau de choix.
    `prevu` (nombre prévu de concurrents) ne sert qu'à ajouter au tableau la ligne « règle simple apprise sur 2025 »."""
    reg = reg or charger_reglages()
    p = reg["plage"]
    n = len(xs)
    c = cout_dh / estimation
    xmin = max(p["plancher"], c * (1 + marge_min))
    xmax = p["plafond"]
    j0 = next((j for j in range(n) if xs[j] >= xmin - EPS), n)
    j1 = max((j for j in range(n) if xs[j] <= xmax + EPS), default=-1)
    ev = {"cout": c, "cout_dh": cout_dh, "estimation": estimation, "marge_min": marge_min, "xmin": xmin, "xmax": xmax,
          "vide": j0 > j1, "reco": None, "zone": None, "meilleur_gain": None, "meilleure_chance": None, "tableau": []}
    gains = [(xs[j] - c) * chance[j] for j in range(n)]
    if not ev["vide"]:
        k = reg["lissage"]
        lisse = lisser(gains, k)
        jb = j0                                           # le meilleur gain, brut, de la plage (pour la raison NO-GO)
        for j in range(j0 + 1, j1 + 1):
            if gains[j] > gains[jb]:
                jb = j
        ev["meilleur_gain"] = _point(xs, chance, c, estimation, jb)
        jm = j0
        for j in range(j0 + 1, j1 + 1):
            if lisse[j] > lisse[jm]:
                jm = j
        if lisse[jm] > 0:
            ev["reco"] = {**_point(xs, chance, c, estimation, jm), "gain_lisse": lisse[jm]}
            a, b = _autour(lisse, jm, j0, j1, reg["zone_part"] * lisse[jm])
            ev["zone"] = {"de": xs[a], "a": xs[b]}
    # Le point de meilleure chance, comme la zone de C2 : maximum de la chance lissée à partir du plancher.
    jp = next((j for j in range(n) if xs[j] >= p["plancher"] - EPS), n - 1)
    ls = lisser(chance, reg["lissage"])
    jc = jp
    for j in range(jp + 1, n):
        if ls[j] > ls[jc]:
            jc = j
    mc = _point(xs, chance, c, estimation, jc)
    mc["sous_marge"] = xs[jc] < xmin - EPS
    ev["meilleure_chance"] = mc
    lignes = [{"cle": f"{x:g}", "x": x} for x in reg["table_choix"]]
    if ev["reco"]:
        lignes.append({"cle": "reco", "x": ev["reco"]["x"]})
    for l in lignes:
        pt = _point(xs, chance, c, estimation, indice_de(xs, l["x"]))
        pt.update(cle=l["cle"], hors_plage=pt["x"] < p["plancher"] - EPS or pt["x"] > xmax + EPS,
                  sous_marge=pt["x"] < c * (1 + marge_min) - EPS, alerte=pt["x"] < p["alerte"] - EPS)
        ev["tableau"].append(pt)
    if not ev["vide"]:
        fixe = regle_simple(xs, chance, c, estimation, xmin, xmax, reg, prevu)
        if fixe:
            fixe.update(hors_plage=False, sous_marge=False, alerte=fixe["x"] < p["alerte"] - EPS)
            ev["tableau"].append(fixe)
    # Pour la phrase « petit / gros marché » : la chance au plancher, à 1,00 et au meilleur point de la courbe.
    jp1 = indice_de(xs, p["plancher"])
    ev["chance_plancher"] = {"x": xs[jp1], "chance": chance[jp1]}
    ev["chance_un"] = {"x": xs[indice_de(xs, 1.0)], "chance": chance[indice_de(xs, 1.0)]}
    jx = jp1
    for j in range(jp1 + 1, n):
        if chance[j] > chance[jx]:
            jx = j
    ev["chance_max"] = {"x": xs[jx], "chance": chance[jx]}
    return ev


# --------------------------------------------------------------------------- la décision

def liste_manques(elig: dict | None) -> str:
    if not elig:
        return ""
    return ", ".join(f"{l['code']} classe {l['classe']}" for l in elig["lignes"]
                     if l["etat"] in ("absent", "classe") and not l.get("couvert_par"))


def liste_incertitudes(elig: dict | None) -> str:
    if not elig:
        return ""
    sortie = []
    for l in elig["lignes"]:
        if l["etat"] == "inconnu":
            sortie.append(f"qualification non reconnue ({(l.get('brut') or 'sans libellé').strip()})")
        elif l.get("alternative"):
            sortie.append(f"alternative « ou » ({l['code']})")
    return ", ".join(dict.fromkeys(sortie))


def decider(ev: dict, elig: dict | None, signaux: dict | None, reg: dict | None = None) -> dict:
    """GO / À ÉTUDIER / NO-GO et ses raisons. `elig` : résultat de eligibilite.verifier (None = non évaluée, traitée
    comme éligible). `signaux` : jours, acheteur_n, infructueux {n, total, taux}, identifies_part, groupes_forts
    [{numero, nom, p}], en_cours, plan_max, prevu — tous facultatifs."""
    reg = reg or charger_reglages()
    s = signaux or {}
    raisons, niveau = [], GO

    def baisser(n):
        nonlocal niveau
        if RANG[n] > RANG[niveau]:
            niveau = n

    def dire(code, ton, **v):
        raisons.append({"code": code, "ton": ton, "v": v})

    E = ev["estimation"]
    jours = s.get("jours")
    if jours is not None and jours < 0:
        dire("delai_depasse", "bloquant", jours=-jours)
        baisser(NOGO)
    elif jours is not None and jours < reg["delai_min_jours"]:
        dire("delai_court", "attention", jours=jours, minimum=reg["delai_min_jours"])
        baisser(ETUDIER)

    if elig and elig["statut"] == "non_eligible":
        dire("non_eligible", "bloquant", liste=liste_manques(elig))
        baisser(NOGO)
    elif elig and elig["statut"] == "a_etudier":
        dire("eligibilite_incertaine", "attention", liste=liste_incertitudes(elig))
        baisser(ETUDIER)

    reco = ev["reco"]
    if ev["vide"]:
        dire("plage_vide", "bloquant", cout_dh=ev["cout_dh"], xmin=ev["xmin"], xmin_dh=ev["xmin"] * E, xmax=ev["xmax"])
        baisser(NOGO)
    elif reco is None or reco["gain"] < reg["gain"]["nogo"]:
        mg = ev["meilleur_gain"]
        dire("aucun_gain", "bloquant", xmin=ev["xmin"], xmax=ev["xmax"], gain=mg["gain"], gain_dh=mg["gain_dh"],
             x=mg["x"], seuil=reg["gain"]["nogo"])
        baisser(NOGO)
    else:
        if reco["chance"] < reg["chance"]["nogo"]:
            dire("chance_faible", "bloquant", chance=reco["chance"], x=reco["x"], seuil=reg["chance"]["nogo"])
            baisser(NOGO)
        elif reco["chance"] < reg["chance"]["go"]:
            dire("chance_moyenne", "attention", chance=reco["chance"], x=reco["x"], seuil=reg["chance"]["go"])
            baisser(ETUDIER)
        else:
            dire("chance_ok", "ok", chance=reco["chance"], x=reco["x"])
        if reco["gain"] < reg["gain"]["go"]:
            dire("gain_moyen", "attention", gain=reco["gain"], gain_dh=reco["gain_dh"], x=reco["x"],
                 prix_dh=reco["x"] * E, seuil=reg["gain"]["go"])
            baisser(ETUDIER)
        else:
            dire("gain_ok", "ok", gain=reco["gain"], gain_dh=reco["gain_dh"], x=reco["x"], prix_dh=reco["x"] * E)
        dire("marge", "info", marge_dh=reco["marge_dh"], marge_pct=reco["marge_pct"], marge_min=ev["marge_min"])
        if reco["x"] < reg["plage"]["alerte"] - EPS:
            dire("proche_seuil", "attention", x=reco["x"], alerte=reg["plage"]["alerte"])

    # La nature du marché : le nombre prévu de concurrents (dossier, étape ③).
    prevu = s.get("prevu")
    if prevu is not None:
        if prevu < reg["petit_marche"]:
            dire("marche_petit", "info", prevu=prevu, c_bas=ev["chance_plancher"]["chance"], x_bas=ev["chance_plancher"]["x"],
                 c_un=ev["chance_un"]["chance"], c_max=ev["chance_max"]["chance"], x_max=ev["chance_max"]["x"])
        elif ev["chance_max"]["chance"] < reg["chance"]["go"]:
            dire("marche_gros_faible", "info", prevu=prevu, c_max=ev["chance_max"]["chance"], x_max=ev["chance_max"]["x"])
        else:
            dire("marche_gros", "info", prevu=prevu, c_max=ev["chance_max"]["chance"], x_max=ev["chance_max"]["x"])

    # Les signaux de confiance et de risque : ils font passer un GO en À ÉTUDIER, jamais en NO-GO.
    faibles = []
    n_ach = s.get("acheteur_n")
    if n_ach is not None and n_ach < reg["confiance"]["acheteur_min"]:
        faibles.append(("acheteur_peu", {"n": n_ach}))
    part = s.get("identifies_part")
    if part is not None and part < reg["confiance"]["identifies_min"]:
        faibles.append(("identifies_peu", {"part": part}))
    confiance = "inconnue" if n_ach is None and part is None else ("faible" if faibles else "correcte")
    if faibles:
        for code, v in faibles:
            dire("confiance_" + code, "attention", **v)
        baisser(ETUDIER)
    for g in s.get("groupes_forts") or []:
        dire("groupe_fort", "attention", nom=g["nom"], p=g["p"], numero=g["numero"])
        baisser(ETUDIER)
    inf = s.get("infructueux")
    if inf and inf["total"] > 0 and inf["taux"] > reg["infructueux_max"]:
        dire("infructueux", "attention", taux=inf["taux"], n=inf["n"], total=inf["total"], seuil=reg["infructueux_max"])
        baisser(ETUDIER)
    if s.get("plan_max") is not None and s.get("en_cours") is not None and s["en_cours"] >= s["plan_max"]:
        dire("plan_plein", "attention", en_cours=s["en_cours"], plan_max=s["plan_max"])
        baisser(ETUDIER)
    return {"decision": niveau, "raisons": raisons, "confiance": confiance}


# --------------------------------------------------------------------------- les phrases

def _arrondi(v: float, d: int) -> tuple[str, int]:
    n = int(math.floor(abs(v) * 10 ** d + 0.5))
    return ("-" if v < 0 and n else ""), n


def _groupes(n: int) -> str:
    t = str(n)
    morceaux = []
    while len(t) > 3:
        morceaux.insert(0, t[-3:])
        t = t[:-3]
    morceaux.insert(0, t)
    return " ".join(morceaux)


def _decimal(v: float, d: int, groupe: bool = False) -> str:
    signe, n = _arrondi(v, d)
    ent, frac = divmod(n, 10 ** d) if d else (n, 0)
    corps = (_groupes(ent) if groupe else str(ent)) + (("," + str(frac).zfill(d)) if d else "")
    return signe + corps


FORMATS = {
    "dh": lambda v: _decimal(v, 0, True), "pct": lambda v: _decimal(v * 100, 1) + " %",
    "pct0": lambda v: _decimal(v * 100, 0) + " %", "t3": lambda v: _decimal(v, 3),
    "n1": lambda v: _decimal(v, 1), "n": lambda v: _decimal(v, 0), "s": lambda v: str(v),
}
_MOTIF = re.compile(r"\{(\w+)(?::(\w+))?\}")


def remplir(modele: str, v: dict) -> str:
    return _MOTIF.sub(lambda m: FORMATS[m.group(2) or "s"](v[m.group(1)]), modele)


def phrase(raison: dict, textes: dict | None = None) -> str:
    textes = textes or charger_textes()
    return remplir(textes["raisons"][raison["code"]], raison["v"])


# --------------------------------------------------------------------------- scénarios et coût

def cout_du_scenario(cout_dh: float, estimation: float, partenaire: dict | None) -> float:
    """Le coût de l'AO pour un scénario : le coût de base plus l'effet déclaré du partenaire (pct : part de
    l'estimation ; dh). Un partenaire ne change que ce coût (et l'éligibilité), jamais le prix d'une autre société."""
    if not partenaire:
        return cout_dh
    e = partenaire.get("effet_cout") or 0.0
    return cout_dh + (e * estimation if partenaire.get("effet_cout_unite") == "pct" else e)
