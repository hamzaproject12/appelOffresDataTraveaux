"""Étape C : la simulation de l'ouverture des plis d'un AO.

Recopié À L'IDENTIQUE dans statique/simulateur.js : même générateur (mulberry32), mêmes tirages dans le même
ordre, mêmes opérations. Même graine = même résultat ; tests/cas_simulation.json et tests/test_simulateur.py
le vérifient.

Un scénario, dans cet ordre :
  1. l'effet commun sur le nombre : u = δ + σ_u·z − σ_u²/2 (la loi A2 du nombre de concurrents) ;
  2. l'effet de marché sur les taux : m = σ_m·z ;
  3. chaque groupe en étoile : le chef de file vient avec la probabilité logistique(logit p + u) ; s'il vient,
     le groupe « vient pour gagner » avec la probabilité w = taux de victoire du chef sur les marchés
     similaires. Pour gagner : chaque accompagnatrice vient avec sa fréquence mesurée sur les victoires du
     chef (q), à son écart habituel à l'offre du chef sur ces victoires. Sinon : elle vient avec sa propre
     probabilité, à son taux « chef présent » ou « chef absent » ;
  4. les sociétés hors groupe : présence logistique(logit p + u), taux μ + m + ε, ε ~ N(0, σ_propre) ;
  5. chaque société présente est évincée avec sa probabilité d'éviction ;
  6. les concurrents non identifiés : K ~ Poisson(λ·eᵘ), taux tiré dans la distribution du segment ;
  7. notre offre x est insérée (admise) et la commission désigne le gagnant.

Règle du gagnant (construire_base.py) : P = (moyenne des offres admises + estimation) / 2 ; le gagnant est
l'offre la plus proche de P par défaut, sinon la plus proche par excès. En taux (estimation = 1), notre offre x
gagne si x ≤ P(x) et aucune autre offre n'est dans [x, P(x)], ou si x > P(x) et toutes les autres offres sont
au-dessus de x (à égalité de montant, l'offre déjà déposée passe devant la nôtre, comme dans
copilote/references.py). La courbe utilise cette lecture directe ; `attribution_exacte` passe par
prix_de_reference et classement_commission importés de construire_base, et le test vérifie qu'elles désignent
le même gagnant (l'arrondi au centime de construire_base mis à part).
"""
from __future__ import annotations

import math
import zlib

import numpy as np

from construire_base import classement_commission, prix_de_reference

M32 = 0xFFFFFFFF
TAUX_MIN, TAUX_MAX = 0.3, 3.0
GRILLE = {"debut": 1600, "denominateur": 2000, "n": 801}     # x = (1600 + j) / 2000 : 0,80 à 1,20 par 0,0005
SCENARIOS = 4000
LISSAGE = 10            # moyenne glissante sur ±10 points de grille (±0,005) pour la zone recommandée
PART_ZONE = 0.9         # la zone : chance lissée ≥ 90 % du maximum, autour du maximum
CLASSES_P = {"debut": 0.70, "pas": 0.005, "n": 120}      # histogramme du prix de référence (0,70 à 1,30)


# --------------------------------------------------------------------------- générateur commun

class Generateur:
    """mulberry32 sur 32 bits non signés ; statique/simulateur.js fait la même chose avec Math.imul."""

    def __init__(self, graine: int):
        self.a = graine & M32

    def uniforme(self) -> float:
        self.a = (self.a + 0x6D2B79F5) & M32
        t = self.a
        t = ((t ^ (t >> 15)) * (t | 1)) & M32
        t = (t ^ ((t + (((t ^ (t >> 7)) * (t | 61)) & M32)) & M32)) & M32
        return ((t ^ (t >> 14)) & M32) / 4294967296.0

    def normale(self) -> float:
        u1 = 1.0 - self.uniforme()
        u2 = self.uniforme()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def poisson(self, moyenne: float) -> int:
        if moyenne <= 0:
            return 0
        limite = math.exp(-moyenne)
        k, p = 0, self.uniforme()
        while p > limite and k < 1000:
            k += 1
            p *= self.uniforme()
        return k


