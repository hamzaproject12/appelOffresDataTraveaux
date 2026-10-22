"""Mesures de l'étape 1 : concordance du rejeu avec les vrais gagnants, et bases simples 2026.

    python -m copilote.mesures [annee_bases_simples]        (2026 par défaut)

Aucune écriture : pv_travaux.db est ouverte en lecture seule.
"""
from __future__ import annotations

import sys

from . import bases
from .donnees import charger_unites, concordance
from .references import GRILLE, meilleur, par_tranche, taux_de_victoire


def pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f} %"


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")        # la console Windows est en cp1252
    annee = argv[0] if argv else "2026"
    db = bases.historique()
    try:
        unites = charger_unites(db)
    finally:
        db.close()
    lots = sum(u.lot is not None for u in unites)
    print(f"{len(unites)} unités ({len(unites) - lots} marchés ordinaires + {lots} lots)\n")

    c = concordance(unites)
    print("Concordance du rejeu avec le vrai gagnant")
    print("| période | unités | non attribuées | sans réf. | comparables | concordants | taux | réf. ≠ stockée |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for nom, b in (("global", c["global"]), *c["par_annee"].items()):
        print(f"| {nom} | {b['unites']} | {b['non_attribuees']} | {b['sans_reference']} | "
              f"{b['comparables']} | {b['concordants']} | {pct(b['taux'])} | {b['reference_differente']} |")

    print("\nPar nombre d'offres retenues (toutes années)")
    print("| offres | comparables | concordants | taux |")
    print("|---|---:|---:|---:|")
    for nom, b in c["par_tranche"].items():
        print(f"| {nom} | {b['comparables']} | {b['concordants']} | {pct(b['taux'])} |")

    print(f"\nBases simples : offre fictive à taux fixe, {annee}")
    for a in (annee, str(int(annee) - 1)):
        sous = [u for u in unites if u.annee == a]
        res = taux_de_victoire(sous)
        m = meilleur(res)
        print(f"\n{a} — n = {m[1]['n'] if m else 0}")
        print("| taux | victoires | taux de victoire |")
        print("|---:|---:|---:|")
        for r in GRILLE:
            print(f"| {r:.4f} | {res[r]['victoires']} | {pct(res[r]['taux_victoire'])} |")
        if m:
            print(f"meilleur taux fixe : {m[0]:.4f} -> {pct(m[1]['taux_victoire'])}")
        taux_cites = [GRILLE[0], 1.0, GRILLE[-1]]
        print(f"\n{a} par nombre d'offres reçues (avant la nôtre)")
        print("| offres | n | " + " | ".join(f"{r:.2f}" for r in taux_cites) + " |")
        print("|---|---:|" + "---:|" * len(taux_cites))
        for nom, ligne in par_tranche(sous, taux_cites).items():
            print(f"| {nom} | {ligne['n']} | " + " | ".join(pct(ligne[r]) for r in taux_cites) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
