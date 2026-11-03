"""Étape C : le taux probable de chaque société de l'AO, la distribution des concurrents non identifiés et
l'effet de marché.

Tout est en taux (montant / estimation) et ne lit que les unités visibles 0..n-1 (coupure de l'AO :
rejeu.limite_pour pour un REJEU, Memoire.coupe).

1. Taux d'une société : moyenne pondérée de ses taux passés, poids
       w = similarité^γ × 0,5^(âge / demi-vie) × (1 + bonus si l'offre était chez le même acheteur)
   (similarité = la note du moteur de l'étape 4, sur 100, sans son filtre de montant et de qualification).
   Avec n_eff = (Σw)² / Σw², la moyenne est rapprochée de celle du segment :
       μ = (n_eff · μ_société + k_moyenne · μ_segment) / (n_eff + k_moyenne)
       σ² = (n_eff · s²_société + k_sigma · σ²_segment) / (n_eff + k_sigma)
   Fourchette 10–90 % : μ ± 1,2816 σ.
2. Segment : les offres visibles chez l'acheteur, dans la famille et la tranche de montant de l'AO ; s'il y en
   a moins de `min_segment`, on élargit (acheteur + famille, famille + tranche, famille, tout). Il sert aussi
   aux concurrents non identifiés (quantiles transmis au simulateur).
3. Effet de marché : la part de variation commune à toutes les offres d'un même marché. Écart de chaque offre
   à la moyenne de la société, moyenne de ces écarts par marché, variance corrigée du bruit d'échantillon :
       σ²_marché = var(moyennes par marché) − moyenne(variance intra / nombre d'offres).
   Le simulateur tire un effet commun m ~ N(0, σ_marché) et, pour chaque société, ε ~ N(0, σ_propre) avec
   σ²_propre = σ² − σ²_marché.
4. Membres d'un groupe : deux profils (chef de file présent / absent), l'écart habituel à l'offre du chef
   (marchés communs, et victoires du chef), la fréquence de présence sur les victoires du chef.

Réglages appris sur 2025 : copilote/simulation.json (python -m copilote.mesures_simulation ajuster).
"""
from __future__ import annotations

import bisect
import json
import math
import statistics
from pathlib import Path

from .bases import cle_acheteur
from .probables import Contexte, Memoire

FICHIER = Path(__file__).with_name("simulation.json")

DEFAUT = {
    "gamma": 1.0,               # exposant de la similarité
    "demi_vie": 365,            # jours
    "bonus_acheteur": 1.0,      # une offre chez le même acheteur pèse (1 + bonus) fois plus
    "k_moyenne": 3.0,           # rapprochement de la moyenne vers le segment (en marchés « effectifs »)
    "k_sigma": 3.0,             # idem pour la dispersion
    "min_segment": 40,          # offres au moins pour garder un niveau de segment
    "taux_min": 0.4, "taux_max": 1.6,   # au-delà, un taux est un chiffre mal lu : ignoré
    "sigma_min": 0.01,
    "facteur_sigma": 1.0,       # σ final × facteur (réglé pour que la fourchette 10–90 % couvre 80 % des offres)
    "sous_historique": 30,      # lignes envoyées à la page
    "quantiles": 41,            # quantiles du segment transmis au simulateur
    "lissage_ecart": 2.0,       # écart au chef : rapproché de la différence des moyennes
    "lissage_presence": 2.0,    # présence sur les victoires du chef : rapprochée de p(venir)
}
TRANCHES_MONTANT = (1e6, 3e6, 1e7)          # dirhams : < 1 M, 1–3 M, 3–10 M, ≥ 10 M
Z90 = 1.2815515655446004


def charger_simulation(chemin: Path | None = None) -> dict:
    """La section « simulation » de simulation.json : facteur de l'effet de marché et table de calibration de la
    chance de gagner, appris sur 2025 (python -m copilote.mesures_simulation calibrer)."""
    try:
        with open(chemin or FICHIER, encoding="utf-8") as f:
            lu = json.load(f)
    except OSError:
        lu = {}
    return {"facteur_marche": 1.0, "calibration": None, **(lu.get("simulation") or {})}