def graine_de(ref: str, lot: str | None) -> int:
    return zlib.crc32(f"{ref}#{lot or ''}".encode()) & M32


def logistique(z: float) -> float:
    if z < -700.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def logit(p: float) -> float:
    p = min(1 - 1e-9, max(1e-9, p))
    return math.log(p / (1 - p))


def quantile_interpole(q: list[float], u: float) -> float:
    x = u * (len(q) - 1)
    a = math.floor(x)
    if a >= len(q) - 1:
        return q[-1]
    return q[a] + (q[a + 1] - q[a]) * (x - a)


def quantile(tries: list[float], p: float) -> float | None:
    if not tries:
        return None
    x = p * (len(tries) - 1)
    a = math.floor(x)
    b = min(a + 1, len(tries) - 1)
    return tries[a] + (tries[b] - tries[a]) * (x - a)


def borner(t: float) -> float:
    return TAUX_MIN if t < TAUX_MIN else (TAUX_MAX if t > TAUX_MAX else t)


def sigma_propre(sigma: float, sigma_m: float) -> float:
    return math.sqrt(max(sigma * sigma - sigma_m * sigma_m, 0.0001))


# --------------------------------------------------------------------------- un scénario

def _scenario(g: Generateur, mo: dict, detail: bool = False):
    """Les offres admises (taux, dans l'ordre de dépôt), et ce qu'il faut pour les sorties.

    Deux temps : qui vient (groupes, sociétés seules, non identifiées), puis les taux, décalés tous ensemble de
    d(N) = constante + pente × log(N), N = nombre de sociétés venues (moins de concurrents, offres plus hautes)."""
    nb = mo["nombre"]
    su = nb["dispersion"]
    u = nb["decalage"] + su * g.normale() - su * su / 2
    ma = mo["marche"]
    m = ma["sigma"] * g.normale()
    soc = mo["societes"]
    # 1. qui vient
    venus_g = []
    n_venus = 0
    for gr in mo["groupes"]:
        c = soc[gr["chef"]]
        present = g.uniforme() < logistique(c["z"] + u)
        gagner = g.uniforme() < gr["w"]
        membres = []
        for j in gr["membres"]:
            s = soc[j]
            seuil = s["q"] if present and gagner else logistique(s["z"] + u)
            membres.append(g.uniforme() < seuil)
        venus_g.append((present, gagner, membres))
        n_venus += int(present) + sum(membres)
    venus_s = []
    for i in mo["seules"]:
        v = g.uniforme() < logistique(soc[i]["z"] + u)
        venus_s.append(v)
        n_venus += int(v)
    k = g.poisson(nb["lambda"] * math.exp(u))
    n_venus += k
    d = ma.get("constante", 0.0) + ma.get("pente", 0.0) * math.log(max(n_venus, 1))
    # 2. les taux et les évictions
    offres: list[float] = []
    qui: list[int] = []                         # indice de société, -1 - k pour la k-ième non identifiée
    evincees: list[tuple] = []
    chefs: list[float | None] = []              # taux admis du chef de chaque groupe (None : absent ou évincé)

    def poser(i: int, taux: float) -> None:
        if g.uniforme() < soc[i]["eviction"]:
            if detail:
                evincees.append((i, borner(taux)))
            return
        offres.append(borner(taux))
        qui.append(i)

    for gr, (present, gagner, membres) in zip(mo["groupes"], venus_g):
        c = soc[gr["chef"]]
        t_chef = None
        if present:
            t_chef = c["mu"] + m + d + c["sp"] * g.normale()
            avant = len(offres)
            poser(gr["chef"], t_chef)
            chefs.append(offres[-1] if len(offres) > avant else None)
        else:
            chefs.append(None)
        for j, vient in zip(gr["membres"], membres):
            if not vient:
                continue
            s = soc[j]
            if present and gagner:
                poser(j, t_chef + s["ev_mu"] + s["ev_sigma"] * g.normale())
            else:
                pr = s["avec"] if present else s["sans"]
                poser(j, pr["mu"] + m + d + pr["sp"] * g.normale())
    for i, vient in zip(mo["seules"], venus_s):
        if vient:
            s = soc[i]
            poser(i, s["mu"] + m + d + s["sp"] * g.normale())
    inc = mo["inconnues"]
    for r in range(k):
        q = quantile_interpole(inc["quantiles"], g.uniforme())
        t = inc["moyenne"] + m + d + (q - inc["moyenne"]) * inc["f"]
        if g.uniforme() < inc["eviction"]:
            if detail:
                evincees.append((-1 - r, borner(t)))
            continue
        offres.append(borner(t))
        qui.append(-1 - r)
    return offres, qui, chefs, evincees, u, m + d


