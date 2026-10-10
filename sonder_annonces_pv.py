"""Troisième et dernière reconnaissance : la vraie liste des annonces d'extrait de PV.

La fois précédente la recherche a renvoyé 85 989 annonces au lieu de 34 556 : nos critères
envoient « annonceType = 3 » (annonce de consultation), un type qui n'existe même pas sur ce
formulaire-là, et le portail l'a donc ignoré. Le bon code est 5.

    ctl0$CONTENU_PAGE$AdvancedSearch$annonceType = 5   Annonce d'extrait de PV
    ctl0$CONTENU_PAGE$AdvancedSearch$categorie   = 1   Travaux

Ce programme lance la recherche avec les bons critères, vérifie qu'on retombe bien sur ton
chiffre, puis ouvre une annonce pour trouver par où se télécharge le PDF. Une dizaine de
requêtes, moins d'une minute. Il n'écrit que dans « sondage3\\ ».

    cd pv_travaux\\site
    python sonder_annonces_pv.py
"""
from __future__ import annotations

import re
import sys
import time
from html import unescape
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
import telecharger_pv as tp                                  # noqa: E402
import collecter_consultations as cc                         # noqa: E402

SORTIE = ICI / "sondage3"
URL = tp.BASE + "?page=entreprise.EntrepriseAdvancedSearch&AvisExtraitPV"
TYPE_EXTRAIT_PV = "5"


def propre(t: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", t or ""))).strip()


def criteres_pv(depuis: str, jusqua: str) -> dict:
    p = cc.PFX
    return {p + "annonceType": TYPE_EXTRAIT_PV,
            p + "categorie": "1",
            p + "dateMiseEnLigneCalculeStart": depuis,
            p + "dateMiseEnLigneCalculeEnd": jusqua}


def liens_de(bloc: str) -> list[str]:
    out = []
    for cible in re.findall(r"""(?:href|onclick)\s*=\s*["']([^"']{6,260})["']""", bloc, re.I):
        c = unescape(cible)
        if c.startswith("#") or "javascript:;" in c or "toggleMenu" in c:
            continue
        out.append(c)
    return list(dict.fromkeys(out))


def main() -> None:
    SORTIE.mkdir(parents=True, exist_ok=True)
    client = tp.Client()
    page = client.html(URL)

    print("=== RECHERCHE DES ANNONCES D'EXTRAIT DE PV (Travaux, depuis 2022) ===")
    html = tp.postback(client, page, cc.PFX + "lancerRecherche",
                       criteres_pv("01/01/2022", "31/12/2027"),
                       button=(cc.PFX + "lancerRecherche", "Lancer la recherche"))
    (SORTIE / "resultats.html").write_text(html, encoding="utf-8", errors="replace")
    total, pages = tp.pagination(html)
    print(f"  {total} annonces, {pages} page(s)")
    print(f"  (ton écran annonçait 34 556 — si on y est, les critères sont bons)")

    # Une ligne de résultat, telle quelle : c'est là que se trouve le chemin vers le PDF.
    lignes = re.findall(r"<tr[^>]*>.*?</tr>", html, re.S | re.I)
    interessantes = [l for l in lignes if "refConsultation" in l]
    print(f"\n=== {len(interessantes)} lignes de résultat ; la première en détail ===")
    if not interessantes:
        print("  aucune ligne reconnue — regarde sondage3\\resultats.html")
        return
    premiere = interessantes[0]
    (SORTIE / "une_ligne.html").write_text(premiere, encoding="utf-8", errors="replace")
    print(f"  texte : {propre(premiere)[:300]}")
    print("  liens :")
    for c in liens_de(premiere):
        print(f"    {c[:150]}")

    # On suit le premier lien de détail pour voir le bloc de téléchargement.
    detail = next((c for c in liens_de(premiere)
                   if "Detail" in c or "detail" in c), None)
    if not detail:
        print("\n  pas de lien de détail reconnu dans la ligne.")
        return
    adresse = detail if detail.startswith("http") else tp.BASE.rsplit("/", 1)[0] + "/" + detail.lstrip("./")
    print(f"\n=== LA PAGE DE L'ANNONCE ===\n  {adresse[:160]}")
    time.sleep(1)
    try:
        page_detail = client.html(adresse)
    except Exception as e:                                   # noqa: BLE001
        print(f"  échec ({type(e).__name__}: {e})")
        return
    (SORTIE / "annonce.html").write_text(page_detail, encoding="utf-8", errors="replace")
    print(f"  {len(page_detail)} octets -> sondage3\\annonce.html")
    print("  adresses de téléchargement :")
    vues = set()
    for c in re.findall(r"""(?:href|onclick|src)\s*=\s*["']([^"']{6,260})["']""", page_detail, re.I):
        c = unescape(c)
        if re.search(r"download|telecharg|fichier|\.pdf|DownloadFile|Piece", c, re.I) and c not in vues:
            vues.add(c)
            print(f"    {c[:170]}")
    if not vues:
        print("    aucune — le fichier est derrière un bouton de formulaire, je lirai annonce.html")
    print(f"\n-> {SORTIE}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu.")
    except Exception as e:                                   # noqa: BLE001
        print(f"ARRÊT : {type(e).__name__}: {e}")
        print("Ce qui a déjà été conservé dans sondage3\\ reste utilisable.")