def charger_reglages(chemin: Path | None = None) -> dict:
    try:
        with open(chemin or FICHIER, encoding="utf-8") as f:
            lu = json.load(f)
    except OSError:
        lu = {}
    return {**DEFAUT, **(lu.get("taux") or {})}


def tranche_montant(estimation: float | None) -> int | None:
    return None if not estimation else bisect.bisect_right(TRANCHES_MONTANT, estimation)


def libelle_tranche(t: int | None) -> str:
    return {None: "montant inconnu", 0: "moins de 1 M DH", 1: "1 à 3 M DH", 2: "3 à 10 M DH",
            3: "10 M DH et plus"}[t]


# --------------------------------------------------------------------------- lecture de l'historique

def _offres_societe(mem: Memoire) -> dict[str, list[tuple[int, float]]]:
    """{société: [(indice d'unité, taux)]}, gardé dans la mémoire (une offre par société et par unité)."""
    cache = getattr(mem, "_taux_societes", None)
    if cache is None:
        cache = {}
        for i, u in enumerate(mem.unites):
            vus = set()
            for c in u.concurrents:
                k, t = c["cle"], c.get("taux")
                if k and k not in vus:
                    vus.add(k)
                    if t:
                        cache.setdefault(k, []).append((i, float(t)))
        mem._taux_societes = cache
    return cache


def _taux_unite(mem: Memoire, i: int, r: dict) -> dict[str, float]:
    sortie = {}
    for c in mem.unites[i].concurrents:
        k, t = c["cle"], c.get("taux")
        if k and k not in sortie and t and r["taux_min"] <= t <= r["taux_max"]:
            sortie[k] = float(t)
    return sortie


def _moy_var(valeurs: list[float], poids: list[float] | None = None) -> tuple[float, float]:
    if poids is None:
        poids = [1.0] * len(valeurs)
    s = sum(poids)
    m = sum(w * v for w, v in zip(poids, valeurs)) / s
    var = sum(w * (v - m) ** 2 for w, v in zip(poids, valeurs)) / s
    ne = s * s / sum(w * w for w in poids)
    if ne > 1.0:
        var *= ne / (ne - 1.0)          # correction de petit échantillon
    return m, var


# --------------------------------------------------------------------------- effet de marché

def effet_marche(mem: Memoire, n: int, r: dict | None = None) -> dict:
    """Le niveau commun des offres d'un marché, mesuré sur les unités visibles (voir l'en-tête), en deux parts :
      - selon le nombre de sociétés présentes : écart d(n) = constante + pente × log(n), ajusté offre par offre
        sur l'écart de chaque offre à la moyenne de sa société (moins il y a de concurrents, plus les offres
        sont hautes) ;
      - un choc aléatoire de σ « sigma », ce qui reste une fois d(n) retiré (marchés d'au moins 3 offres).
    Gardé par début de mois : la coupure réelle est toujours postérieure (rien d'après n n'est lu)."""
    r = r or charger_reglages()
    cache = mem.__dict__.setdefault("_effet_marche", {})
    if n in cache:
        return cache[n]
    par_societe: dict[str, list[float]] = {}
    lignes = []
    for i in range(n):
        t = _taux_unite(mem, i, r)
        if t:
            lignes.append((t, len(mem.presents[i])))
        for k, v in t.items():
            par_societe.setdefault(k, []).append(v)
    moy = {k: sum(v) / len(v) for k, v in par_societe.items() if len(v) >= 3}
    xs, ys = [], []
    for t, nb in lignes:
        for k, v in t.items():
            if k in moy:
                xs.append(math.log(max(nb, 1)))
                ys.append(v - moy[k])
    pente = constante = 0.0
    if len(xs) >= 100:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        pente = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 0 else 0.0
        constante = my - pente * mx
    moyennes, intra, total = [], [], []
    for t, nb in lignes:
        d = constante + pente * math.log(max(nb, 1))
        e = [v - moy[k] - d for k, v in t.items() if k in moy]
        if len(e) < 3:
            continue
        m = sum(e) / len(e)
        s2 = sum((x - m) ** 2 for x in e) / (len(e) - 1)
        moyennes.append(m)
        intra.append(s2 / len(e))
        total.extend(e)
    if len(moyennes) < 30:
        sortie = {"sigma": 0.0, "part": 0.0, "marches": len(moyennes), "sigma_total": None,
                  "constante": constante, "pente": pente}
    else:
        mm = sum(moyennes) / len(moyennes)
        v_m = sum((x - mm) ** 2 for x in moyennes) / (len(moyennes) - 1) - sum(intra) / len(intra)
        v_m = max(v_m, 0.0)
        mt = sum(total) / len(total)
        v_t = sum((x - mt) ** 2 for x in total) / (len(total) - 1)
        sortie = {"sigma": math.sqrt(v_m), "part": v_m / v_t if v_t > 0 else 0.0, "marches": len(moyennes),
                  "sigma_total": math.sqrt(v_t), "constante": constante, "pente": pente}
    cache[n] = sortie
    return sortie


