"""Étape D : backtest de la décision (règle « GO + prix recommandé ») sur 2026.

    python -m copilote.mesures_decision courbes [--dossier D]   garde les courbes de chance et les vraies issues
                                                                 de 2026 (500 marchés) et de 2025 (300 marchés)
    python -m copilote.mesures_decision backtest [--dossier D]  seuils choisis sur 2025, mesure sur 2026
                                                                 → rapports/copilote_etape_D_mesures.json

Sans fuite : la chance de chaque marché vient du simulateur de C2 (historique d'avant le jour du marché) ; les seuils
« GO » et les taux fixes de comparaison sont choisis sur 2025 seulement ; 2026 ne sert qu'à mesurer. L'issue réelle de
notre offre fictive est rejouée parmi les vraies offres du PV avec la règle de construire_base (donnees.rejouer_attribution).

Coûts fictifs : 0,80, 0,85 et 0,90 × l'estimation, marge minimale 5 % du coût. Dans le backtest, la décision ne voit que
le gain espéré, la chance, la marge et la nature du marché (pas l'historique de l'acheteur ni les groupes).
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np

from . import config, decision, mesures_simulation as ms

COUTS = (0.80, 0.85, 0.90)            # coûts fictifs, en part de l'estimation
MARGE_MIN = 0.05
XS = [(1600 + j) / 2000 for j in range(801)]
FIXES = ms.FIXES
TIERS = (5.5, 10.5)                   # tiers de nombre prévu de concurrents (ceux de la calibration de C2)
SEUILS_GAIN = (0.005, 0.01, 0.015, 0.02, 0.03)
SEUILS_CHANCE = (0.05, 0.10, 0.15, 0.20, 0.30)
CHAMPS = ("courbe", "gagne_grille", "fixes", "tranche", "nb_offres", "nombre_prevu", "identifiant", "estimation",
          "attribue")


def courbes(dossier: Path) -> None:
    """Rejoue le backtest de C2 en gardant, pour chaque marché attribué, la courbe de chance calibrée, l'issue réelle
    d'une offre fictive à chaque taux de la grille, et l'estimation (pour compter en dirhams)."""
    dernier = {}
    verite0, un0 = ms.verite_unite, ms.mesurer_un

    def verite(u):
        dernier["estimation"] = u.estimation
        return verite0(u)

    def un(s, v, base4):
        ligne = un0(s, v, base4)
        ligne["estimation"] = dernier.get("estimation")
        return ligne

    ms.verite_unite, ms.mesurer_un = verite, un
    try:
        for annee, n, graine, hors in (("2026", 500, 1, False), ("2025", 300, 13, True)):
            r = ms.mesurer(annee, n, graine, hors_echantillon=hors)
            lignes = [{k: l.get(k) for k in CHAMPS} for l in r["_lignes"] if "courbe" in l]
            chemin = dossier / f"decision_courbes_{annee}.pkl"
            with open(chemin, "wb") as f:
                pickle.dump(lignes, f)
            print(f"{annee} : {len(lignes)} marchés attribués → {chemin}", flush=True)
    finally:
        ms.verite_unite, ms.mesurer_un = verite0, un0


# --------------------------------------------------------------------------- la règle et ses points de comparaison

def _tiers(prevu: float | None) -> int:
    return 0 if prevu is None or prevu < TIERS[0] else (1 if prevu < TIERS[1] else 2)


def _compte() -> dict:
    return {"joues": 0, "victoires": 0, "gain_dh": 0.0, "gain_points": 0.0}


def _ajouter(r: dict, gagne: bool, x: float, c: float, estimation: float) -> None:
    r["joues"] += 1
    if gagne:
        r["victoires"] += 1
        r["gain_dh"] += (x - c) * estimation
        r["gain_points"] += x - c


def _fin(r: dict, total: int) -> dict:
    return {**r, "gain_dh": round(r["gain_dh"]), "gain_points": round(r["gain_points"], 4),
            "taux_victoire": (r["victoires"] / r["joues"]) if r["joues"] else None,
            "gain_par_ao_joue_dh": round(r["gain_dh"] / r["joues"]) if r["joues"] else None, "marches": total}


def jouer_regle(lignes: list[dict], c: float, reg: dict, jouer=(decision.GO,)) -> dict:
    """La règle « décision + prix recommandé » : on joue au prix recommandé quand la décision fait partie de `jouer`."""
    r, petits, gros = _compte(), _compte(), _compte()
    decisions = {decision.GO: 0, decision.ETUDIER: 0, decision.NOGO: 0}
    for l in lignes:
        E = l["estimation"] or 1.0
        ev = decision.evaluer(XS, l["courbe"], E, c * E, MARGE_MIN, reg)
        d = decision.decider(ev, None, {"prevu": l["nombre_prevu"]}, reg)["decision"]
        decisions[d] += 1
        if d in jouer and ev["reco"]:
            x = ev["reco"]["x"]
            g = bool(l["gagne_grille"][decision.indice_de(XS, x)])
            _ajouter(r, g, x, c, E)
            _ajouter(petits if (l["nombre_prevu"] or 0) < reg["petit_marche"] else gros, g, x, c, E)
    return {**_fin(r, len(lignes)), "decisions": decisions, "petits_marches": _fin(petits, 0), "gros_marches": _fin(gros, 0)}


def toujours(lignes: list[dict], x: float, c: float) -> dict:
    r = _compte()
    j = decision.indice_de(XS, x)
    for l in lignes:
        _ajouter(r, bool(l["gagne_grille"][j]), x, c, l["estimation"] or 1.0)
    return _fin(r, len(lignes))


def _admis(c: float) -> list[int]:
    xmin = max(decision.charger_reglages()["plage"]["plancher"], c * (1 + MARGE_MIN))
    return [k for k, x in enumerate(FIXES) if x >= xmin - 1e-12]


def meilleur_fixe_2025(lignes_2025: list[dict], c: float) -> dict:
    """Le taux fixe (global, et par tiers de nombre prévu) qui maximise le gain total de 2025 pour ce coût, avec la même
    contrainte que la règle : jamais sous le plancher, jamais sous coût × (1 + marge minimale)."""
    adm = _admis(c)

    def meilleur(sous):
        if not sous or not adm:
            return None
        gains = np.zeros(len(adm))
        taux = np.array([FIXES[k] for k in adm])
        for l in sous:
            g = np.array([bool(l["fixes"][k]) for k in adm], dtype=float)
            gains += g * (taux - c) * (l["estimation"] or 1.0)
        return FIXES[adm[int(np.argmax(gains))]]

    return {"global": meilleur(lignes_2025),
            "tiers": [meilleur([l for l in lignes_2025 if _tiers(l["nombre_prevu"]) == t]) for t in range(3)]}


LIBELLES_TRANCHES = ("petit marché (moins de 5,5 concurrents prévus)", "marché moyen (5,5 à 10,5 concurrents prévus)",
                     "gros marché (plus de 10,5 concurrents prévus)")
REGLE_SIMPLE = config.ICI / "copilote" / "regle_simple_2025.json"


def apprendre_regle_simple(lignes_2025: list[dict]) -> dict:
    """La « règle simple » affichée à l'étape ⑥ : pour chaque tranche de nombre prévu de concurrents (et pour tous), la part
    des estimations gagnées à chaque taux fixe de la grille, sur 2025. Le meilleur taux fixe d'un coût c est ensuite le
    maximum de (taux − c) × w(taux) sur les taux permis : c'est exactement le choix de `meilleur_fixe_2025`, pour n'importe
    quel coût (le fichier n'est donc pas lié aux trois coûts fictifs)."""
    def w(sous):
        total = sum((l["estimation"] or 1.0) for l in sous)
        return [round(sum((l["estimation"] or 1.0) * bool(l["fixes"][k]) for l in sous) / total, 6) if total else 0.0
                for k in range(len(FIXES))]

    tranches = []
    for t in range(3):
        sous = [l for l in lignes_2025 if _tiers(l["nombre_prevu"]) == t]
        tranches.append({"borne": TIERS[t] if t < 2 else None, "libelle": LIBELLES_TRANCHES[t], "marches": len(sous), "w": w(sous)})
    return {"_aide": "Règle simple apprise sur 2025 (python -m copilote.mesures_decision regle-simple). w[k] = part des estimations "
                     "gagnées en jouant le taux fixe taux[k] sur les marchés 2025 de la tranche. Meilleur taux fixe d'un coût c = "
                     "maximum de (taux − c) × w sur les taux entre max(plancher ; c × (1 + marge minimale)) et le plafond.",
            "appris_sur": "2025", "taux": FIXES, "tranches": tranches,
            "tout": {"libelle": "tous les marchés", "marches": len(lignes_2025), "w": w(lignes_2025)}}


def jouer_fixe(lignes: list[dict], taux, c: float) -> dict:
    """Un taux fixe pour tous (nombre) ou un taux par tiers de nombre prévu (liste de 3)."""
    r = _compte()
    for l in lignes:
        x = taux if not isinstance(taux, list) else taux[_tiers(l["nombre_prevu"])]
        if x is None:
            continue
        _ajouter(r, bool(l["fixes"][FIXES.index(x)]), x, c, l["estimation"] or 1.0)
    return _fin(r, len(lignes))


def avec_seuils(reg: dict, gain_go: float, chance_go: float) -> dict:
    return {**reg, "gain": {**reg["gain"], "go": gain_go}, "chance": {**reg["chance"], "go": chance_go}}


def choisir_seuils(lignes_2025: list[dict]) -> tuple[dict, list]:
    """Les seuils « GO » (gain, chance) choisis sur 2025 : le couple qui maximise le gain total (DH) des trois coûts
    fictifs ; à égalité, le plus exigeant."""
    reg = decision.charger_reglages()
    essais = []
    for g in SEUILS_GAIN:
        for ch in SEUILS_CHANCE:
            total = sum(jouer_regle(lignes_2025, c, avec_seuils(reg, g, ch))["gain_dh"] for c in COUTS)
            essais.append({"gain_go": g, "chance_go": ch, "gain_total_dh": total})
    meilleur = max(essais, key=lambda e: (e["gain_total_dh"], e["gain_go"], e["chance_go"]))
    return meilleur, essais


def backtest(dossier: Path, seuils: dict | None = None) -> dict:
    with open(dossier / "decision_courbes_2026.pkl", "rb") as f:
        l26 = pickle.load(f)
    with open(dossier / "decision_courbes_2025.pkl", "rb") as f:
        l25 = pickle.load(f)
    reg0 = decision.charger_reglages()
    choix, essais = choisir_seuils(l25) if seuils is None else (seuils, [])
    reg = avec_seuils(reg0, choix["gain_go"], choix["chance_go"])
    fixes_c2 = ms.taux.charger_simulation().get("fixes_2025", {})
    sortie = {"marches_2026": len(l26), "marches_2025": len(l25), "marge_min": MARGE_MIN, "seuils_choisis_sur_2025": choix,
              "essais_2025": essais, "couts": {}}
    ok_total = 0
    for c in COUTS:
        fixe = meilleur_fixe_2025(l25, c)
        regle = jouer_regle(l26, c, reg)
        etude = jouer_regle(l26, c, reg, jouer=(decision.GO, decision.ETUDIER))
        a = toujours(l26, 0.95, c)
        fg = jouer_fixe(l26, fixe["global"], c)
        ft = jouer_fixe(l26, fixe["tiers"], c)
        c2 = fixes_c2.get("tout", {}).get("taux")
        fc2 = jouer_fixe(l26, c2, c) if c2 in FIXES else None
        ok = regle["gain_dh"] >= max(a["gain_dh"], fg["gain_dh"], ft["gain_dh"])
        ok_total += ok
        sortie["couts"][f"{c:.2f}"] = {
            "regle_go": regle, "regle_go_ou_a_etudier": etude, "toujours_0_95": a,
            "taux_fixe_2025_global": {"taux": fixe["global"], **fg}, "taux_fixe_2025_par_tiers": {"taux": fixe["tiers"], **ft},
            "taux_fixe_c2_meilleure_victoire": None if fc2 is None else {"taux": c2, **fc2},
            "regle_au_moins_aussi_bonne_que_les_trois": ok}
    sortie["feu_vert"] = {"couts_ok": ok_total, "sur": len(COUTS), "ok": ok_total >= 2}
    return sortie


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m copilote.mesures_decision")
    p.add_argument("commande", choices=("courbes", "backtest", "regle-simple"))
    p.add_argument("--dossier", default=str(config.ICI / "rapports"))
    p.add_argument("--gain-go", type=float)
    p.add_argument("--chance-go", type=float)
    a = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    dossier = Path(a.dossier)
    dossier.mkdir(parents=True, exist_ok=True)
    if a.commande == "courbes":
        courbes(dossier)
        return 0
    if a.commande == "regle-simple":
        with open(dossier / "decision_courbes_2025.pkl", "rb") as f:
            r = apprendre_regle_simple(pickle.load(f))
        REGLE_SIMPLE.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        print("→", REGLE_SIMPLE, [t["marches"] for t in r["tranches"]])
        return 0
    seuils = {"gain_go": a.gain_go, "chance_go": a.chance_go} if a.gain_go is not None and a.chance_go is not None else None
    r = backtest(dossier, seuils)
    sortie = config.RAPPORTS / "copilote_etape_D_mesures.json"
    sortie.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in r.items() if k != "essais_2025"}, ensure_ascii=False, indent=1))
    print("→", sortie)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
