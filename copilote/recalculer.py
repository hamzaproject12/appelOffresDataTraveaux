"""Recalcule les résultats du copilote dans copilote_calcul.db (base supprimable à tout moment).

    python -m copilote.recalculer groupes [--jusqua AAAA-MM-JJ] [--tirages 200]
    python -m copilote.recalculer similarite [--jusqua AAAA-MM-JJ]

--jusqua ne garde que les marchés (et les avis) datés jusqu'à ce jour inclus : sert au backtest.
Le calcul est rangé sous l'identifiant « courant » ou « jusqua-AAAA-MM-JJ » ; un recalcul remplace
les lignes du même identifiant. Les seuils des niveaux sont dans reglages.json (section « groupes »).
Ce module tourne dans un processus à part (planificateur, ligne de commande), jamais dans le serveur web.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

from . import bases
from . import fusions as _fusions
from . import groupes


def recalculer_groupes(jusqua: str | None = None, tirages: int = groupes.TIRAGES) -> str:
    debut = time.time()
    table_fusions = _fusions.charger()
    hist = bases.historique()
    try:
        d = groupes.charger(hist, jusqua, table_fusions)
    finally:
        hist.close()
    ret = bases.retours()
    try:
        avis = groupes.lire_avis(ret, jusqua, table_fusions)
    finally:
        ret.close()

    r = groupes.detecter(d, avis, tirages)
    extra = {}
    if avis:
        # L'accord se mesure sur le calcul SANS avis : avec, il serait d'accord par construction.
        extra["accord_avis"] = groupes.accord(groupes.detecter(d, {}, tirages), avis)

    calc = bases.calcul()
    try:
        calcul = groupes.enregistrer(calc, r, extra=extra)
    finally:
        calc.close()
    niveaux = Counter(p.niveau for p in r.paires if p.niveau)
    print(f"groupes « {calcul} » : {len(d.marches)} marchés, {len(d.societes)} sociétés, "
          f"{d.nb_fusions} fusions, {len(r.paires)} paires examinées (fort {niveaux['fort']}, "
          f"moyen {niveaux['moyen']}, faible {niveaux['faible']}), {len(r.groupes)} groupes, "
          f"{len(r.familles)} familles, {len(avis)} avis — {time.time() - debut:.0f} s", flush=True)
    return calcul


def recalculer_similarite(jusqua: str | None = None) -> str:
    """Refait l'index TF-IDF de l'objet et le range dans copilote_calcul.db (« courant » ou « jusqua-… »).

    Avec --jusqua, le vocabulaire et l'idf ne voient que les marchés datés jusqu'à ce jour inclus (backtest).
    """
    from . import similarite
    debut = time.time()
    calcul = "courant" if not jusqua else f"jusqua-{jusqua}"
    similarite.oublier()
    calc = bases.calcul()
    try:
        # On supprime l'ancien index : le recalcul doit se faire même si un index à jour existe.
        if _table_existe(calc, "sim_index"):
            calc.execute("DELETE FROM sim_index WHERE calcul = ?", (calcul,))
            calc.commit()
        limite = None
        if jusqua:
            limite = similarite.jour_de(jusqua)
            if limite is None:
                raise SystemExit("--jusqua : date attendue au format AAAA-MM-JJ")
            limite += 1
        m = similarite.moteur(bases.historique, bases.chemin_historique(), calc, calcul, limite, sauver=True)
    finally:
        calc.close()
    print(f"similarité « {calcul} » : {len(m.corpus)} marchés, {m.index.matrice.shape[1]} traits, "
          f"{m.index.matrice.nnz} valeurs — {time.time() - debut:.0f} s", flush=True)
    return calcul


def _table_existe(db, nom: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nom,)).fetchone() is not None


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="python -m copilote.recalculer")
    sous = p.add_subparsers(dest="quoi", required=True)
    g = sous.add_parser("groupes", help="groupes de présence commune")
    g.add_argument("--jusqua", help="AAAA-MM-JJ : ne garder que les marchés jusqu'à ce jour inclus")
    g.add_argument("--tirages", type=int, default=groupes.TIRAGES)
    sim = sous.add_parser("similarite", help="index de similarité de l'objet")
    sim.add_argument("--jusqua", help="AAAA-MM-JJ : idf et vocabulaire ajustés sur les marchés jusqu'à ce jour inclus")
    a = p.parse_args(argv)
    if a.quoi == "groupes":
        recalculer_groupes(a.jusqua, a.tirages)
    elif a.quoi == "similarite":
        recalculer_similarite(a.jusqua)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
