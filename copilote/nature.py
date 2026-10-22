"""Nature des travaux : famille (route, assainissement, bâtiment…) et nature (neuf / réhabilitation /
entretien), par règles de mots-clés lues dans nature_mots.json.

Le texte est mis à plat comme dans le reste du site : minuscules, accents retirés, tout ce qui n'est
pas une lettre ou un chiffre devient une espace (« d'art » → « d art »). Les mots-clés subissent le
même traitement, on peut donc les écrire avec leurs accents dans le JSON.

Aucun LLM, aucun modèle : une règle claire, que l'on corrige en ajoutant un mot au fichier JSON.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

FICHIER = Path(__file__).with_name("nature_mots.json")
NATURES = ("neuf", "rehabilitation", "entretien")
BATIMENT_NEUF = "batiment_neuf"
BATIMENT_REHAB = "batiment_rehabilitation"


def plat(texte: str | None) -> str:
    """« Travaux d'aménagement de l'École » → « travaux d amenagement de l ecole »."""
    t = unicodedata.normalize("NFKD", (texte or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


class Motif:
    """Un ensemble de mots-clés, cherchés mot entier par mot entier (le pluriel en s/x est accepté).

    Plus rapide qu'une grosse expression régulière : 20 000 objets se classent en une seconde.
    `premiere(t)` renvoie la position du premier mot-clé trouvé dans le texte à plat `t`, ou None.
    """

    def __init__(self, mots: list[str]):
        self.exacts: set[str] = set()
        self.prefixes: list[str] = []
        self.phrases: list[tuple[str, bool]] = []      # (expression, accepte une suite)
        for mot in mots:
            suite = mot.rstrip().endswith("*")
            m = plat(mot.replace("*", " "))
            if not m:
                continue
            if " " in m:
                self.phrases.append((m, suite))
            elif suite:
                self.prefixes.append(m)
            else:
                self.exacts.add(m)
        self.prefixes_t = tuple(self.prefixes)

    def premiere(self, t: str) -> int | None:
        meilleur, pos = None, 0
        for mot in t.split(" "):
            if (mot in self.exacts or (mot[-1:] in ("s", "x") and mot[:-1] in self.exacts)
                    or (self.prefixes_t and mot.startswith(self.prefixes_t))):
                meilleur = pos
                break
            pos += len(mot) + 1
        if self.phrases:
            bloc = " " + t + " "
            for phrase, suite in self.phrases:
                i = bloc.find(" " + phrase)
                while i >= 0:
                    fin = i + 1 + len(phrase)
                    if suite or bloc[fin:fin + 1] == " " or bloc[fin:fin + 2] in ("s ", "x "):
                        if meilleur is None or i < meilleur:
                            meilleur = i
                        break
                    i = bloc.find(" " + phrase, i + 1)
        return meilleur

    def search(self, t: str) -> bool:
        return self.premiere(t) is not None


@lru_cache(maxsize=1)
def regles(chemin: str | None = None) -> dict:
    """Le fichier JSON, compilé. Mis en cache : appeler regles.cache_clear() après l'avoir modifié."""
    with open(chemin or FICHIER, encoding="utf-8") as f:
        brut = json.load(f)
    familles = {nom: {"libelle": d["libelle"], "motif": Motif(d["mots"])}
                for nom, d in brut["familles"].items()}
    nature = {nom: {"libelle": d["libelle"], "motif": Motif(d["mots"]),
                    "secondaire": Motif(d.get("secondaires", []))}
              for nom, d in brut["nature"].items()}
    return {"ordre": [n for n in brut["ordre"] if n in familles], "familles": familles, "nature": nature}


def libelle(code: str | None) -> str | None:
    if not code:
        return None
    r = regles()
    if code == BATIMENT_NEUF:
        return "Bâtiment neuf"
    if code == BATIMENT_REHAB:
        return "Réhabilitation de bâtiment"
    if code in r["familles"]:
        return r["familles"][code]["libelle"]
    if code in r["nature"]:
        return r["nature"][code]["libelle"]
    return code


def nature_de(texte: str | None) -> str | None:
    """neuf / rehabilitation / entretien : le mot principal cité le plus tôt l'emporte, sinon un mot
    secondaire ; None si rien ne se reconnaît."""
    t = plat(texte)
    r = regles()["nature"]
    for cle in ("motif", "secondaire"):
        meilleur, position = None, None
        for nom in NATURES:
            motif = r[nom][cle]
            pos = motif.premiere(t)
            if pos is not None and (position is None or pos < position):
                meilleur, position = nom, pos
        if meilleur:
            return meilleur
    return None


def famille_de(texte: str | None, nature: str | None = None) -> str | None:
    """La première famille (dans l'ordre du fichier) dont un mot-clé figure dans l'objet.

    Le bâtiment se range en « batiment_rehabilitation » quand la nature est réhabilitation ou
    entretien, en « batiment_neuf » sinon. None si rien ne se reconnaît.
    """
    t = plat(texte)
    r = regles()
    for nom in r["ordre"]:
        if r["familles"][nom]["motif"].search(t):
            if nom == "batiment":
                return BATIMENT_REHAB if nature in ("rehabilitation", "entretien") else BATIMENT_NEUF
            return nom
    return None


def classer(texte: str | None) -> tuple[str | None, str | None]:
    """(famille, nature) d'un objet."""
    nature = nature_de(texte)
    return famille_de(texte, nature), nature