# --------------------------------------------------------------------------- segment

def segment(mem: Memoire, ctx: Contexte, r: dict | None = None) -> dict:
    """La distribution des offres du segment de l'AO, au niveau le plus fin qui en a assez."""
    r = r or charger_reglages()
    n = ctx.n
    tr = tranche_montant(ctx.estimation)
    ach = sorted({i for a in ctx.acheteur_cles for i in Memoire.avant(mem.par_acheteur.get(a, []), n)})

    def offres(indices, filtre):
        return [v for i in indices if filtre(i) for v in _taux_unite(mem, i, r).values()]

    def meme_tranche(i):
        return tranche_montant(mem.unites[i].estimation) == tr

    fam = ctx.famille
    niveaux = []
    if fam:
        niveaux += [("acheteur, famille et tranche de montant", ach, lambda i: mem.famille[i] == fam and meme_tranche(i)),
                    ("acheteur et famille", ach, lambda i: mem.famille[i] == fam)]
        niveaux += [("famille et tranche de montant", range(n), lambda i: mem.famille[i] == fam and meme_tranche(i)),
                    ("famille", range(n), lambda i: mem.famille[i] == fam)]
    else:
        niveaux += [("acheteur et tranche de montant", ach, meme_tranche), ("acheteur", ach, lambda i: True)]
    niveaux.append(("tous les marchés", range(n), lambda i: True))
    for nom, indices, filtre in niveaux:
        v = offres(indices, filtre)
        if len(v) >= r["min_segment"] or nom == "tous les marchés":
            break
    if not v:
        v = [1.0]
    v.sort()
    q = int(r["quantiles"])
    quantiles = [_quantile(v, k / (q - 1)) for k in range(q)]
    m, var = _moy_var(v)
    return {"niveau": nom, "offres": len(v), "moyenne": m, "sigma": math.sqrt(var), "quantiles": quantiles,
            "famille": fam, "tranche": libelle_tranche(tr)}


def _quantile(tries: list[float], p: float) -> float:
    if len(tries) == 1:
        return tries[0]
    x = p * (len(tries) - 1)
    a = int(math.floor(x))
    b = min(a + 1, len(tries) - 1)
    return tries[a] + (tries[b] - tries[a]) * (x - a)


# --------------------------------------------------------------------------- la note de similarité

class Similarites:
    """La note (sur 100) de chaque unité visible par rapport à l'AO, sans le filtre du moteur : une offre
    passée hors de la fourchette de montant pèse peu, mais elle n'est pas perdue."""

    def __init__(self, moteur=None, composantes=None, poids: dict | None = None):
        self.notes, self.index = None, {}
        if moteur is not None and composantes is not None:
            from .similarite import CRITERES, poids_utiles
            utiles = poids_utiles(composantes, poids)
            somme = sum(utiles.values()) or 1.0
            self.notes = sum(utiles[c] * composantes.valeurs[c] for c in CRITERES) / somme * 100.0
            self.index = moteur.corpus.index_de

    def note(self, u) -> float:
        if self.notes is None:
            return 50.0
        j = self.index.get(u.identifiant)
        return float(self.notes[j]) if j is not None and j < len(self.notes) else 0.0