def reference_sans(offres: list[float]) -> float | None:
    if not offres:
        return None
    s = 0.0
    for t in offres:
        s += t
    return (s / len(offres) + 1.0) / 2.0


def gagnant_sans(offres: list[float], p: float) -> int:
    """Indice de l'offre désignée (sans la nôtre) : la plus proche sous P, sinon la plus basse au-dessus ;
    à égalité, la première déposée."""
    meilleur, sous = -1, False
    for i, t in enumerate(offres):
        if t <= p:
            if not sous or t > offres[meilleur]:
                meilleur, sous = i, True
        elif not sous and (meilleur < 0 or t < offres[meilleur]):
            meilleur = i
    return meilleur


def grille(mo: dict | None = None) -> np.ndarray:
    gr = (mo or {}).get("grille") or GRILLE
    return np.array([(gr["debut"] + j) / gr["denominateur"] for j in range(gr["n"])])


def victoires_sur_grille(offres: list[float], xs: np.ndarray) -> np.ndarray:
    """Pour chaque x de la grille : notre offre gagne-t-elle (lecture directe de la règle, voir l'en-tête) ?"""
    k = len(offres)
    if k == 0:
        return np.ones(len(xs), dtype=bool)
    s = 0.0
    for t in offres:
        s += t
    r = np.sort(np.array(offres))
    p = ((s + xs) / (k + 1) + 1.0) / 2.0
    idx = np.searchsorted(r, xs, side="left")
    suivant = np.where(idx < k, r[np.minimum(idx, k - 1)], np.inf)
    sous = xs <= p
    return np.where(sous, suivant > p, r[0] > xs)


# --------------------------------------------------------------------------- la simulation entière

def preparer(mo: dict) -> dict:
    """Les grandeurs dérivées (logit, σ propre) calculées une fois ; même calcul dans simulateur.js."""
    sm = mo["marche"]["sigma"]
    soc = []
    for s in mo["societes"]:
        x = dict(s)
        x["z"] = logit(s["p"]) if s["p"] > 0 else -1e9
        x["sp"] = sigma_propre(s["sigma"], sm)
        for cle in ("avec", "sans"):
            if s.get(cle):
                x[cle] = {"mu": s[cle]["mu"], "sp": sigma_propre(s[cle]["sigma"], sm)}
        soc.append(x)
    inc = dict(mo["inconnues"])
    si = inc["sigma"]
    inc["f"] = math.sqrt(max(0.1, 1.0 - sm * sm / (si * si))) if si > 0 else 1.0
    dans = {i for gr in mo["groupes"] for i in [gr["chef"]] + gr["membres"]}
    return {**mo, "societes": soc, "inconnues": inc, "seules": [i for i in range(len(soc)) if i not in dans]}


