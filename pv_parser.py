"""Transformer le texte (OCR ou natif) d'un extrait de PV en données structurées.

Version 2 — construite et mesurée sur 50 PV réels d'acheteurs différents (ministères, ONEE,
SRM, communes, CHU, agences...). Aucune IA : uniquement des règles.

Principe
--------
Tous les extraits de PV suivent l'ordre fixé par le décret des marchés publics, avec des
formulations qui varient d'un acheteur à l'autre :

    titre + n° d'AO, objet, maître d'ouvrage, dates
    concurrents ayant déposé un pli            -> liste de noms
    concurrents écartés / évincés / admis        -> listes de noms (avec l'étape)
    montants des actes d'engagement              -> tableau nom + montant(s)
    vérification / rectification des montants    -> tableau nom + montant avant / après
    concurrent retenu / attributaire             -> tableau ou phrase
    justification, date d'achèvement, signature

1. Chaque ligne est classée : titre de section (reconnu par ses mots, avec ou sans « : »),
   en-tête de tableau, ou contenu.
2. Le contenu de chaque section est lu comme une liste de noms ou comme un tableau
   nom + montants (noms sur 2 lignes, numéros d'ordre, colonnes « Lot », « % »... gérés).
3. Les variantes d'un même nom (erreurs d'OCR) sont regroupées ; un nom qui ressemble à un
   titre de tableau ou à une phrase est rejeté plutôt qu'inventé.
4. L'attributaire vient du tableau « concurrent retenu », sinon d'une phrase
   (« la société X est retenue pour un montant de... », « offre présentée par X »).

Tout ce qui est douteux va dans `warnings` : un montant faux est pire qu'un montant absent.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

# =========================================================================== texte


def fold(text: str) -> str:
    """minuscules, sans accents, apostrophes unifiées."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[’‘`´]", "'", text).lower()


# Montants : 5 725 459,20 | 67.360,80 | 319.137,50 | 499084,00 | 1983 189,60 | 180 000.00 | 3 067 393.52
_SEP = r"[ .\u00a0\u202f]"
AMOUNT = re.compile(
    r"(?<![\d,.%])("
    r"\d{1,4}(?:" + _SEP + r"\d{3})+\s?[,.]\s?\d{2}"    # avec séparateurs de milliers
    r"|\d{3,}\s?,\s?\d{2}"                              # 499084,00
    r"|\d{1,3}(?:[ \u00a0\u202f]\d{3})+"                # 3 007 680 (sans centimes)
    r")(?!\s?%)(?![\d,])"
)


def to_number(raw: str) -> float | None:
    s = re.sub(r"[\s\u00a0\u202f]", "", raw)
    m = re.match(r"^([\d.,]*?)[.,](\d{2})$", s)
    if m:
        entier = re.sub(r"[.,]", "", m.group(1))
        s = f"{entier}.{m.group(2)}"
    else:
        s = re.sub(r"[.,]", "", s)
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def amounts_in(line: str) -> list[tuple[int, float]]:
    """(position, valeur) des montants plausibles d'une ligne (>= 1 000 DH, sauf s'il y a des centimes)."""
    # OCR : « I142 700,00 », « l 549 740,00 » -> 1
    line = re.sub(r"(?<![A-Za-z|])[Il](?=\s?\d{1,3}[ .]\d{3})", "1", line)
    line = line.replace("|", " ")                 # bordure de tableau, jamais un chiffre
    out = []
    for m in AMOUNT.finditer(line):
        v = to_number(m.group(1))
        if v is None:
            continue
        a_centimes = bool(re.search(r"[,.]\s?\d{2}$", m.group(1)))
        if v >= 1000:
            out.append((m.start(), v))
    return out


MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
          "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12}