# --------------------------------------------------------------------------- le taux d'une société

def _poids(mem: Memoire, ctx: Contexte, i: int, sims: Similarites, achs: frozenset, r: dict) -> tuple:
    u = mem.unites[i]
    s = max(sims.note(u), 1.0) / 100.0
    age = max(0, ctx.jour - mem.jours[i])
    chez = cle_acheteur(u.acheteur) in achs
    w = s ** r["gamma"] * 0.5 ** (age / r["demi_vie"]) * (1.0 + (r["bonus_acheteur"] if chez else 0.0))
    return w, s, age, chez


def profil(mem: Memoire, ctx: Contexte, cle: str, seg: dict, sims: Similarites, r: dict,
           filtre=None, details: bool = False) -> dict:
    """μ, σ, n, n_eff d'une société (sur les unités visibles qui passent `filtre`)."""
    lignes = []
    for i, t in _offres_societe(mem).get(cle, []):
        if i >= ctx.n:
            break
        if not (r["taux_min"] <= t <= r["taux_max"]) or (filtre and not filtre(i)):
            continue
        w, s, age, chez = _poids(mem, ctx, i, sims, ctx.acheteur_cles, r)
        lignes.append((i, t, w, s, age, chez))
    mu_s, s_s = seg["moyenne"], seg["sigma"]
    if not lignes:
        mu, var, ne, m_brut, v_brut = mu_s, s_s ** 2, 0.0, None, None
    else:
        m_brut, v_brut = _moy_var([l[1] for l in lignes], [l[2] for l in lignes])
        sw = sum(l[2] for l in lignes)
        ne = sw * sw / sum(l[2] ** 2 for l in lignes)
        mu = (ne * m_brut + r["k_moyenne"] * mu_s) / (ne + r["k_moyenne"])
        var = (ne * v_brut + r["k_sigma"] * s_s ** 2) / (ne + r["k_sigma"])
    sigma = max(math.sqrt(var) * r["facteur_sigma"], r["sigma_min"])
    sortie = {"mu": mu, "sigma": sigma, "q10": mu - Z90 * sigma, "q90": mu + Z90 * sigma,
              "n": len(lignes), "n_eff": ne, "moyenne_brute": m_brut,
              "sigma_brut": None if v_brut is None else math.sqrt(v_brut),
              "chez_acheteur": sum(1 for l in lignes if l[5])}
    if details:
        sw = sum(l[2] for l in lignes) or 1.0
        lignes.sort(key=lambda l: -l[2])
        sortie["sous_historique"] = [_ligne(mem, l, sw, cle) for l in lignes[:int(r["sous_historique"])]]
    return sortie


def _ligne(mem: Memoire, l: tuple, sw: float, cle: str) -> dict:
    i, t, w, s, age, chez = l
    u = mem.unites[i]
    return {"ref": u.ref, "lot": u.lot, "date": u.date, "acheteur": u.acheteur or "", "taux": round(t, 4),
            "poids": round(w / sw, 4), "similarite": round(s * 100, 1), "age_jours": age, "chez_acheteur": chez,
            "gagne": u.cle_gagnant == cle, "offres": len(mem.presents[i]),
            "reference": None if mem.taux_reference(u) is None else round(mem.taux_reference(u), 4)}


def _ecarts(mem: Memoire, n: int, membre: str, chef: str, r: dict, sur_victoires: bool) -> list[float]:
    idx = mem.victoires.get(chef, []) if sur_victoires else mem.par_societe.get(chef, [])
    sortie = []
    for i in Memoire.avant(idx, n):
        t = _taux_unite(mem, i, r)
        if membre in t and chef in t:
            sortie.append(t[membre] - t[chef])
    return sortie


