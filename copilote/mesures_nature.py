"""Couverture des familles et de la nature sur l'historique, et objets non classés.

    python -m copilote.mesures_nature [--csv rapports/copilote_non_classes.csv]

Lecture seule. Les unités sont celles du copilote (un marché, ou un lot) ; le texte est celui de
l'index (objet, précédé de l'intitulé pour un lot).
"""
from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from contextlib import closing

from . import bases, config, corpus, nature
from .donnees import charger_unites

# Où l'objet cesse de décrire les travaux et commence à dire où ils ont lieu.
COUPURE = re.compile(r" (?:a la commune|a l arrondissement|au niveau|relevant|province|prefecture|commune|"
                     r"ville de|au douar|douar|sis |sise |au profit|dans la|en deux lots|en 2 lots|lot )")


def squelette(texte: str | None, mots: int = 8) -> str:
    """L'objet réduit à ce qu'on fait : sans nombres, coupé avant le lieu, au plus `mots` mots."""
    t = nature.plat(texte)
    t = COUPURE.split(" " + t, 1)[0]
    return " ".join([m for m in t.split() if not m.isdigit()][:mots])


def couverture(textes: list[str]) -> dict:
    fam, nat, aucun = Counter(), Counter(), 0
    non_classes = []
    for t in textes:
        f, n = nature.classer(t)
        fam[f] += 1
        nat[n] += 1
        if f is None:
            non_classes.append(t)
        if f is None and n is None:
            aucun += 1
    total = len(textes) or 1
    return {"total": len(textes), "famille_ok": total - fam[None], "nature_ok": total - nat[None],
            "aucun": aucun, "familles": fam, "natures": nat, "non_classes": non_classes,
            "taux_famille": (total - fam[None]) / total, "taux_nature": (total - nat[None]) / total}


def top_non_classes(non_classes: list[str], n: int = 100) -> list[tuple[str, int, str]]:
    """Les `n` squelettes d'objets non classés les plus fréquents, avec un exemple complet."""
    compte, exemple = Counter(), {}
    for t in non_classes:
        s = squelette(t)
        if s:
            compte[s] += 1
            exemple.setdefault(s, nature.plat(t)[:160])
    return [(s, k, exemple[s]) for s, k in compte.most_common(n)]


def mots_non_classes(non_classes: list[str], n: int = 40) -> list[tuple[str, int]]:
    """Les mots les plus fréquents des objets non classés (un mot compté une fois par objet)."""
    bruit = {"de", "du", "des", "la", "le", "les", "l", "d", "et", "en", "a", "au", "aux", "un", "une", "travaux",
             "province", "commune", "lot", "unique", "relevant", "direction", "provinciale", "region", "prefecture",
             "ville", "dans", "pour", "sur", "par", "sis", "sise", "ct", "douar", "niveau", "ait", "sidi", "ben"}
    c = Counter(m for t in non_classes for m in set(nature.plat(t).split()) if m not in bruit and not m.isdigit()
                and len(m) > 2)
    return c.most_common(n)


def mesurer() -> dict:
    with closing(bases.historique()) as db:
        unites = charger_unites(db)
        fiches = corpus.charger_fiches(db, unites)
    return couverture([f.texte for f in fiches.values()])


def main(argv: list[str]) -> int:
    chemin = argv[argv.index("--csv") + 1] if "--csv" in argv else str(config.RAPPORTS / "copilote_non_classes.csv")
    r = mesurer()
    print(f"{r['total']} unités ; famille reconnue {r['taux_famille']:.1%} ; nature reconnue {r['taux_nature']:.1%} ; "
          f"ni l'une ni l'autre : {r['aucun']}")
    for f, k in r["familles"].most_common():
        print(f"  {nature.libelle(f) or '(non classé)':32} {k:6}  {k / r['total']:.1%}")
    print("  nature : " + ", ".join(f"{nature.libelle(n) or 'non classée'} {k / r['total']:.1%}"
                                     for n, k in r["natures"].most_common()))
    top = top_non_classes(r["non_classes"])
    config.RAPPORTS.mkdir(exist_ok=True)
    with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["rang", "objet_reduit", "nombre", "exemple"])
        for i, (s, k, ex) in enumerate(top, 1):
            w.writerow([i, s, k, ex])
    print(f"{len(top)} objets non classés écrits dans {chemin}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