def zone_recommandee(xs: np.ndarray | list, chance: list[float], scenarios: int = SCENARIOS) -> dict | None:
    """Le point recommandé : le maximum de la courbe lissée (±0,005). La zone : autour de lui, les x où la chance
    lissée reste ≥ 90 % du maximum. (`scenarios` n'est plus utilisé : gardé pour la signature.)"""
    n = len(chance)
    if n == 0:
        return None
    lisse = []
    for j in range(n):
        a, b = max(0, j - LISSAGE), min(n - 1, j + LISSAGE)
        s = 0.0
        for k in range(a, b + 1):
            s += chance[k]
        lisse.append(s / (b - a + 1))
    jm = 0
    for j in range(1, n):
        if lisse[j] > lisse[jm]:
            jm = j
    if lisse[jm] <= 0:
        return None
    seuil = PART_ZONE * lisse[jm]
    a = jm
    while a > 0 and lisse[a - 1] >= seuil:
        a -= 1
    b = jm
    while b < n - 1 and lisse[b + 1] >= seuil:
        b += 1
    return {"x": float(xs[jm]), "indice": jm, "chance": chance[jm], "chance_lissee": lisse[jm],
            "de": float(xs[a]), "a": float(xs[b]), "seuil": seuil}


def simuler(modele: dict, central: bool = True) -> dict:
    mo = preparer(modele)
    S = int(mo.get("scenarios") or SCENARIOS)
    g = Generateur(int(mo["graine"]))
    xs = grille(mo)
    gains = np.zeros(len(xs), dtype=np.int64)
    refs, refs_s, nombres, etats = [], [], [], []
    ng = len(mo["groupes"])
    chef_present = [0] * ng
    chef_gagne = [0] * ng
    chef_ecarts: list[list[float]] = [[] for _ in range(ng)]
    for s in range(S):
        etats.append(g.a)
        offres, qui, chefs, _, _, _ = _scenario(g, mo)
        gains += victoires_sur_grille(offres, xs)
        p0 = reference_sans(offres)
        nombres.append(len(offres))
        refs_s.append(p0)
        if p0 is None:
            continue
        refs.append(p0)
        gi = gagnant_sans(offres, p0)
        for k, t in enumerate(chefs):
            if t is None:
                continue
            chef_present[k] += 1
            chef_ecarts[k].append(t - p0)
            if qui[gi] == mo["groupes"][k]["chef"]:
                chef_gagne[k] += 1
    brute = [int(v) / S for v in gains]
    chance = calibrer(brute, mo.get("calibration"))
    # La calibration est croissante : elle garde l'ordre des points. Le point recommandé et la zone se lisent donc
    # sur la courbe simulée (les paliers de la table aplatiraient la courbe) ; la chance affichée est calibrée.
    zone = zone_recommandee(xs, brute, S)
    if zone:
        zone["chance"] = chance[zone["indice"]]
        zone["chance_lissee"], zone["seuil"] = calibrer([zone["chance_lissee"], zone["seuil"]], mo.get("calibration"))
    tries = sorted(refs)
    med = quantile(tries, 0.5)
    sortie = {
        "scenarios": S, "graine": int(mo["graine"]), "grille": [float(x) for x in xs], "chance": chance,
        "chance_brute": brute,
        "zone": zone,
        "prix_reference": {"n": len(tries), "mediane": med, "q10": quantile(tries, 0.1), "q90": quantile(tries, 0.9),
                           "q25": quantile(tries, 0.25), "q75": quantile(tries, 0.75),
                           "histogramme": histogramme(tries)},
        "offres_moyennes": sum(nombres) / S,
        "chefs": [{"chef": mo["societes"][gr["chef"]]["cle"], "present": chef_present[k] / S,
                   "gagne": chef_gagne[k] / S,
                   "ecart_mediane": quantile(sorted(chef_ecarts[k]), 0.5),
                   "ecart_q10": quantile(sorted(chef_ecarts[k]), 0.1),
                   "ecart_q90": quantile(sorted(chef_ecarts[k]), 0.9)} for k, gr in enumerate(mo["groupes"])],
    }
    if central and med is not None:
        sortie["central"] = scenario_central(mo, etats, refs_s, nombres, med,
                                             zone["x"] if zone else 1.0, modele.get("estimation"))
    return sortie


