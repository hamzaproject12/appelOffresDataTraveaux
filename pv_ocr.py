"""Lecture du texte d'un PV page par page (PDF texte, PDF scanné ou image).

Les PV publiés sur marchespublics.gov.ma sont presque tous des scans. Trois pièges, traités ici :

1. Les tableaux à bordures : les traits sont lus comme « | », « l » ou « 1 » collés aux montants,
   ou font sauter tout le tableau. On efface les traits (morphologie) avant l'OCR, ce qui laisse
   chaque ligne du tableau comme une ligne de texte : « SOCIETE X SARL   54 000,00 DH ».
2. Les pages scannées de travers ou à l'envers (90°, 180°, 270°) : si le texte lu ne ressemble pas
   à du français, on essaie les autres orientations et on garde la meilleure.
3. Les PDF « texte » dont la couche texte est corrompue (polices mal encodées : « ObJeT », « Àppel »,
   « §üEËI{â ») : on compare avec l'OCR et on garde la lecture la plus propre.
"""

from __future__ import annotations

import io
import re

import cv2
import numpy as np
import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps

def _trouver_tesseract() -> None:
    """Windows : Tesseract n'est pas toujours dans le PATH ; on le cherche aux emplacements habituels."""
    import os
    import shutil
    from pathlib import Path
    # Langues posées à côté des scripts (C:\pv\tessdata) : pas besoin de droits administrateur.
    local = Path(__file__).resolve().parent / "tessdata"
    if (local / "fra.traineddata").exists():
        os.environ["TESSDATA_PREFIX"] = str(local)
    if os.environ.get("TESSERACT"):
        pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT"]
        return
    if shutil.which("tesseract"):
        return
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
              os.path.expanduser(r"~\miniconda3\Library\bin\tesseract.exe"),
              os.path.expanduser(r"~\anaconda3\Library\bin\tesseract.exe")):
        if os.path.exists(p):
            pytesseract.pytesseract.tesseract_cmd = p
            return


_trouver_tesseract()

RENDER_DPI = 300
# Plafond de pixels pour une page rendue. Une page A4 à 300 dpi en fait 8,7 millions ; ce
# plafond ne gêne donc aucun document normal. Il protège des pages anormalement grandes :
# un scan A4 à 600 dpi réenveloppé en PDF donne une page de 25 x 35 pouces, qui rendue à
# 300 dpi ferait 78 millions de pixels — 235 Mo brut, et six fois plus une fois passée dans
# le nettoyage des traits. Dix processus là-dessus épuisent la mémoire de la machine.
MAX_PIXELS = 16_000_000
OCR_TIMEOUT = 180          # secondes par page : une page qui bloque Tesseract ne bloque pas tout le lot
LANG = "fra"
TESS_CONFIG = "--psm 4"
MIN_NATIVE_CHARS = 200
H_LINE = 60
V_LINE = 45

# Vocabulaire qu'on retrouve dans tout extrait de PV : sert à juger si une lecture est bonne.
VOCAB = re.compile(r"\b(concurrents?|soumissionnaires?|ouverture|plis|engagement|montants?|liste|objet|retenue?s?|"
                   r"commission|offres?|appel|maitre|ma[iî]tre|ouvrage|date|des|les|société|societe|sarl|marché|"
                   r"publics?|dossiers?|administratifs?|techniques?|réserve|reserve|néant|neant|avis|journaux|"
                   r"attributaire|justification|vérification|verification)\b", re.I)


def score_texte(text: str) -> float:
    """Plus c'est haut, plus le texte ressemble à un PV lisible (mots attendus, peu de symboles)."""
    if not text.strip():
        return 0.0
    mots = len(VOCAB.findall(text))
    bizarres = len(re.findall(r"[§{}\[\]\\*$#~^<>¤£¥]", text))
    casse = len(re.findall(r"\b[a-zà-ÿ]+[A-ZÀ-Þ][a-zà-ÿA-ZÀ-Þ]*\b", text))     # « ObJeT », « lnformation »
    return mots - 0.5 * bizarres - 0.5 * casse


def echelle_sure(page, dpi: float) -> float:
    """Le facteur de rendu pour cette page, ramené sous le plafond de pixels."""
    try:
        largeur, hauteur = page.get_size()                   # en points, 1/72 de pouce
    except Exception:                                        # noqa: BLE001
        return dpi / 72
    s = dpi / 72
    surface = max(1.0, largeur * hauteur)
    if surface * s * s > MAX_PIXELS:
        s = (MAX_PIXELS / surface) ** 0.5
    return max(0.35, s)


