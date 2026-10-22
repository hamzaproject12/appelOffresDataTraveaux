"""GET /api/copilote/similaires/{ref_annonce}?lot=&poids= : les marchés les plus proches d'un AO ouvert.

Lit l'AO dans ao_ouverts.db (lecture seule), rapproche son acheteur de l'historique, puis demande
au moteur (similarite.py) les 50 meilleurs marchés antérieurs. Un AO « REJEU- » ne voit que ce qui
précède son ouverture réelle (rejeu.jusqua_pour) ; un « REJEU- » sans coupure est une erreur.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import acheteurs, nature, qualifs, rejeu, reglages, similarite
from .bases import cle_acheteur


class ErreurAnalyse(Exception):
    def __init__(self, statut: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.statut, self.extra = statut, extra or {}


_connus_memo: dict = {}


def _connus(hist: sqlite3.Connection, cle_fichier) -> list[dict]:
    if _connus_memo.get("cle") != cle_fichier:
        _connus_memo.update(cle=cle_fichier, valeur=acheteurs.noms_historique(hist))
    return _connus_memo["valeur"]


def lire_ao(ao: sqlite3.Connection, ref_annonce: str, lot: str | None) -> dict:
    ligne = ao.execute("SELECT * FROM appels_ouverts WHERE ref_annonce = ?", (ref_annonce,)).fetchone()
    if ligne is None:
        raise ErreurAnalyse(404, "AO introuvable")
    lots = [dict(r) for r in ao.execute(
        "SELECT lot, intitule, estimation, caution_provisoire FROM appels_lots WHERE ref_annonce = ?"
        " ORDER BY rowid", (ref_annonce,))]
    quals = [(r["libelle"], r["classe"]) for r in ao.execute(
        "SELECT libelle, classe FROM appels_qualifications WHERE ref_annonce = ?", (ref_annonce,))]
    choisi = None
    if lots:
        if lot is None:
            raise ErreurAnalyse(400, "AO alloti : un AO s'analyse lot par lot, préciser ?lot=",
                                {"lots": [{"lot": x["lot"], "intitule": x["intitule"], "estimation": x["estimation"]}
                                          for x in lots]})
        choisi = next((x for x in lots if str(x["lot"]) == str(lot)), None)
        if choisi is None:
            raise ErreurAnalyse(404, f"Lot {lot} introuvable dans cet AO",
                                {"lots": [str(x["lot"]) for x in lots]})
    elif lot is not None:
        raise ErreurAnalyse(404, "Cet AO n'a pas de lots")
    return {"ligne": dict(ligne), "lot": choisi, "qualifications": quals}


def analyser(ref_annonce: str, lot: str | None, poids_texte: str | None, *, ouvrir_historique, chemin_historique,
             ao: sqlite3.Connection, calc: sqlite3.Connection, ret: sqlite3.Connection,
             aujourdhui: date | None = None) -> dict:
    r0 = reglages.charger()
    try:
        poids = reglages.poids_depuis_texte(poids_texte, r0["poids"])
    except reglages.ReglageInvalide as e:
        raise ErreurAnalyse(422, str(e)) from None
    r = reglages.avec_poids(r0, poids)

    donnees = lire_ao(ao, ref_annonce, lot)
    ligne, lot_choisi = donnees["ligne"], donnees["lot"]

    # Quel jour fait foi ? Un rejeu : la veille de l'ouverture réelle ; un vrai AO : aujourd'hui.
    try:
        coupure = rejeu.jusqua_pour(ref_annonce, calc)
    except rejeu.RejeuSansCoupure as e:
        raise ErreurAnalyse(409, str(e)) from None
    if coupure:
        jour_ref = similarite.jour_de(coupure) + 1
        calcul, mode = f"jusqua-{coupure}", "rejeu"
    else:
        jour_ref = (aujourdhui or date.today()).toordinal()
        calcul, mode = "courant", "reel"

    objet = ligne["objet"] or ""
    estimation = ligne["estimation"]
    if lot_choisi:
        objet = f"{lot_choisi['intitule'] or ''} {objet}".strip()
        estimation = lot_choisi["estimation"] or estimation

    hist = ouvrir_historique()
    try:
        connus = _connus(hist, similarite._cle_fichier(chemin_historique))
        nom_ach = ligne["acheteur"] or ""
        cle_ach = ligne["cle_acheteur"] or cle_acheteur(nom_ach)
        rg = r["correspondances_acheteurs"]
        rapproche = acheteurs.rapprocher(cle_ach, nom_ach, connus, ret, rg["seuil_auto"], rg["ecart_ambigu"],
                                         enregistrer=mode == "reel")
    finally:
        hist.close()

    quals = qualifs.paires(donnees["qualifications"])
    cible = similarite.cible_depuis_texte(objet, nom_ach, estimation, quals, jour_ref,
                                          acheteur_cles=rapproche["cles"], lieu=ligne.get("lieu"),
                                          ref=ref_annonce, lot=str(lot_choisi["lot"]) if lot_choisi else None)
    m = similarite.moteur(ouvrir_historique, chemin_historique, calc, calcul,
                          jour_limite=jour_ref if mode == "rejeu" else None, sauver=mode == "reel")
    comp = m.composantes(cible, r)
    notes = m.noter(comp, poids) if comp.k else comp.masque.astype(float)
    n = r["resultats"]
    retenus = m.meilleurs(notes, n) if comp.k else []
    lignes = [m.ligne(cible, comp, int(i), notes, poids) for i in retenus]
    return {
        "ao": {"ref_annonce": ref_annonce, "lot": cible.lot, "objet": objet, "acheteur": nom_ach,
               "estimation": estimation, "mode": mode,
               "date_reference": date.fromordinal(jour_ref).isoformat(),
               "famille": {"code": cible.famille, "libelle": nature.libelle(cible.famille)},
               "nature": {"code": cible.nature, "libelle": nature.libelle(cible.nature)},
               "region": cible.region,
               "qualifications": [{"code": c, "classe": cl} for c, cl in quals.items()]},
        "acheteur": {"nom": nom_ach, "cle": cle_ach, "statut": rapproche["statut"],
                     "cles_utilisees": rapproche["cles"], "propositions": rapproche["propositions"]},
        "poids": poids, "filtre": r["filtre"],
        "index": {"calcul": m.calcul, "origine": m.origine, "documents": int(m.index.matrice.shape[0]),
                  "traits": int(m.index.matrice.shape[1]), "cree_le": m.index.cree_le},
        "marches_lus": comp.k, "marches_comparables": int(comp.masque.sum()),
        "resume": similarite.mediane_reference(m, retenus, r["pour_mediane"]) if len(retenus) else
                  {"nb": 0, "mediane": None, "q1": None, "q3": None},
        "similaires": lignes,
    }