def calibrer(chance: list[float], table: dict | None) -> list[float]:
    """La chance simulée corrigée par la table de calibration (croissante, apprise sur 2025) : interpolation
    linéaire entre ses points. Sans table, la chance simulée telle quelle."""
    if not table:
        return list(chance)
    tx, ty = table["x"], table["y"]
    sortie = []
    for c in chance:
        if c <= tx[0]:
            sortie.append(ty[0])
            continue
        if c >= tx[-1]:
            sortie.append(ty[-1])
            continue
        k = 1
        while tx[k] < c:
            k += 1
        sortie.append(ty[k - 1] + (ty[k] - ty[k - 1]) * (c - tx[k - 1]) / (tx[k] - tx[k - 1]))
    return sortie


def histogramme(tries: list[float]) -> list[int]:
    h = [0] * CLASSES_P["n"]
    for v in tries:
        k = math.floor((v - CLASSES_P["debut"]) / CLASSES_P["pas"])
        if 0 <= k < CLASSES_P["n"]:
            h[k] += 1
    return h


def scenario_central(mo: dict, etats: list[int], refs: list, nombres: list[int], med: float, x: float,
                     estimation: float | None) -> dict:
    """Le scénario le plus proche du centre : P sans notre offre près de sa médiane, et un nombre d'offres
    près de la médiane. Rejoué en détail, notre offre placée au point recommandé."""
    nm = sorted(nombres)[len(nombres) // 2]
    meilleur, score = 0, None
    for s, (p, k) in enumerate(zip(refs, nombres)):
        if p is None:
            continue
        sc = abs(p - med) * 100.0 + abs(k - nm)
        if score is None or sc < score:
            meilleur, score = s, sc
    g = Generateur(0)
    g.a = etats[meilleur]
    offres, qui, _, evincees, u, m = _scenario(g, mo, detail=True)
    return detailler(mo, meilleur, offres, qui, evincees, x, estimation, u, m)


def detailler(mo: dict, s: int, offres: list[float], qui: list[int], evincees: list, x: float,
              estimation: float | None, u: float, m: float) -> dict:
    soc = mo["societes"]

    def nom(i):
        return {"cle": soc[i]["cle"], "nom": soc[i].get("nom") or soc[i]["cle"]} if i >= 0 else \
            {"cle": None, "nom": f"Société non identifiée {-i}"}

    toutes = offres + [x]
    moyenne = sum(toutes) / len(toutes)
    p = (moyenne + 1.0) / 2.0
    gi = gagnant_avec(offres, x, p)
    lignes = [{**nom(i), "taux": t, "admise": True} for i, t in zip(qui, offres)]
    lignes += [{**nom(i), "taux": t, "admise": False} for i, t in evincees]
    sortie = {"scenario": s, "x": x, "offres": lignes, "moyenne": moyenne, "prix_reference": p,
              "gagnant": "nous" if gi == len(offres) else nom(qui[gi])["nom"], "nous_gagnons": gi == len(offres),
              "effet_nombre": u, "effet_marche": m}
    if estimation:
        sortie["exact"] = attribution_exacte(offres, x, estimation)
    return sortie


def gagnant_avec(offres: list[float], x: float, p: float) -> int:
    """Le gagnant, notre offre x en dernier (indice len(offres)) ; à égalité, la première déposée."""
    toutes = offres + [x]
    return gagnant_sans(toutes, p)


def attribution_exacte(offres: list[float], x: float, estimation: float) -> dict:
    """La même ouverture des plis, en dirhams, avec les fonctions de construire_base.py."""
    montants = [t * estimation for t in offres] + [x * estimation]
    moyenne, nb, reference, _, _ = prix_de_reference(estimation, montants)
    rangs = classement_commission(list(enumerate(montants)), reference)
    premier = next((i for i, r in rangs.items() if r == 1), None)
    return {"moyenne": moyenne, "prix_reference": reference, "nb_offres": nb,
            "nous_gagnons": premier == len(offres), "indice_gagnant": premier}


# --------------------------------------------------------------------------- le modèle d'un dossier

def modele(dossier: dict, graine: int | None = None, scenarios: int = SCENARIOS, reglages: dict | None = None) -> dict:
    """Le modèle du simulateur à partir du dossier d'analyse (analyse.py) : le même objet que la page
    construit puis modifie (statique/simulateur.js, `appliquer`). `reglages` : facteur de l'effet de marché et
    table de calibration (simulation.json par défaut)."""
    from .taux import charger_simulation
    rs = reglages if reglages is not None else charger_simulation()
    pr = dossier["probables"]["candidats"]
    tx = dossier["taux"]
    conc = dossier["concurrents"]
    indice = {c["cle"]: k for k, c in enumerate(pr)}
    par_membre = {m["cle"]: (g, m) for g in tx["groupes"] for m in g["membres"]}
    societes = []
    for c in pr:
        p = tx["societes"].get(c["cle"])
        s = {"cle": c["cle"], "nom": c["nom"], "p": float(c["p"]), "eviction": float(c["eviction"]["p"]),
             "mu": p["mu"], "sigma": p["sigma"], "q": 0.0, "ev_mu": 0.0, "ev_sigma": 0.0}
        if c["cle"] in par_membre:
            g, m = par_membre[c["cle"]]
            s.update(avec={"mu": m["chef_present"]["mu"], "sigma": m["chef_present"]["sigma"]},
                     sans={"mu": m["chef_absent"]["mu"], "sigma": m["chef_absent"]["sigma"]},
                     q=m["presence_victoires"]["q"], ev_mu=m["ecart_victoires"]["mu"],
                     ev_sigma=m["ecart_victoires"]["sigma"])
        societes.append(s)
    groupes = []
    for g in tx["groupes"]:
        membres = [indice[m["cle"]] for m in g["membres"] if m["cle"] in indice]
        if g["chef"] in indice and membres:
            groupes.append({"chef": indice[g["chef"]], "w": g["taux_victoire_chef"], "membres": membres})
    seg = tx["segment"]
    ao = dossier.get("ao") or {}
    return {
        "graine": graine if graine is not None else graine_de(str(ao.get("ref_annonce") or ""), ao.get("lot")),
        "scenarios": scenarios, "grille": dict(GRILLE), "estimation": ao.get("estimation"),
        "nombre": {"decalage": float(conc.get("decalage") or 0.0), "dispersion": float(conc["dispersion"]),
                   "lambda": float(conc["lambda"])},
        "marche": {"sigma": float(tx["effet_marche"]["sigma"]) * float(rs.get("facteur_marche") or 1.0),
                   "constante": float(tx["effet_marche"].get("constante") or 0.0),
                   "pente": float(tx["effet_marche"].get("pente") or 0.0)},
        "societes": societes, "groupes": groupes, "calibration": rs.get("calibration"),
        "inconnues": {"quantiles": seg["quantiles"], "moyenne": seg["moyenne"], "sigma": seg["sigma"],
                      "eviction": float(pr[0]["eviction"]["global"]) if pr else 0.1},
    }


def appliquer(modele: dict, modifs: dict | None) -> dict:
    """Les modifications de la page ({cle: {retiree, p, mu}}) sur une copie du modèle ; recopiée de
    statique/simulateur.js (appliquer), pour les tests et les mesures."""
    modifs = modifs or {}
    societes = []
    for s in modele["societes"]:
        md = modifs.get(s["cle"])
        if not md:
            societes.append(s)
            continue
        x = dict(s)
        if md.get("retiree"):
            x["p"], x["q"] = 0, 0
        elif md.get("p") is not None:
            x["p"] = md["p"]
        if md.get("mu") is not None:
            d = md["mu"] - s["mu"]
            x["mu"] = md["mu"]
            if s.get("avec"):
                x["avec"] = {"mu": s["avec"]["mu"] + d, "sigma": s["avec"]["sigma"]}
            if s.get("sans"):
                x["sans"] = {"mu": s["sans"]["mu"] + d, "sigma": s["sans"]["sigma"]}
        societes.append(x)
    return {**modele, "societes": societes}