def sous_plafond(image: Image.Image) -> Image.Image:
    """La même image, réduite si elle dépasse le plafond. Les proportions sont conservées."""
    pixels = image.width * image.height
    if pixels <= MAX_PIXELS:
        return image
    facteur = (MAX_PIXELS / pixels) ** 0.5
    return image.resize((max(1, int(image.width * facteur)),
                         max(1, int(image.height * facteur))), Image.LANCZOS)


def remove_lines(image: Image.Image) -> Image.Image:
    gray = np.array(ImageOps.exif_transpose(image).convert("L"))
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (H_LINE, 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, V_LINE)))
    rules = cv2.dilate(horizontal | vertical, np.ones((3, 3), np.uint8))
    cleaned = gray.copy()
    cleaned[rules > 0] = 255
    return Image.fromarray(cleaned)


def _ocr(image: Image.Image) -> str:
    try:
        return pytesseract.image_to_string(image, lang=LANG, config=TESS_CONFIG, timeout=OCR_TIMEOUT)
    except RuntimeError:          # délai dépassé
        return ""


def ocr_image(image: Image.Image) -> str:
    """OCR d'une page, en redressant une page tournée si besoin (réessaie plus petit si la mémoire manque)."""
    try:
        return _ocr_image(image)
    except (MemoryError, cv2.error):
        # 11 processus en parallèle sur une grande page scannée : on réduit l'image et on réessaie.
        petite = image.resize((max(1, image.width // 2), max(1, image.height // 2)), Image.LANCZOS)
        try:
            return _ocr_image(petite)
        except (MemoryError, cv2.error):
            return ""


def _ocr_image(image: Image.Image) -> str:
    image = sous_plafond(ImageOps.exif_transpose(image).convert("RGB"))
    # Pages scannées à basse résolution (images TIF/JPG) : agrandir pour l'OCR.
    if image.width < 1600:
        ratio = 2400 / image.width
        image = image.resize((2400, int(image.height * ratio)), Image.LANCZOS)
    propre = remove_lines(image)
    text = _ocr(propre)
    s = score_texte(text)
    if s >= 8:
        return text
    # Lecture pauvre : la page est peut-être tournée. D'abord l'avis de Tesseract (rapide)...
    angles = []
    try:
        osd = pytesseract.image_to_osd(propre, config="--psm 0 -c min_characters_to_try=5", timeout=60)
        rot = int(re.search(r"Rotate: (\d+)", osd).group(1))
        if rot:
            angles.append(rot)
    except Exception:
        pass
    # ... puis les 3 orientations possibles.
    angles += [a for a in (90, 270, 180) if a not in angles]
    best_text, best_score = text, s
    for a in angles:
        t = _ocr(propre.rotate(-a, expand=True, fillcolor=255))
        sc = score_texte(t)
        if sc > best_score:
            best_text, best_score = t, sc
        if best_score >= 8:
            break
    return best_text


def read_pages(data: bytes, max_pages: int, force_ocr: bool = False) -> tuple[list[str], str]:
    """(texte de chaque page, méthode) pour un PDF ou une image."""
    if data[:5] != b"%PDF-":
        image = Image.open(io.BytesIO(data))
        image.load()
        return [ocr_image(image)], "ocr"

    document = pdfium.PdfDocument(data)
    try:
        count = min(len(document), max_pages)
        native = []
        for index in range(count):
            text_page = document[index].get_textpage()
            try:
                native.append(text_page.get_text_range())
            finally:
                text_page.close()
        if not force_ocr and native and sum(len(t.strip()) for t in native) / len(native) >= MIN_NATIVE_CHARS:
            suspect = [i for i, t in enumerate(native)
                       if len(re.findall(r"[§{}\[\]\\*$#~^<>¤]", t)) > 0.005 * max(1, len(t))
                       or score_texte(t) < 0.004 * len(t)]
            if not suspect:
                return native, "native"
            # Couche texte douteuse (souvent générée par le scanner) : on refait l'OCR de ces pages
            # et on garde, page par page, la meilleure des deux lectures.
            pages, methode = list(native), "native"
            for i in suspect:
                page = document[i]
                ocr_text = ocr_image(page.render(scale=echelle_sure(page, RENDER_DPI)).to_pil())
                if score_texte(ocr_text) > score_texte(native[i]):
                    pages[i], methode = ocr_text, "native+ocr"
            return pages, methode
        pages = []
        for i in range(count):
            page = document[i]
            try:
                image = page.render(scale=echelle_sure(page, RENDER_DPI)).to_pil()
            except (MemoryError, RuntimeError):
                image = page.render(scale=echelle_sure(page, 150)).to_pil()
            pages.append(ocr_image(image))
        return pages, "ocr"
    finally:
        document.close()