def _robuste(ecarts: list[float], defaut_mu: float, defaut_sigma: float, k: float) -> dict:
    """Médiane et dispersion (MAD) des écarts, rapprochées du défaut avec un poids n / (n + k)."""
    n = len(ecarts)
    if n == 0:
        return {"mu": defaut_mu, "sigma": defaut_sigma, "n": 0, "mediane": None}
    med = statistics.median(ecarts)
    mad = statistics.median([abs(e - med) for e in ecarts]) * 1.4826 if n >= 3 else defaut_sigma
    w = n / (n + k)
    return {"mu": w * med + (1 - w) * defaut_mu, "sigma": max(w * mad + (1 - w) * defaut_sigma, 0.003),
            "n": n, "mediane": med}


def calculer(mem: Memoire, ctx: Contexte, candidats: list[dict], groupes: list[dict], *, moteur=None,
             composantes=None, poids: dict | None = None, reglages: dict | None = None,
             details: set[str] | None = None) -> dict:
    """Les profils de taux de chaque candidate envoyée, le segment, l'effet de marché et, pour les membres
    des groupes, les profils chef présent / absent et l'écart au chef. `details` : sociétés dont le
    sous-historique est envoyé (par défaut : la liste et les groupes)."""
    r = reglages or charger_reglages()
    n = ctx.n
    sims = Similarites(moteur, composantes, poids)
    seg = segment(mem, ctx, r)
    debut_mois = mem.coupe((mem.dates[n - 1][:6] + "00") if n else None) if n else 0
    em = effet_marche(mem, debut_mois, r)
    if details is None:
        details = {c["cle"] for c in candidats if c.get("dans_liste")} | {m for g in groupes for m in g["membres"]}
    societes = {}
    for c in candidats:
        societes[c["cle"]] = profil(mem, ctx, c["cle"], seg, sims, r, details=c["cle"] in details)
    par_groupe = []
    for g in groupes:
        chef = g["chef"]
        pc = societes.get(chef)
        presents_chef = set(Memoire.avant(mem.par_societe.get(chef, []), n))
        victoires = Memoire.avant(mem.victoires.get(chef, []), n)
        membres = []
        for m in g["membres"][1:]:
            pm = societes.get(m)
            if pm is None or pc is None:
                continue
            seg_m = {"moyenne": pm["mu"], "sigma": pm["sigma"]}
            avec = profil(mem, ctx, m, seg_m, sims, r, filtre=lambda i: i in presents_chef)
            sans = profil(mem, ctx, m, seg_m, sims, r, filtre=lambda i: i not in presents_chef)
            defaut = pm["mu"] - pc["mu"]
            ecart = _robuste(_ecarts(mem, n, m, chef, r, False), defaut, math.hypot(pm["sigma"], pc["sigma"]),
                             r["lissage_ecart"])
            ecart_v = _robuste(_ecarts(mem, n, m, chef, r, True), ecart["mu"], ecart["sigma"], r["lissage_ecart"])
            cand = next((c for c in candidats if c["cle"] == m), {})
            pres = sum(1 for i in victoires if m in mem.presents[i])
            k = r["lissage_presence"]
            q = (pres + k * float(cand.get("p") or 0.0)) / (len(victoires) + k)
            membres.append({"cle": m, "chef_present": _court(avec), "chef_absent": _court(sans),
                            "ecart": ecart, "ecart_victoires": ecart_v,
                            "presence_victoires": {"q": q, "presentes": pres, "victoires": len(victoires)}})
        sc = g.get("score_chef") or {}
        par_groupe.append({"numero": g.get("numero"), "chef": chef, "membres": membres,
                           "taux_victoire_chef": float(sc.get("taux_victoire") or 0.0),
                           "victoires_chef": len(victoires)})
    return {"societes": societes, "segment": seg, "effet_marche": em, "groupes": par_groupe,
            "reglages": {k: v for k, v in r.items()}}


def _court(p: dict) -> dict:
    return {k: p[k] for k in ("mu", "sigma", "n", "n_eff", "moyenne_brute")}
