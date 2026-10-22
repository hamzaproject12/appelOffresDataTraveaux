"""Région d'un acheteur, déduite de son nom.

La base n'a pas de colonne région. Le nom de l'acheteur en contient presque toujours une trace :
« REGION DE GUELMIM - OUED NOUN », « DIRECTION PROVINCIALE ... DE TAZA », « COMMUNE DE TEMARA ».
On cherche les mots-clés ci-dessous (villes, provinces, régions) dans le nom débarrassé de ses
accents ; le premier trouvé dans le texte l'emporte. Sinon, on essaie le lieu de l'acheteur dans
ao_ouverts.db. Sinon : None (région inconnue, comptée à part dans le rapport).

Sert seulement à mesurer la diversité des contextes d'un groupe : une erreur d'attribution de
région ne change qu'un compte.
"""
from __future__ import annotations

import re
import unicodedata

# Les 12 régions de 2015, plus « Nord (APDN) » : l'agence des préfectures et provinces du Nord
# couvre plusieurs régions, on la garde à part plutôt que de deviner.
MOTS: dict[str, tuple[str, ...]] = {
    "Tanger-Tétouan-Al Hoceïma": (
        "TANGER", "TANJA", "TETOUAN", "HOCEIMA", "HOCEMA", "LARACHE", "CHEFCHAOUEN", "CHAOUEN",
        "OUEZZANE", "OUAZZANE", "FNIDEQ", "MDIQ", "M DIQ", "MARTIL", "ASSILAH", "ANJRA", "LOUKKOUS",
        "LOUKKOS", "KSAR EL KEBIR", "KSAR KEBIR", "AMENDIS"),
    "Oriental": (
        "ORIENTAL", "OUJDA", "ANGAD", "NADOR", "BERKANE", "TAOURIRT", "TAOURIRTE", "JERADA",
        "FIGUIG", "BOUARFA", "GUERCIF", "DRIOUCH", "DRIOUECH", "SAIDIA", "MARCHICA", "MOULOUYA",
        "MELOUIA", "ZEGZEL"),
    "Fès-Meknès": (
        "FES", "MEKNES", "IFRANE", "TAZA", "SEFROU", "HAJEB", "BOULMANE", "TAOUNATE",
        "MOULAY YACOUB", "SEBOU", "SAIS"),
    "Rabat-Salé-Kénitra": (
        "RABAT", "SALE", "KENITRA", "KHEMISSET", "TEMARA", "SKHIRAT", "SIDI KACEM",
        "SIDI SLIMANE", "BOUREGREG", "GHARB", "ZEMMOUR", "ZEMOUR", "ZAIR", "ZAER", "OULMES",
        "TIFLET", "AIN AOUDA"),
    "Béni Mellal-Khénifra": (
        "BENI MELLAL", "BENIMELLAL", "KHENIFRA", "AZILAL", "KHOURIBGA", "FQUIH", "TADLA",
        "DEMNATE", "OUAOULA", "OUM ER RBIA", "OUM ERRABIA"),
    "Casablanca-Settat": (
        "CASABLANCA", "CASA", "SETTAT", "MOHAMMEDIA", "MOHAMMADIA", "JADIDA", "BERRECHID",
        "BENSLIMANE", "BEN SLIMANE", "NOUACEUR", "MEDIOUNA", "BENNOUR", "CHAOUIA", "DOUKKALA",
        "ANFA", "BERNOUSSI", "AIN SEBAA", "HAY MOHAMMADI", "HAY HASSANI", "JORF", "ZMAMRA",
        "DEROUA", "MOULAY ABDELLAH"),
    "Marrakech-Safi": (
        "MARRAKECH", "SAFI", "HAOUZ", "ELHAOUZ", "CHICHAOUA", "ESSAOUIRA", "SRAGHNA",
        "KELAA DES SRAGHNA", "RHAMNA", "YOUSSOUFIA", "BENGUERIR", "BEN GUERIR", "TENSIFT",
        "MENARA", "TAMESLOHTE", "CADI AYYAD"),
    "Drâa-Tafilalet": (
        "DRAA", "DARAA", "TAFILALET", "ERRACHIDIA", "OUARZAZATE", "OUARZAZAT", "ZAGORA",
        "ZAGOURA", "TINGHIR", "MIDELT", "RISSANI", "ALNIF", "FERKLA", "RHERRISS", "GOULMIMA",
        "KELAAT M GOUNA"),
    "Souss-Massa": (
        "SOUSS", "MASSA", "AGADIR", "INEZGANE", "INZEGANE", "TAROUDANT", "TIZNIT", "CHTOUKA",
        "TATA", "TAFRAOUT", "IDAOUTANANE", "IDA OUTANANE", "OUTANANE", "TAMRI", "AIT MELLOUL",
        "BELQUASSEM", "IBN ZOHR"),
    "Guelmim-Oued Noun": (
        "GUELMIM", "QUELMIM", "OUED NOUN", "SIDI IFNI", "IFNI", "TANTAN", "TAN TAN", "ASSA ZAG"),
    "Laâyoune-Sakia El Hamra": (
        "LAAYOUNE", "SAKIA", "BOUJDOUR", "SMARA", "SEMARA", "TARFAYA", "EL MARSA"),
    "Dakhla-Oued Ed-Dahab": ("DAKHLA", "EDDAHAB", "ED DAHAB", "AOUSSERD"),
    "Nord (APDN)": ("PROVINCES DU NORD",),
}

# Un seul motif : le plus long mot-clé d'abord, pour que « SIDI SLIMANE » passe avant « SALE »…
_PAR_MOT = {mot: region for region, mots in MOTS.items() for mot in mots}
_MOTIF = re.compile(r"\b(" + "|".join(re.escape(m) for m in sorted(_PAR_MOT, key=len, reverse=True))
                    + r")\b")


def _plat(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte or "")
    texte = "".join(ch for ch in texte if not unicodedata.combining(ch)).upper()
    return " ".join(re.sub(r"[^A-Z]+", " ", texte).split())


def region_de(*textes: str | None) -> str | None:
    """Région du premier texte qui en donne une (nom de l'acheteur, puis lieu)."""
    for texte in textes:
        m = _MOTIF.search(_plat(texte or ""))
        if m:
            return _PAR_MOT[m.group(1)]
    return None