def parse_date(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(20\d{2})", text)
    if m and 1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"
    m = re.search(r"(\d{1,2})(?:er)?\s+([a-z]+)\s+(20\d{2})", fold(text))
    if m and m.group(2) in MONTHS:
        return f"{int(m.group(1)):02d}/{MONTHS[m.group(2)]:02d}/{m.group(3)}"
    return None


# =========================================================================== noms de sociétés

LEGAL = re.compile(r"\b(ste|societe|sarl|sarlau|sa|sas|snc|au|groupement|gpt|cooperative|entreprise|"
                   r"ets|etablissements?|bureau|cabinet|architecte|laboratoire|group|groupe)\b")
# Mots qui ne font pas partie d'un nom de société : s'ils dominent la ligne, ce n'est pas un nom.
NOT_NAME = set("""liste montant montants concurrent concurrents soumissionnaire soumissionnaires neant aucun aucune
page verification verifications rectification rectifies rectifie engagement engagements commission justification
classement offre offres dossier dossiers attributaire attributaires designation objet date reserve reserves
avant apres ttc ht dh dhs dirhams mad montant total prix estimation lot lots nom noms retenu retenus ecarte ecartes
evince evinces admis admissibles admissible phase rejet pays taux majoration honoraires minimum maximum min max
administratif administratifs technique techniques financiere financieres examen issue pli plis depose deposes
note classement ordre chiffres lettres acte actes proposition architectes resultat decision concorrent concorrents motif motifs ecartement centime centimes
mille zero millions""".split())
STOPWORDS = set("""de des du la le les l d a au aux et en pour par sur avec dans un une est a ete ont qui que ce
cette son sa ses leur leurs ne pas se il elle ils ayant apres avant suite selon lors""".split())

_BULLET = re.compile(r"^(?:[\s\-–—_•·*>✓✔»«\"“”'#=°|:;.,~+©®]|[oe](?=\s)|Ÿ[”\"_]*|[vV][”\"]+_?|"
                     r"\d{1,2}\s?[.)\-](?!\d)|0\d(?=\s)|\d{1,2}(?=\s+(?:st[ée]|soci[ée]t[ée]|groupement|entreprise|ets)\b)|"
                     r"[eE][lL]\s?\d{1,2}\s?[:.,]|[a-hA-H]\)|(?:i|ii|iii|iv|v|vi)[.)](?=\s))+", re.I)


def strip_bullets(line: str) -> str:
    return _BULLET.sub("", line).strip()


def clean_name(text: str) -> str:
    t = strip_bullets(text.replace("\u00a0", " "))
    t = re.split(r";|\s:$", t, maxsplit=1)[0]                          # « NOM ; tampon lu par l'OCR »
    avant, sep, apres = t.partition(" : ")
    if sep and len(avant.split()) >= 2:                               # « STE X TRAVAUX : x lame) »
        t = avant
    t = re.sub(r"\((?:[^()]*\b(?:soumissionnaire|lot|d[ée]claration|manque|honneur)\b[^()]*)\)?", " ", t, flags=re.I)
    t = re.sub(r"\bpour (?:le|les|une?|la)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*,?\s*\b(?:sise?|sis|situ[ée]e?s?|domicili\w*|dont le si[eè]ge|adresse)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*-?\s*(?:et ce,?\s*)?pour (?:un|le) montant.*$", "", t, flags=re.I)
    t = re.sub(r"^\s*(?:unique|lot\s*(?:n\s*[°o]?\s*)?\d{1,2})\b[.:]?\s*", "", t, flags=re.I)
    t = re.sub(r"\(?\s*lots?\s*(?:n\s*[°o])?\s*[\d, et]+\)?\s*$", "", t, flags=re.I)
    t = re.sub(r"\s-\s*offre de base.*$", "", t, flags=re.I)
    t = re.sub(r"\s+(?:manque|d[ée]faut|absence|non conform|non[- ]respect|insuffisan)\w*\b.*$", "", t, flags=re.I)
    t = re.sub(r"\b(?:unique|maroc)\s*$", lambda m: m.group(0) if not re.search(r"unique", m.group(0), re.I) else "", t)
    t = re.sub(r"\b\d{1,2}(?:[,.]\d{1,2})?\s?%", " ", t)                 # taux de majoration / honoraires
    t = re.sub(r"\b(?:MAD|DHS?|DIRHAMS?|TTC|HT)\b\.?", " ", t, flags=re.I)
    t = strip_bullets(t)
    t = re.sub(r"^(?:la |le )?(?:soci[ée]t[ée]|ste\.?|st[ée]\.?)\s+(?=[A-Z0-9])", lambda m: m.group(0), t, flags=re.I)
    t = re.sub(r"^(?:la soci[ée]t[ée]|le groupement|l'entreprise|le concurrent|la ste)\s+", "", t, flags=re.I)
    t = re.sub(r"[\s|_\-–—.,;:!/\\*#=\[\]{}]+$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—_|.,;:\"'«»")
    # déchets en fin de ligne (cadre, tampon) : jetons d'une lettre minuscule ou de symboles
    toks = t.split()
    while len(toks) > 1 and (re.fullmatch(r"[^\w&]+|[a-zÈËÉ]|[A-Z]", toks[-1])
                             or len(re.findall(r"\d", toks[-1])) >= 3):
        toks.pop()
    return " ".join(toks)


VILLES = ("casablanca|casa|rabat|meknes|fes|marrakech|agadir|tanger|kenitra|oujda|tetouan|sale|temara|nador|safi|"
          "eljadida|laayoune|dakhla|essaouira|mohammedia|berkane|khouribga|settat|benimellal|errachidia|ouarzazate|"
          "taza|guelmim|larache|khemisset|benguerir|midelt|taroudant|tiznit|chtouka|berrechid|elkelaa|ifrane|"
          "azrou|sefrou|tiflet|skhirat|bouznika|dakhla|assa|zagora|tinghir|alhoceima|chefchaouen|maroc")
_VILLE_FIN = re.compile(r"(?:%s)+$" % VILLES)


def name_key(name: str) -> str:
    core = fold(name)
    core = re.sub(r"[^a-z0-9 ]", " ", core)
    core = LEGAL.sub(" ", core)
    k = re.sub(r"[^a-z0-9]", "", core)
    k2 = re.sub(r"(maroc)+$", "", k)          # colonne « Pays » collée au nom (ONEE) : « SULZER MAROC MAROC »
    k = k2 if len(k2) >= 3 else k
    return k or re.sub(r"[^a-z0-9]", "", fold(name))


def similar(a: str, b: str) -> float:
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    # même société, une fois avec la ville (« Sté. TRECQ MEKNES » / « Sté. TRECQ »)
    sa, sb = _VILLE_FIN.sub("", ka), _VILLE_FIN.sub("", kb)
    if sa == sb and len(sa) >= 4 and (sa != ka or sb != kb):
        return 0.95
    if min(len(ka), len(kb)) <= 6:          # sigles (LGC / LGCI) : l'égalité seule compte
        return 0.0
    court, long_ = sorted((ka, kb), key=len)
    if len(court) >= 8 and court in long_ and len(court) >= 0.6 * len(long_):
        return 0.9
    # nom tronqué par la largeur de colonne : « AXELI services Informatiques » / « AXELI Services Informatique et ... »
    prefixe = len(__import__("os").path.commonprefix([ka, kb]))
    if prefixe >= 8 and prefixe >= 0.9 * len(court):
        return 0.85                           # « NEXT GENERATION SERVI » tronqué par la marge
    return SequenceMatcher(None, ka, kb).ratio()


def looks_like_name(text: str) -> bool:
    """Vrai si `text` ressemble à un nom de société (et pas à un titre, une phrase ou du bruit)."""
    if not text or len(re.findall(r"[A-Za-zÀ-ÿ]", text)) < 3:
        return False
    f = fold(text)
    if re.search(r"\b(date|heure|ice|rc|if|patente)\s*:", f):
        return False
    if "@" in text or re.match(r"^(royaume|ministere|adresses?|e-?mail|tel|fax|www|http|province|wilaya|"
                                r"direction|delegation|agence nationale|office national)\b", f):
        return False
    words = re.findall(r"[a-z0-9&']+", f)
    if not words:
        return False
    if re.match(r"^(neant|aucun|aucune|idem|nb|n°|page|signe|fait a|le president|la presidente|date|objet)\b", f):
        return False
    bad = sum(w in NOT_NAME for w in words)
    stop = sum(w in STOPWORDS for w in words)
    legal = bool(LEGAL.search(f))
    if bad and (bad >= 2 or bad / len(words) > 0.3) and not legal:
        return False
    if bad >= 3:
        return False
    # une phrase : beaucoup de mots en minuscules et de mots outils
    lower_words = [w for w in re.findall(r"[A-Za-zÀ-ÿ']{3,}", text) if w.islower()]
    lettres = re.findall(r"[A-Za-zÀ-ÿ]", text)
    majuscules = sum(c.isupper() for c in lettres) / max(1, len(lettres))
    if (stop >= 4 and majuscules < 0.7) or (len(lower_words) >= 4 and len(lower_words) > len(words) / 2):
        return False
    if len(text) > 120:
        return False
    letters = re.findall(r"[A-Za-zÀ-ÿ]", text)
    if len(letters) < 0.45 * len(text.replace(" ", "")):
        return False                         # bruit d'OCR (symboles)
    return True


# =========================================================================== sections

REJECT = r"(ecart|evinc|elimin|rejet|non admis|non retenu|exclu)"


def header_of(line: str) -> tuple[str, str] | None:
    """Si la ligne est un titre de section : (section, texte après le titre) ; sinon None."""
    s = strip_bullets(line)
    f = fold(s)
    f = re.sub(r"\s+", " ", f)
    after = s.split(":", 1)[1].strip() if ":" in s else ""

    def res(section):
        return section, after

    if re.match(r"^(justification|critere du choix)", f):
        return res("justification")
    if re.match(r"^(date (et heure )?d.?\s?achevement|fait a\b|fait,|signe\b|signature|le president|pour le president)", f):
        return res("fin")
    if re.match(r"^(liste|noms?|concurrents?|soumissionnaires?|societes?|architectes?)\b.{0,60}"
                r"(ayant|avant|ryant|qui ont) (depose|soumissionne|remis|presente|deposes)", f) \
            or re.match(r"^(liste des )?(concurrents?|societes?) (ayant|avant) .{0,30}(depos|remis|soumission|present)", f):
        if "prospectus" in f or "documents techniques" in f:
            return res("autre")
        return res("deposants")
    tete_tableau = "montant" in f and not re.match(r"^(liste|montants?)\b", f)
    sans_titre = re.match(r"^(concurrents?|societes?|soumissionnaires?|architectes?)\b", f) and ":" not in s \
        and (len(f.split()) <= 3 or re.search(r"montant|engagement|\bavant\b|\bapres\b|rectif|ttc|offre", f))
    if re.search(r"retenus?\s*/\s*(ecart|evinc)", f):
        return res("admis")                  # tableaux « retenus / écartés » sur 2 colonnes
    if re.match(r"^(liste (des|de|du)|concurrents?|societes?|architectes?|soumissionnaires?)", f) and re.search(REJECT, f) \
            and not tete_tableau and not sans_titre:
        if "financ" in f:
            return res("ecartes_fin")
        if "administrati" in f:
            return res("ecartes_admin")
        if "techni" in f or "prospectus" in f or "projets" in f:
            return res("ecartes_tech")
        return res("ecartes")
    if re.match(r"^(liste (des|de|du)|les? |concurrents?|societes?|architectes?)", f) and \
            re.search(r"\b(admis|admissibles?|retenus? (a l.issue|apres))\b", f) and not tete_tableau \
            and not sans_titre:
        if "avec reserve" in f:
            return res("admis_avec")
        return res("admis")
    titre = ":" in s or re.search(r"\b(des|du|aux) (concurrents?|soumissionnaires?|offres?)\b", f)
    if re.match(r"^montant en (chiffres|lettres)", f):
        return None
    if re.match(r"^((la |le )?verification|montants? .{0,40}(rectifi|apres verif|apres rectif))", f) and \
            (titre or f.startswith("verification")):
        return res("verification")
    if re.match(r"^(classement|la note|l.evaluation|note globale|evaluation)", f):
        return res("autre")
    if (re.match(r"^(montants?|la teneur|taux d.honoraire|resultat d.ouverture|offres? financieres?|"
                 r"ouverture et analyse)", f) and titre) or \
            re.match(r"^liste des concurrents dont les offres financieres", f):
        if re.search(r"(taux d.honoraire|honoraires)", f):
            return res("montants")
        if re.search(r"\b(retenus?)\b", f) and not re.search(r"(concurrents|soumissionnaires) retenus", f):
            return res("retenu")
        if re.search(r"(concurrents|soumissionnaires) retenus\s*:?\s*$", f):
            return res("montants_ret")      # « retenus » = admis ou attributaire selon les PV
        return res("montants")
    if re.match(r"^(le |les |la )?(concurrents?|soumissionnaires?|societes?|architectes?|offres?)\s*(\(s\))?\s*"
                r"(reten\w*|attributaires?)\b", f) \
            or re.match(r"^(attributaires?|concurrent attributaire|resultat\s*:?$|resultat\s*:|decision de la commission|"
                        r"le concurrent retenu par)", f) \
            or re.match(r"^le\(s\) concurrent\(s\) retenu", f) or re.match(r"^soumissionnaire\(?r?\)? retenu", f):
        if re.search(r"(a l.issue|apres (evaluation|examen|etude))", f) and "verification arithmetique" not in f:
            return res("admis")
        return res("retenu")
    if re.match(r"^(liste (des|de|du))\b", f):
        return res("autre")
    return None


def is_table_header(line: str) -> bool:
    f = fold(line)
    words = re.findall(r"[a-z]+", f)
    if not words:
        return False
    header_words = NOT_NAME | STOPWORDS | {"en", "l", "d", "s", "nom", "des", "societe", "societes", "concurrent",
                                           "architecte", "architectes", "annuel", "an", "gg", "par", "the"}
    return sum(w in header_words for w in words) >= max(1, 0.7 * len(words)) and not LEGAL.search(
        f.replace("societe", "").replace("societes", ""))


FOOTER = re.compile(r"^(extrait (du|de) (p\.?\s?v|proces).{0,80}(page|\d\s*/\s*\d)|page \d+\s*(/|sur|de)\s*\d+|"
                    r"p a g e \d|\d+\s*/\s*\d+$|\d+\]\d+$)")


def _cle_ligne(line: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(line))


def prepare(pages: list[str]) -> list[str]:
    # En-tête / pied de page répétés (logo, ministère, adresse) : retirés, sinon ils passent pour des noms.
    repetes: set[str] = set()
    if len(pages) >= 2:
        vus: Counter = Counter()
        for page in pages:
            ls = [l for l in page.splitlines() if l.strip()]
            bords = ls[:14] + ls[-6:]
            vus.update({_cle_ligne(l) for l in bords if len(_cle_ligne(l)) >= 6})
        repetes = {k for k, n in vus.items() if n >= 2}
    lines = []
    for page in pages:
        ls = [l for l in page.splitlines() if l.strip()]
        bords = set(range(min(14, len(ls)))) | set(range(max(0, len(ls) - 6), len(ls)))
        for j, raw in enumerate(ls):
            line = re.sub(r"[ \t\u00a0]+", " ", raw).strip()
            if not line:
                continue
            if j in bords and _cle_ligne(line) in repetes and not re.search(r"\d{3}", line) \
                    and not header_of(line):
                continue
            f = fold(line)
            if FOOTER.match(f) or re.match(r"^(tel|fax|siege social|rc\s*:|i\.?c\.?e|www\.)", f):
                continue
            lines.append(line)
    return lines


def split_sections(lines):
    """[(section, [lignes])...] dans l'ordre + lignes d'en-tête (avant la 1re section)."""
    head, blocs = [], []
    current = None
    seen_amounts = False
    saute = False
    for i, line in enumerate(lines):
        if saute:
            saute = False
            continue
        h = header_of(line)
        if h is None and seen_amounts and current is not None and current[0] not in ("retenu", "montants", "verification") \
                and re.match(r"^concurrent\s+(retenu\s+)?montant", fold(line)):
            h = ("retenu", "")
            if current[1] and LOT_MARK.match(strip_bullets(current[1][-1])):
                lot_ligne = current[1].pop()
                current = ["retenu", [lot_ligne]]
                blocs.append(current)
                continue
        if (h is None or h[0] == "autre") and i + 1 < len(lines) and not line.rstrip().endswith(":"):
            # titre coupé sur 2 lignes : « Liste des concurrents » / « ayant déposé leurs plis : »
            h2 = header_of(line + " " + lines[i + 1])
            if h2 and h2[0] not in ("autre", (h or ("",))[0]) and re.match(r"^(ayant|avant|ryant|qui ont|a l.issue|"
                                                                         r"admis|ecart|evinc|elimin)", fold(lines[i + 1])):
                h, saute = h2, True
        if h:
            section, after = h
            if section == "ecartes":
                section = "ecartes_fin" if seen_amounts else "ecartes_admin"
            if section in ("montants", "montants_ret", "verification"):
                seen_amounts = True
            current = [section, []]
            blocs.append(current)
            if after and not re.match(r"^(neant|aucun|aucune|/)\b", fold(after)):
                current[1].append(after)
            continue
        if current is None:
            head.append(line)
        else:
            current[1].append(line)
    return head, blocs


# =========================================================================== lecture des sections

LOT_MARK = re.compile(r"^(?:>\s*)?(?:pour le )?lot\s*(?:n\s*[°o]?\s*)?(\d{1,2}|unique)\b", re.I)


NUMERO = re.compile(r"^\s*[-–—>•*]?\s*(\d{1,2})\s*[.)\-]?\s+(?=\S)")


def strip_numbering(lines: list[str]) -> list[str]:
    """Colonne « N° » d'un tableau (1 ADV MAROC / 2 BEAUTIFUL ...) : retirée si les numéros se suivent."""
    nums = [(i, int(m.group(1)), m.end()) for i, l in enumerate(lines) if (m := NUMERO.match(l))]
    if len(nums) == 1 and nums[0][1] == 1 and re.match(r"[A-Z]", lines[nums[0][0]][nums[0][2]:]):
        out = list(lines)
        out[nums[0][0]] = lines[nums[0][0]][nums[0][2]:]
        return out
    if len(nums) < 2:
        return lines
    suite = sum(1 for (_, a, _), (_, b, _) in zip(nums, nums[1:]) if b in (a + 1, 1))
    if nums[0][1] > 2 or suite < 0.6 * (len(nums) - 1):
        return lines
    out = list(lines)
    for i, _, fin in nums:
        out[i] = lines[i][fin:]
    return out


def _style_puce(line: str) -> str | None:
    m = re.match(r"^\s*(?:([-–—>•*»✓✔°+])|([oe])\s|(\d{1,2})\s*[.)\-]|(\d{1,2})\s+[A-Z]|([a-h])\))", line)
    if not m:
        return None
    if m.group(1) or m.group(2):
        return "sym"
    return "num" if (m.group(3) or m.group(4)) else "lettre"


def _a_puce(line: str, style: str | None = None) -> bool:
    st = _style_puce(line)
    return st is not None and (style is None or st == style)


def read_list(lines: list[str]) -> list[tuple[str, str | None]]:
    """[(nom, lot)] depuis une liste à puces."""
    out, lot = [], None
    lignes_utiles = [l for l in lines if l.strip()]
    styles = Counter(st for l in lignes_utiles if (st := _style_puce(l)))
    style = styles.most_common(1)[0][0] if styles else None
    avec_puces = sum(_a_puce(l, style) for l in lignes_utiles) >= max(2, 0.35 * len(lignes_utiles))
    prec_puce = False
    lines_orig = [l for l in lines]
    lines = strip_numbering(lines)
    for brute, line in zip(lines_orig, lines):
        # Liste à puces : une ligne sans puce est la suite de la précédente (adresse, fin de nom)
        if out and brute.lstrip().startswith("(") and not LEGAL.search(fold(brute).split(")")[-1] if ")" in brute else ""):
            out[-1] = (f"{out[-1][0]} {clean_name(line)}".strip(), out[-1][1])    # « ... SARL » + « (AU)-Oujda »
            continue
        debut_societe = re.match(r"^\W*(?:\w{1,2}\W+)?(ste|societe|sté|groupement|gpt|cabinet|entreprise|ets|bureau)\b",
                                 fold(brute)) or re.search(r"\bsarl\b", fold(brute))
        if avec_puces and not _a_puce(brute, style) and prec_puce and out and not debut_societe:
            suite = clean_name(line)
            if re.search(r"(\bET|&|\bDE|\bDU|-)\s*$", out[-1][0]) and looks_like_name(suite) \
                    and LEGAL.search(fold(suite)) is None or re.fullmatch(r"(?i)\s*(sarl|au|sa|a\.u)[\s.]*(au|a\.u)?\.?\s*", suite or "x"):
                out[-1] = (f"{out[-1][0]} {suite}".strip(), out[-1][1])
            continue
        prec_puce = _a_puce(brute, style)
        f = fold(strip_bullets(line))
        m = LOT_MARK.match(strip_bullets(line))
        if m and not re.search(r"[A-Z]{3,}.*[A-Z]{3,}", line[m.end():]):
            lot = m.group(1)
            continue
        if re.match(r"^(neant|aucun|aucune|/|idem)\b", f) or is_table_header(line):
            continue
        if amounts_in(line):
            continue
        if line.rstrip().endswith(":"):                           # suite d'un titre sur 2 lignes ?
            lettres = re.findall(r"[A-Za-zÀ-ÿ]", line)
            if sum(c.islower() for c in lettres) > 0.4 * max(1, len(lettres)):
                continue
        name = clean_name(line)
        if looks_like_name(name):
            out.append((name, lot))
    return out


def read_table(lines: list[str], known: list[str]) -> list[dict]:
    """Lignes nom + montants : [{nom, montants, lot}] ; gère les noms sur 2 lignes et les montants orphelins."""
    rows = []
    pending: str | None = None
    orphan: list[float] | None = None
    lot = None
    adresse = False                     # dans l'adresse d'un concurrent (« sise à ... ») : lignes ignorées
    for line in strip_numbering(lines):
        s = strip_bullets(line)
        f = fold(s)
        if adresse:
            if amounts_in(s):
                s = s[amounts_in(s)[0][0]:]            # montant en fin d'adresse
                f = fold(s)
                adresse = False
            elif (re.match(r"^(ste|societe|groupement|gpt|entreprise|ets|cabinet|bureau d.etudes|la societe)\b", f)
                  or re.search(r"\bsarl", f)) and looks_like_name(clean_name(s)):
                adresse = False
            else:
                continue
        if re.search(r"\b(?:sise?|sis|situ[ée]e?s?|domicili\w*|dont le si[eè]ge)\b", s, re.I) and not amounts_in(s):
            adresse = True
        m = LOT_MARK.match(s)
        if m:
            lot = m.group(1)
            rest = s[m.end():].strip(" :-")
            if not amounts_in(rest) and not looks_like_name(clean_name(rest)):
                continue
            s = rest
        if re.match(r"^(neant|aucun|aucune|idem)\b", f) or (s.rstrip().endswith(":") and not amounts_in(s)):
            continue
        am = amounts_in(s)
        name_part = clean_name(s[: am[0][0]] if am else s)
        has_name = looks_like_name(name_part) and not is_table_header(name_part)
        if am and not has_name:
            # « La maintenance ... des installations COMPTOIR FROID ET 19 200.00 » : un nom connu dans la ligne
            dedans = _known_in(s[: am[0][0]], known)
            if dedans:
                name_part, has_name = dedans, True
        vals = [v for _, v in am]
        if vals and has_name:
            if pending:
                joined = f"{pending} {name_part}"
                if _should_join(pending, name_part, joined, known):
                    name_part = joined
                elif orphan:
                    rows.append({"nom": pending, "montants": orphan, "lot": lot})
                pending = None
            rows.append({"nom": name_part, "montants": vals, "lot": lot})
            orphan = None
        elif vals:
            if pending:
                rows.append({"nom": pending, "montants": vals, "lot": lot})
                pending = None
            elif rows and not rows[-1]["montants"]:
                rows[-1]["montants"] = vals
            else:
                orphan = vals
        elif has_name:
            if orphan and not pending:
                rows.append({"nom": name_part, "montants": orphan, "lot": lot})
                orphan = None
            elif pending and _continuation(pending, name_part):
                pending = f"{pending} {name_part}"
            else:
                if pending:
                    rows.append({"nom": pending, "montants": [], "lot": lot})
                pending = name_part
    if pending:
        rows.append({"nom": pending, "montants": orphan or [], "lot": lot})
    return rows


def _known_in(text: str, known: list[str]) -> str | None:
    k = name_key(text)
    trouves = []
    for n in known:
        kn = name_key(n)
        tete = kn[: max(8, int(len(kn) * 0.7))]
        if len(kn) >= 6 and tete in k:
            trouves.append(n)
    return trouves[0] if len(trouves) == 1 else None


def _continuation(first: str, second: str) -> bool:
    if second.lstrip().startswith("("):          # « SOCIETE X » + « (SIGLE) SARL »
        return True
    if re.search(r"(\bET|&|\bDE|\bDU|\bD'|,|-|/|«|&\s*(?:STE|SOCIETE|STÉ)\.?)\s*$", first, flags=re.I):
        return True
    if re.fullmatch(r"(?:\s*\b(?:sarl|au|sa|sarlau|sas|snc)\b\s*)+", fold(second)):
        return True                              # « STE X » + « SARL AU »
    return len(second.split()) <= 2 and len(second) <= 12 and not LEGAL.search(fold(second))


def _should_join(first: str, second: str, joined: str, known: list[str]) -> bool:
    if _continuation(first, second):
        return True
    best = lambda n: max((similar(n, k) for k in known), default=0.0)
    return best(joined) >= 0.8 and best(joined) > max(best(first), best(second))


# =========================================================================== en-tête du PV

FIELD_START = re.compile(r"^(ma.tre d|date|lieu|journa|les journa|l.avis|liste|objet|obiet|montant|site|"
                         r"direction|representant|acheteur|reference|procedure|categorie|estimation|n°)", re.I)


def field(lines: list[str], label: str, suivante: bool = False) -> str | None:
    pat = re.compile(r"^" + label + r"\s*[:.]?\s*(.*)$", re.I)
    for i, line in enumerate(lines):
        s = strip_bullets(line)
        m = pat.match(fold(s))
        if not m:
            continue
        value = s[m.start(1):].strip(" :.-")
        if not value and suivante and i + 1 < len(lines):
            value = strip_bullets(lines[i + 1])
        for nxt in lines[i + 1: i + 3]:
            if not value or value.endswith((".", ";")) or FIELD_START.match(strip_bullets(nxt)) or \
                    header_of(nxt) or nxt.lstrip().startswith(("-", "•", "e ", "*")):
                break
            if len(nxt) > 90:
                break
            value += " " + nxt.strip()
        value = re.sub(r"\s+", " ", value).strip(" .;:")
        if value:
            return value
    return None


def read_head(lines: list[str]) -> dict:
    out: dict = {}
    folded = [fold(l) for l in lines[:40]]
    idx = next((i for i, l in enumerate(folded) if "extrait" in l or "proces" in l or "resultats definitifs" in l), 0)
    title = " ".join(lines[idx: idx + 4])
    m = re.search(r"\bn\s*[°º\"o*]?\s*(?:ao|a\.o\.o|d.?appel d.?offres)?\s*[:.]?\s*"
                  r"([A-Z0-9][A-Z0-9]*(?:\s*[/\-]\s*[A-Z0-9.\-]+)+|\d{6,})", title, re.I)
    num = re.sub(r"\s+", "", m.group(1)) if m else ""
    num = re.sub(r"-(?:objet|du|en|relatif|pour|portant|lance)\w*$", "", num, flags=re.I)
    out["numero_ao"] = num.strip("/.-") or None
    out["intitule"] = re.sub(r"\s+", " ", title)[:250] or None

    ft = fold(" ".join(lines[:25]))
    procedures = [
        ("concours architectural", r"concours architectural"),
        ("consultation architecturale", r"consultation architecturale"),
        ("appel d'offres ouvert simplifié", r"(appel d.?\s?offres?|a\.?o\.?o\.?) ouverts? (national )?simplifie|aoo simplifie|simplifie"),
        ("appel d'offres ouvert international", r"appel d.?\s?offres? ouvert international"),
        ("appel d'offres ouvert national", r"appel d.?\s?offres? ouvert national"),
        ("appel d'offres restreint", r"appel d.?\s?offres? restreint"),
        ("appel d'offres avec présélection", r"preselection"),
        ("appel d'offres ouvert", r"appel d.?\s?offres? ouvert|a\.?o\.?o"),
        ("marché négocié", r"negocie"),
    ]
    out["procedure"] = next((n for n, p in procedures if re.search(p, ft)), None)

    objet = field(lines[:60], r"[-•*]?\s*ob[jiyl]e[ct]\w*(?: de l.appel d.offres)?")
    if not objet:
        m = re.search(r"\b(relati[fv]e?s? (?:a|au|aux)\b.*)$", fold(title))
        if m:
            objet = re.sub(r"^relati[fv]e?s? (?:a|à|au|aux)\s+", "", title[m.start(1):], flags=re.I).strip(" :.")
    out["objet"] = objet

    mo = field(lines[:60], r"(?:representant du )?ma.tre d.?\s?ouvrage(?! delegue)", suivante=True) \
        or field(lines[:60], r"acheteur public")
    if mo:
        moitie = mo.split(" : ")                      # « DPETL Sefrou : DPETL Sefrou » (libellé répété)
        if len(moitie) == 2 and fold(moitie[0]).strip() == fold(moitie[1]).strip():
            mo = moitie[0]
        mo = re.split(r"\s[+\-•|]?\s*(?:\b[A-Z]\s?[a-z]{0,2}\s)?\b(?:date|lieu|journa\w*)\b", mo, maxsplit=1, flags=re.I)[0]
        mo = mo.strip(" +-/|.;:") or None
    if mo and re.match(r"^(signature|et cachet)", fold(mo)):
        mo = None
    out["maitre_ouvrage"] = mo
    out["date_ouverture_plis"] = parse_date(field(lines, r"date (?:et heure )?d.?\s?ouverture(?: des plis)?", True))
    out["lieu_ouverture_plis"] = field(lines, r"lieu d.?\s?ouverture(?: des plis)?", True)
    out["date_achevement_travaux_commission"] = parse_date(
        field(lines, r"date d.?\s?achevement[^:]*", True))
    return out


# =========================================================================== assemblage


class Bidders:
    def __init__(self):
        self.items: list[dict] = []

    def find(self, name: str) -> dict | None:
        mots = sorted(re.findall(r"[a-z0-9]{2,}", LEGAL.sub(" ", fold(name))))
        if len(mots) >= 2:
            for it in self.items:          # « Ahmed CHRAIJI » = « CHRAIJI Ahmed »
                if any(sorted(re.findall(r"[a-z0-9]{2,}", LEGAL.sub(" ", fold(v)))) == mots for v in it["_variants"]):
                    return it
        best, score = None, 0.0
        for it in self.items:
            s = max(similar(name, v) for v in it["_variants"])
            if s > score:
                best, score = it, s
        return best if score >= 0.8 else None

    def find_loose(self, name: str) -> dict | None:
        """find(), puis : un nom connu qui termine la chaîne lue (« opérations arithmétiques CID » -> CID)."""
        it = self.find(name)
        if it:
            return it
        k = name_key(name)
        cands = [i for i in self.items if any(len(name_key(v)) >= 3 and k.endswith(name_key(v)) for v in i["_variants"])]
        return cands[0] if len(cands) == 1 else None

    def get(self, name: str, create=True) -> dict | None:
        it = self.find_loose(name)
        if it:
            it["_variants"].append(name)
            return it
        if not create:
            return None
        it = {"_variants": [name], "_sections": set(), "_lots": set(), "montant_acte_engagement": None,
              "montant_apres_verification": None, "autres_montants": [], "statut": "depose"}
        self.items.append(it)
        return it

    def names(self):
        return [self.display(i) for i in self.items]

    @staticmethod
    def display(it):
        c = Counter(it["_variants"])
        return max(c, key=lambda v: (c[v], -len(re.findall(r"[^A-Za-z0-9 &'.\-/()À-ÿ]", v)), len(v)))


RETENU_PHRASE = re.compile(
    r"(?:la soci[ée]t[ée]|le groupement|l'entreprise|la ste|ste|le concurrent|concurrent retenu\s*:?)?\s*"
    r"(?P<nom>[A-Z0-9][A-Za-zÀ-ÿ0-9&'.\- /]{1,80}?)\s*(?:[,\-]\s*)?"
    r"(?:et ce,?\s*)?(?:est retenue?|,?\s*pour (?:un|le) montant|qui est d.un montant)", re.I)
PRESENTEE_PAR = re.compile(r"(?:offre|proposition)s?[^.]{0,60}?pr[ée]sent[ée]e?s? par\s*:?\s*(?:la soci[ée]t[ée]|"
                           r"la st[ée]|le groupement|l'entreprise|le concurrent)?\s*:?\s*"
                           r"(?P<nom>[A-Z0-9][A-Za-zÀ-ÿ0-9&'.\- /]{1,80}?)(?:\s*,|\s+est\b|\s+qui\b|$|\.)", re.I)


def parse(pages: list[str]):
    warnings: list[str] = []
    infos: list[str] = []
    lines = prepare(pages)
    head, blocs = split_sections(lines)
    data = read_head(lines)
    alltext = fold(" ".join(lines))

    bidders = Bidders()
    known = lambda: bidders.names()
    n_ref = lambda: sum("deposants" in i["_sections"] for i in bidders.items)

    # 1) listes de noms (déposants d'abord : c'est la liste de référence)
    order = ["deposants", "admis", "admis_avec", "ecartes_admin", "ecartes_tech", "ecartes_fin"]
    for wanted in order:
        for section, content in blocs:
            if section != wanted:
                continue
            for name, lot in read_list(content):
                # La liste des plis déposés fait référence : un nom des listes suivantes qui n'y
                # correspond pas est le plus souvent une ligne parasite (en-tête de page, tampon).
                if section != "deposants" and n_ref() >= 1 and not bidders.find_loose(name):
                    continue
                it = bidders.get(name)
                it["_sections"].add(section)
                if lot:
                    it["_lots"].add(lot)
    n_deposants = len(bidders.items)

    # 2) tableaux de montants
    tables_retenus = []
    for section, content in blocs:
        if section not in ("montants", "montants_ret", "verification"):
            continue
        lignes_tab = read_table(content, known())
        if section == "montants_ret":
            tables_retenus.append([r for r in lignes_tab if r["montants"]])
        for row in lignes_tab:
            creer = bool(row["montants"]) and (n_ref() == 0 or (
                len(row["nom"].split()) >= 2 and LEGAL.search(fold(row["nom"])) is not None))
            it = bidders.find_loose(row["nom"])
            if it is None and row["montants"] and bidders.items:
                memes = [i for i in bidders.items if i["montant_acte_engagement"] is not None and
                         any(abs(i["montant_acte_engagement"] - v) < 0.01 for v in row["montants"][:1])]
                if len(memes) == 1 and section != "montants":
                    it = memes[0]
            if it is None:
                it = bidders.get(row["nom"], create=creer)
            else:
                it["_variants"].append(row["nom"])
            if not it:
                continue
            it["_sections"].add(section)
            if row["lot"]:
                it["_lots"].add(row["lot"])
            vals = row["montants"]
            if not vals:
                continue
            if section in ("montants", "montants_ret"):
                if it["montant_acte_engagement"] is None:
                    it["montant_acte_engagement"] = vals[0]
                    it["autres_montants"] = vals[1:]
                elif row["lot"]:
                    it["autres_montants"].append(vals[0])
            else:
                if len(vals) >= 4:        # min / max, avant / après
                    after = vals[len(vals) // 2]
                else:
                    after = vals[-1]
                if it["montant_apres_verification"] is None:
                    it["montant_apres_verification"] = after
                if it["montant_acte_engagement"] is None:
                    it["montant_acte_engagement"] = vals[0]

    # 3) attributaire(s)
    attributaires = []           # [(nom, montant, lot)]
    for section, content in blocs:
        if section != "retenu":
            continue
        rows = [r for r in read_table(content, known()) if r["nom"]]
        colonnes_avant_apres = re.search(r"\bavant\b", fold(" ".join(content))) is not None
        avec_montant = [r for r in rows if r["montants"]]
        if bidders.items and len(avec_montant) > 1:
            connus = [r for r in avec_montant if bidders.find_loose(r["nom"])]
            if connus:
                avec_montant = connus        # lignes parasites (« et zéro centime ») écartées
        for r in (avec_montant or rows):
            vals = r["montants"]
            montant = (vals[-1] if colonnes_avant_apres and len(vals) == 2 else vals[0]) if vals else None
            nom = r["nom"]
            if not bidders.find_loose(nom) and montant is not None:
                # nom mal lu dans le tableau du retenu : on reconnaît le concurrent à son montant
                memes = [i for i in bidders.items if montant in (i["montant_acte_engagement"],
                                                                  i["montant_apres_verification"])]
                if len(memes) == 1:
                    nom = bidders.display(memes[0])
            if bidders.find_loose(nom) or montant is not None or LEGAL.search(fold(nom)) or not bidders.items:
                attributaires.append((nom, montant, r["lot"]))
        if not avec_montant:
            txt = " ".join(content)
            m = RETENU_PHRASE.search(txt) or PRESENTEE_PAR.search(txt)
            if m and looks_like_name(clean_name(m.group("nom"))):
                am = amounts_in(txt[m.end():][:200])
                phrase = (clean_name(m.group("nom")), am[0][1] if am else None, None)
                if not attributaires or not bidders.find_loose(attributaires[0][0]):
                    attributaires = [phrase]
        if not attributaires:
            txt = " ".join(content)
            m = RETENU_PHRASE.search(txt) or PRESENTEE_PAR.search(txt)
            if m:
                apres = txt[m.end():]
                am = amounts_in(apres[:120])
                attributaires.append((clean_name(m.group("nom")), am[0][1] if am else None, None))
    if not attributaires:
        joined = " ".join(lines)
        for rx in (RETENU_PHRASE, PRESENTEE_PAR):
            for m in rx.finditer(joined):
                nom = clean_name(m.group("nom"))
                if looks_like_name(nom) and (bidders.find(nom) or not bidders.items):
                    am = amounts_in(joined[m.end(): m.end() + 150])
                    attributaires.append((nom, am[0][1] if am else None, None))
                    break
            if attributaires:
                break
    # « Montant de l'acte d'engagement des concurrents retenus » avec une seule ligne : c'est l'attributaire
    if not attributaires and tables_retenus and len(tables_retenus[-1]) == 1:
        r = tables_retenus[-1][0]
        attributaires.append((r["nom"], r["montants"][0], r["lot"]))
    # justification : « Le concurrent X est mieux disant »
    for section, content in blocs:
        if section == "justification" and not attributaires:
            txt = " ".join(content)
            m = re.search(r"(?:le concurrent|la soci[ée]t[ée])\s+(?P<nom>[A-Z0-9][A-Z0-9&'.\- ]{2,60}?)\s+est\b", txt)
            if m and bidders.find(m.group("nom")):
                attributaires.append((m.group("nom"), None, None))

    infructueux = bool(re.search(r"infructueu(?!\w*\s*:?\s*neant)", alltext)) and not re.search(
        r"declaration infructueu\w*\s*:?\s*neant", alltext)

    attribs_out = []
    for nom, montant, lot in attributaires:
        nom = clean_name(nom)
        if not looks_like_name(nom):
            continue
        it = bidders.get(nom)
        it["_sections"].add("retenu")
        if lot:
            it["_lots"].add(lot)
        if montant is None:
            montant = it["montant_apres_verification"] or it["montant_acte_engagement"]
        elif it["montant_acte_engagement"] is None:
            it["montant_acte_engagement"] = montant
        attribs_out.append({"lot": lot, "attributaire": bidders.display(it), "montant": montant})
    # un même attributaire listé deux fois (tableau + phrase) : dédoublonner
    vus, uniques = set(), []
    for a in attribs_out:
        k = (a["lot"], name_key(a["attributaire"]))
        if k not in vus:
            vus.add(k)
            uniques.append(a)
    attribs_out = uniques
    if attribs_out:
        infructueux = infructueux and False if not re.search(r"(lot|lots).{0,40}infructueu", alltext) else infructueux

    # 4) statut de chaque concurrent
    soumissionnaires = []
    for it in bidders.items:
        s = it["_sections"]
        if "retenu" in s:
            statut = "attributaire"
        elif "ecartes_fin" in s:
            statut = "ecarte_offre_financiere"
        elif "ecartes_tech" in s:
            statut = "ecarte_offre_technique"
        elif "ecartes_admin" in s:
            statut = "ecarte_dossier_administratif_technique"
        elif s & {"admis", "admis_avec", "montants", "montants_ret", "verification"}:
            statut = "admis"
        else:
            statut = "depose"
        soumissionnaires.append({
            "nom": bidders.display(it),
            "montant_acte_engagement": it["montant_acte_engagement"],
            "montant_apres_verification": it["montant_apres_verification"],
            "autres_montants": it["autres_montants"],
            "lots": sorted(it["_lots"]),
            "statut": statut,
            "admis_avec_reserve": "admis_avec" in s,
        })

    # Montant aberrant (chiffres collés par l'OCR) : mieux vaut pas de montant qu'un faux.
    PLAFOND = 5e9
    for x in soumissionnaires:
        for champ in ("montant_acte_engagement", "montant_apres_verification"):
            if x[champ] is not None and x[champ] > PLAFOND:
                warnings.append(f"« {x['nom']} » : montant illisible ({x[champ]:,.0f} DH), écarté.".replace(",", " "))
                x[champ] = None
        x["autres_montants"] = [v for v in x["autres_montants"] if v <= PLAFOND]
    for a in attribs_out:
        if a["montant"] is not None and a["montant"] > PLAFOND:
            warnings.append(f"Montant de l'attributaire illisible ({a['montant']:,.0f} DH), écarté.".replace(",", " "))
            a["montant"] = None

    # 5) contrôles
    if not soumissionnaires and not infructueux:
        warnings.append("Aucun concurrent trouvé (document illisible, en arabe, tourné, ou qui n'est pas un PV).")
    if n_deposants:
        for x, it in zip(soumissionnaires, bidders.items):
            if "deposants" in it["_sections"]:
                continue
            texte = f"« {x['nom']} » absent de la liste des plis déposés (nom mal lu ?)."
            # Sans montant ni rôle d'attributaire, c'est presque toujours une ligne parasite du tableau :
            # on le signale sans marquer tout le PV « à vérifier ».
            if x["statut"] == "attributaire" or x["montant_acte_engagement"] is not None:
                warnings.append(texte)
            else:
                infos.append(texte)
    if not attribs_out and not infructueux and soumissionnaires:
        warnings.append("Attributaire non trouvé.")
    for x in soumissionnaires:
        a, b = x["montant_acte_engagement"], x["montant_apres_verification"]
        if a and b and abs(a - b) > 0.01:
            infos.append(f"« {x['nom']} » : montant rectifié de {a:,.2f} à {b:,.2f} DH.".replace(",", " "))
        if a and b and (b > 3 * a or a > 3 * b):
            warnings.append(f"« {x['nom']} » : écart anormal entre montant initial et vérifié (lecture à vérifier).")
    for a in attribs_out:
        if a["montant"] is None and not re.search(r"honoraire|\btaux\b|en %|proposition financiere de l.architecte", alltext):
            warnings.append(f"Montant de l'attributaire « {a['attributaire']} » non trouvé.")
    lots_vus = {l for x in soumissionnaires for l in x["lots"] if l != "unique"}
    if len(lots_vus) > 1:
        infos.append(f"Marché alloti ({len(lots_vus)} lots).")
    if not data["maitre_ouvrage"]:
        infos.append("Maître d'ouvrage absent du PV (utiliser l'acheteur public du portail).")
    if re.search(r"\bh\.?t\b|hors taxes", alltext) and not re.search(r"t\.?t\.?c", alltext):
        infos.append("Montants exprimés hors taxes (HT).")

    premier = attribs_out[0] if attribs_out else None
    data.update({
        "infructueux": infructueux,
        "attributaire": premier["attributaire"] if premier else None,
        "montant_attribue": premier["montant"] if premier else None,
        "attributaires_par_lot": attribs_out if len(attribs_out) > 1 else [],
        "justification": " ".join(" ".join(c) for s, c in blocs if s == "justification").strip()[:400] or None,
        "nombre_soumissionnaires": len(soumissionnaires),
        "soumissionnaires": soumissionnaires,
        "devise": "DH",
    })
    return data, warnings, infos
