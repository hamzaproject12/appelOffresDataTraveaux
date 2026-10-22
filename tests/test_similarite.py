"""Étape 4 : familles et nature, index TF-IDF, score de similarité, rapprochement des acheteurs, API."""
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sklearn.feature_extraction.text import TfidfVectorizer

from copilote import (acheteurs, api_similaires, bases, config, corpus, index_texte, mesures_similarite, nature,
                      qualifs, regions, reglages, rejeu, similarite)
from copilote.routes import creer_routeur
from tests import outils
from tests.test_acces import CODES, SECRET, cookie_visiteur

ICI = Path(__file__).resolve().parent.parent

SCHEMA_AO = """
CREATE TABLE appels_ouverts (ref_annonce TEXT PRIMARY KEY, org_acronyme TEXT, reference TEXT, objet TEXT,
  acheteur TEXT, cle_acheteur TEXT, publie_le TEXT, date_limite TEXT, estimation REAL, caution_provisoire REAL,
  lieu TEXT, url TEXT, source TEXT, recu_le TEXT, maj_le TEXT, brut TEXT);
CREATE TABLE appels_lots (ref_annonce TEXT, lot TEXT, intitule TEXT, estimation REAL, caution_provisoire REAL,
  PRIMARY KEY (ref_annonce, lot));
CREATE TABLE appels_qualifications (ref_annonce TEXT, libelle TEXT, classe TEXT);
"""

SERIES = [  # (objet, acheteur, qualification, estimation de base)
    ("Travaux de construction de pistes rurales reliant les douars de la commune {}", "COMMUNE DE {}", "B.1- Terrassements", 5e6),
    ("Travaux d'alimentation en eau potable du douar {}", "COMMUNE DE {}", "F.2- Adduction", 2e6),
    ("Construction de l'ecole primaire de {}", "DIRECTION PROVINCIALE DE L'EDUCATION NATIONALE DE {}", "A.2- Beton arme", 8e6),
]


def base_similaire(chemin: Path, annee_fin="2026") -> None:
    """72 marchés de 2025 (24 par série) puis 6 de 2026 ; 4 offres chacun, taux de 0,90 à 1,05."""
    db = outils.creer(chemin)
    n = 0
    for annee, nb in (("2025", 24), (annee_fin, 2)):
        for i in range(nb):
            for s, (objet, ach, qual, base) in enumerate(SERIES):
                n += 1
                lieu = f"LIEU{i % 6}"
                est = base * (1 + 0.04 * (i % 7))
                ref = f"m{n:03d}"
                offres = [(f"soc{(i + j) % 7}", round(est * (0.90 + 0.05 * j + 0.003 * (s + i % 3)), 2)) for j in range(4)]
                outils.marche(db, ref, est, offres, gagnant=offres[1][0],
                              date=f"{annee}{1 + i % 12:02d}{1 + (i * 3) % 27:02d}", acheteur=ach.format(lieu),
                              objet=objet.format(lieu))
                outils.qualification(db, ref, qual, qual[0] + "- X", classe=str(2 + s))
    db.commit()
    db.close()


def base_ao(chemin: Path, lignes=(), lots=(), quals=()) -> None:
    db = sqlite3.connect(chemin)
    db.executescript(SCHEMA_AO)
    for r in lignes:
        db.execute("INSERT INTO appels_ouverts (ref_annonce, objet, acheteur, cle_acheteur, estimation, lieu)"
                   " VALUES (?,?,?,?,?,?)", (*r[:3], bases.cle_acheteur(r[2]), *r[3:]))
    for r in lots:
        db.execute("INSERT INTO appels_lots (ref_annonce, lot, intitule, estimation) VALUES (?,?,?,?)", r)
    for r in quals:
        db.execute("INSERT INTO appels_qualifications VALUES (?,?,?)", r)
    db.commit()
    db.close()


class NatureEtQualifs(unittest.TestCase):
    def test_a_plat_sans_accents_ni_apostrophe(self):
        self.assertEqual(nature.plat("Travaux d'aménagement de l'École"), "travaux d amenagement de l ecole")

    def test_familles(self):
        cas = {
            "Travaux de construction d'un ouvrage d'art au PK 42 de la RR702": "ouvrage_art",
            "Alimentation en eau potable du douar X": "eau_potable",
            "Construction de l'école primaire": "batiment_neuf",
            "Réhabilitation de l'école primaire": "batiment_rehabilitation",
            "Entretien courant des voies de la commune": "route_piste",
            "Éclairage public de la ville": "eclairage_electricite",
            "Aménagement d'un terrain de sport de proximité": "sport",
            "Travaux d'assainissement liquide": "assainissement",
            "Reboisement sur une superficie de 45 ha": "foret_environnement",
        }
        for texte, attendu in cas.items():
            self.assertEqual(nature.classer(texte)[0], attendu, texte)

    def test_nature(self):
        self.assertEqual(nature.nature_de("Construction d'une école"), "neuf")
        self.assertEqual(nature.nature_de("Réhabilitation et extension d'une école"), "rehabilitation")  # le plus tôt
        self.assertEqual(nature.nature_de("Travaux d'entretien des routes"), "entretien")
        self.assertEqual(nature.nature_de("Travaux d'aménagement de la place"), "neuf")                  # mot secondaire
        self.assertIsNone(nature.nature_de("Les travaux"))

    def test_mot_entier_pluriel_et_prefixe(self):
        self.assertEqual(nature.famille_de("Réalisation de ponts"), "ouvrage_art")
        self.assertNotEqual(nature.famille_de("Réalisation des ponctions"), "ouvrage_art")     # « pont » n'est pas dans « ponctions »
        self.assertEqual(nature.famille_de("Travaux d'assainissement liquide"), "assainissement")   # « assainissement » entier

    def test_un_texte_inconnu_n_a_pas_de_famille(self):
        self.assertEqual(nature.classer("Xyzzy plugh"), (None, None))

    def test_qualifications_par_code(self):
        self.assertEqual(qualifs.code_de("B.1- Travaux de terrassements routiers courants"), "B.1")
        self.assertEqual(qualifs.code_de("B.1"), "B.1")
        self.assertEqual(qualifs.code_de("A.10- Travaux"), "A.10")
        self.assertIsNone(qualifs.code_de("Bâtiment"))
        self.assertEqual(qualifs.classe_de("Classe 3"), 3)
        self.assertIsNone(qualifs.classe_de(None))
        self.assertEqual(qualifs.paires([("B.1- Terrassements", "3"), ("B.1", "4"), ("C.2", None)]),
                         {"B.1": 4, "C.2": None})
        self.assertEqual(qualifs.proximite_classe(3, 3), 1.0)
        self.assertEqual(qualifs.proximite_classe(3, 4), 0.6)
        self.assertEqual(qualifs.proximite_classe(3, 1), 0.3)
        self.assertEqual(qualifs.proximite_classe(3, 6), 0.0)
        self.assertEqual(qualifs.proximite_classe(None, 5), 0.5)


class Index(unittest.TestCase):
    TEXTES = ["travaux de construction de pistes rurales", "construction de l ecole primaire", "alimentation en eau potable",
              "travaux de pistes reliant les douars", "ecole primaire de la commune", "eau potable du douar", "route nationale"]

    def test_meme_calcul_que_tfidfvectorizer(self):
        ix = index_texte.construire(self.TEXTES)
        ref = TfidfVectorizer(analyzer="char_wb", ngram_range=index_texte.NGRAMMES, min_df=index_texte.MIN_DF,
                              sublinear_tf=True, lowercase=False, dtype=np.float32)
        attendu = ref.fit_transform(self.TEXTES)
        self.assertEqual(set(ix.vocabulaire), set(ref.vocabulary_))
        ordre = [ix.vocabulaire[t] for t in sorted(ref.vocabulary_, key=ref.vocabulary_.get)]
        np.testing.assert_allclose(ix.matrice[:, ordre].toarray(), attendu.toarray(), atol=1e-6)
        # et un texte nouveau se projette dans le même espace
        np.testing.assert_allclose(ix.vecteur(self.TEXTES[1])[:, ordre].toarray(), attendu[1].toarray(), atol=1e-6)

    def test_rangement_et_rechargement_identiques(self):
        calc = sqlite3.connect(":memory:")
        calc.row_factory = sqlite3.Row
        ix, origine = index_texte.obtenir(calc, "courant", self.TEXTES, "sig1")
        self.assertEqual(origine, "calcule")
        relu, origine2 = index_texte.obtenir(calc, "courant", self.TEXTES, "sig1")
        self.assertEqual(origine2, "recharge")
        self.assertEqual(relu.vocabulaire, ix.vocabulaire)
        np.testing.assert_array_equal(relu.idf, ix.idf)
        self.assertEqual((relu.matrice != ix.matrice).nnz, 0)
        np.testing.assert_allclose(relu.similarites(self.TEXTES[0]), ix.similarites(self.TEXTES[0]))

    def test_un_corpus_different_refait_l_index(self):
        calc = sqlite3.connect(":memory:")
        calc.row_factory = sqlite3.Row
        index_texte.obtenir(calc, "courant", self.TEXTES, "sig1")
        _, origine = index_texte.obtenir(calc, "courant", self.TEXTES + ["autre"], "sig2")
        self.assertEqual(origine, "calcule")
        self.assertIsNone(index_texte.charger(calc, "courant", "sig1"))

    def test_ajuste_sur_un_prefixe_ne_voit_pas_la_suite(self):
        textes = ["travaux de pistes", "travaux de pistes rurales", "travaux de pistes", "zzqxj kwv zzqxj kwv"] * 2
        ix = index_texte.construire(textes, ajuste_sur=4)
        self.assertFalse(any("zzq" in g for g in ix.vocabulaire))         # un mot qui n'apparaît qu'après n'existe pas
        self.assertEqual(ix.matrice.shape[0], len(textes))                # mais les lignes tardives sont bien projetées
        self.assertEqual(ix.matrice[4:].nnz > 0, True)


class Moteur(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.d = Path(self.dossier.name)
        base_similaire(self.d / "pv.db")
        self.db = sqlite3.connect(f"file:{self.d / 'pv.db'}?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("cle_acheteur", 1, bases.cle_acheteur)
        self.c = corpus.construire(self.db)
        self.ix = index_texte.construire(self.c.texte, self.c.signature())
        self.m = similarite.Moteur(self.c, self.ix)
        self.r = reglages.charger()
        self.poids = self.r["poids"]

    def tearDown(self):
        self.db.close()
        self.dossier.cleanup()

    def cible(self, objet="Travaux de construction de pistes rurales reliant les douars de la commune LIEU1",
              acheteur="COMMUNE DE LIEU1", estimation=5e6, quals=None, jour="2026-01-10"):
        return similarite.cible_depuis_texte(objet, acheteur, estimation, {"B.1": 3} if quals is None else quals,
                                             similarite.jour_de(jour))

    def test_corpus_range_par_date_et_avant_est_strict(self):
        self.assertTrue(np.all(np.diff(self.c.jour) >= 0))
        j = int(self.c.jour[40])
        k = self.c.avant(j)
        self.assertTrue(np.all(self.c.jour[:k] < j))
        self.assertTrue(np.all(self.c.jour[k:] >= j))

    def test_aucun_candidat_n_est_posterieur_ni_du_meme_jour(self):
        for jour in ("2025-03-15", "2025-07-01"):
            cible = self.cible(jour=jour)
            comp = self.m.composantes(cible, self.r)
            idx = self.m.meilleurs(self.m.noter(comp, self.poids), 50)
            self.assertTrue(len(idx))
            self.assertTrue(np.all(self.c.jour[idx] < cible.jour))

    def test_sans_historique_avant_la_date_il_n_y_a_rien(self):
        comp = self.m.composantes(self.cible(jour="2024-01-01"), self.r)
        self.assertEqual(comp.k, 0)
        self.assertEqual(len(self.m.meilleurs(np.zeros(0), 50)), 0)

    def test_filtre_montant(self):
        for estimation in (1.5e6, 2e7):                      # l'un trop petit pour les gros marchés, l'autre trop grand
            comp = self.m.composantes(self.cible(estimation=estimation, quals={}), self.r)
            ratio = self.c.estimation[:comp.k] / estimation
            hors = (ratio < 0.3) | (ratio > 3.0)
            self.assertTrue(hors.any() and (~hors).any())
            np.testing.assert_array_equal(comp.masque, ~hors)      # sans qualification demandée, seul le montant filtre

    def test_filtre_qualification_par_code(self):
        # Marchés « B.1 » : compatibles ; marchés « F.2 » et « A.2 » : exclus (ils ont une qualification, sans B.1).
        comp = self.m.composantes(self.cible(quals={"B.1": 3}, estimation=None), self.r)
        for i in range(comp.k):
            codes = set(self.c.fiches[i].quals)
            self.assertEqual(bool(comp.masque[i]), "B.1" in codes, codes)
        sans = self.m.composantes(self.cible(quals={"B.1": 3}, estimation=None),
                                  {**self.r, "filtre": {**self.r["filtre"], "qualification": "aucun"}})
        self.assertTrue(sans.masque.all())
        self.assertFalse(self.m.composantes(self.cible(quals={}, estimation=None), self.r).masque.min() == 0)

    def test_un_marche_sans_qualification_reste_comparable(self):
        db = sqlite3.connect(self.d / "pv.db")
        db.execute("DELETE FROM qualifications WHERE ref = 'm001'")
        db.commit()
        db.close()
        c = corpus.construire(self.db)
        m = similarite.Moteur(c, index_texte.construire(c.texte, c.signature()))
        comp = m.composantes(self.cible(quals={"B.1": 3}, estimation=None), self.r)
        i = c.index_de["m001"]
        self.assertTrue(comp.masque[i])
        self.assertAlmostEqual(comp.valeurs["qualification"][i], self.r["qualification"]["inconnue"])

    def test_note_entre_0_et_100_et_detail_somme_la_note(self):
        cible = self.cible(jour="2026-06-01")
        comp = self.m.composantes(cible, self.r)
        notes = self.m.noter(comp, self.poids)
        idx = self.m.meilleurs(notes, 50)
        for i in idx:
            self.assertTrue(0 <= notes[i] <= 100.0)
            d = self.m.detail(cible, comp, int(i), self.poids)
            self.assertEqual(set(d), set(reglages.CRITERES))
            self.assertAlmostEqual(sum(x["points"] for x in d.values()), notes[i], delta=0.06)
            for x in d.values():
                self.assertTrue(0 <= x["valeur"] <= 1)
                self.assertTrue(x["explication"])

    def test_criteres_sans_information_sortent_du_denominateur(self):
        cible = self.cible(objet="Travaux de construction de pistes rurales reliant les douars de la commune LIEU3",
                           acheteur="COMMUNE DE LIEU3", quals={}, estimation=None, jour="2026-06-01")
        cible.region = None
        comp = self.m.composantes(cible, self.r)
        self.assertEqual(comp.inutiles, frozenset({"qualification", "montant", "region"}))
        notes = self.m.noter(comp, self.poids)
        i = int(self.m.meilleurs(notes, 1)[0])
        d = self.m.detail(cible, comp, i, self.poids)
        self.assertTrue(all(d[n]["ignore"] and d[n]["points"] == 0 for n in ("qualification", "montant", "region")))
        self.assertAlmostEqual(sum(x["points"] for x in d.values()), notes[i], delta=0.06)
        self.assertAlmostEqual(sum(x["poids_effectif"] for x in d.values()), 100.0, delta=0.05)
        self.assertGreater(notes[i], 55)             # sans le plafond de 70 que donnaient les trois critères à 0
        # des poids qui ne portent que sur des critères inutilisables sont gardés tels quels, sans diviser par zéro
        seul = {**dict.fromkeys(reglages.CRITERES, 0), "montant": 1}
        self.assertTrue(np.isfinite(self.m.noter(comp, seul)).all())

    def test_classement_trie_et_plafonne(self):
        comp = self.m.composantes(self.cible(jour="2026-06-01", quals={}, estimation=None), self.r)
        notes = self.m.noter(comp, self.poids)
        idx = self.m.meilleurs(notes, 50)
        self.assertEqual(len(idx), 50)
        self.assertEqual(len(self.m.meilleurs(notes, 10)), 10)
        self.assertTrue(all(notes[a] >= notes[b] - 1e-9 for a, b in zip(idx, idx[1:])))

    def test_le_meme_acheteur_passe_devant_avec_un_poids_acheteur_seul(self):
        cible = self.cible(jour="2026-06-01", acheteur="COMMUNE DE LIEU3")
        comp = self.m.composantes(cible, self.r)
        notes = self.m.noter(comp, {**dict.fromkeys(reglages.CRITERES, 0), "acheteur": 1})
        cle = bases.cle_acheteur("COMMUNE DE LIEU3")
        memes = [i for i in range(comp.k) if self.c.acheteur_cle[i] == cle and comp.masque[i]]
        self.assertGreaterEqual(len(memes), 2)
        meilleur = self.m.meilleurs(notes, len(memes))
        self.assertEqual(set(meilleur), set(memes))                  # tous les « même acheteur » avant les autres
        self.assertAlmostEqual(float(notes[meilleur[0]]), 100.0)

    def test_famille_inconnue_de_la_cible_ne_penalise_pas(self):
        cible = self.cible(objet="Xyzzy plugh")
        self.assertIsNone(cible.famille)
        comp = self.m.composantes(cible, self.r)
        np.testing.assert_allclose(comp.valeurs["objet_nature"], comp.cos[:comp.k] * 1.0, atol=1e-9)

    def test_objet_proche_bien_classe(self):
        cible = self.cible(objet="Alimentation en eau potable du douar LIEU2", quals={"F.2": 3},
                           acheteur="COMMUNE DE LIEU2", jour="2026-06-01")
        comp = self.m.composantes(cible, self.r)
        idx = self.m.meilleurs(self.m.noter(comp, self.poids), 10)
        self.assertTrue(all(self.c.famille[i] == "eau_potable" for i in idx))

    def test_recence_decroit(self):
        comp = self.m.composantes(self.cible(jour="2026-06-01"), self.r)
        age = comp.k and (similarite.jour_de("2026-06-01") - self.c.jour[:comp.k])
        ordre = np.argsort(age)
        self.assertTrue(np.all(np.diff(comp.valeurs["recence"][ordre]) <= 1e-12))
        demi = self.r["recence"]["demi_vie_jours"]
        i = int(np.argmin(np.abs(age - demi)))
        self.assertAlmostEqual(comp.valeurs["recence"][i], 0.5 ** (age[i] / demi))

    def test_poids_en_parametre(self):
        base = self.poids
        p = reglages.poids_depuis_texte("acheteur:50, montant:5", base)
        self.assertEqual((p["acheteur"], p["montant"], p["region"]), (50, 5, base["region"]))
        self.assertEqual(reglages.poids_depuis_texte(None, base), base)
        for mauvais in ("inconnu:5", "acheteur", "acheteur:abc", "acheteur:-1", "acheteur:nan"):
            with self.assertRaises(reglages.ReglageInvalide, msg=mauvais):
                reglages.poids_depuis_texte(mauvais, base)
        with self.assertRaises(reglages.ReglageInvalide):
            reglages.poids_depuis_texte(",".join(f"{c}:0" for c in reglages.CRITERES), base)

    def test_reglages_par_defaut(self):
        r = reglages.charger()
        self.assertEqual(r["poids"], {"objet_nature": 40, "acheteur": 30, "qualification": 10, "montant": 10,
                                      "region": 5, "recence": 5})
        self.assertEqual(sum(r["poids"].values()), 100)
        self.assertEqual((r["filtre"]["montant_min"], r["filtre"]["montant_max"]), (0.3, 3.0))
        self.assertEqual((r["resultats"], r["pour_mediane"]), (50, 20))

    def test_ligne_de_resultat(self):
        cible = self.cible(jour="2026-06-01")
        comp = self.m.composantes(cible, self.r)
        notes = self.m.noter(comp, self.poids)
        i = int(self.m.meilleurs(notes, 1)[0])
        ligne = self.m.ligne(cible, comp, i, notes, self.poids)
        for cle in ("identifiant", "ref", "objet", "acheteur", "date", "estimation", "score", "detail", "gagnant",
                    "reference_taux", "nb_offres", "nb_concurrents"):
            self.assertIn(cle, ligne)
        self.assertEqual(ligne["nb_concurrents"], 4)
        self.assertEqual(ligne["gagnant"]["source"], "attribution")
        self.assertAlmostEqual(ligne["reference_taux"], float(self.c.ref_taux[i]), places=4)

    def test_mediane_de_reference(self):
        idx = np.array([0, 1, 2])
        m = similarite.mediane_reference(self.m, idx, 20)
        self.assertEqual(m["nb"], 3)
        self.assertAlmostEqual(m["mediane"], round(float(np.median(self.c.ref_taux[idx])), 4))
        self.assertEqual(similarite.mediane_reference(self.m, np.array([], dtype=int), 20)["mediane"], None)

    def test_backtest_sans_fuite_sur_la_base_de_poche(self):
        with mock.patch.object(config, "COPILOTE_CALCUL", self.d / "calcul.db"), \
                mock.patch.object(config, "COPILOTE_RETOURS", self.d / "retours.db"), \
                mock.patch.object(bases, "chemin_historique", return_value=self.d / "pv.db"):
            similarite.oublier()
            r = mesures_similarite.backtest("2026", progression=False)
        similarite.oublier()
        self.assertEqual(r["marches_testes"], 6)
        self.assertEqual(r["fuites"]["candidats_non_anterieurs"], 0)
        self.assertEqual(r["fuites"]["idf_ajuste_sur"], int(np.sum(self.c.jour < similarite.jour_de("2026-01-01"))))
        self.assertIn("sim_defaut", r["resultats"])
        self.assertIn("sim_defaut contre globale", r["comparaisons"])


class Acheteurs(unittest.TestCase):
    CONNUS = [
        {"cle": "a1", "nom": "COMMUNE URBAINE DE TAZA", "marches": 9},
        {"cle": "a2", "nom": "COMMUNE RURALE DE TAHLA", "marches": 5},
        {"cle": "a3", "nom": "DIRECTEUR PROVINCIAL DE L'EQUIPEMENT ET DU TRANSPORT DE TAZA", "marches": 40},
        {"cle": "a4", "nom": "DIRECTEUR PROVINCIAL DE L'EQUIPEMENT ET DU TRANSPORT DE TATA", "marches": 30},
        {"cle": "a5", "nom": "PREFECTURE DE TAZA", "marches": 3},
    ]

    def setUp(self):
        for c in self.CONNUS:
            c["type"], c["noyau"], c["variante"] = (acheteurs.type_acheteur(c["nom"]), acheteurs.noyau(c["nom"]),
                                                    acheteurs.variante(c["nom"]))
        self.ret = sqlite3.connect(":memory:")
        self.ret.row_factory = sqlite3.Row

    def test_types_et_noyau(self):
        self.assertEqual(acheteurs.type_acheteur("Commune rurale de Tahla"), "commune")
        self.assertEqual(acheteurs.type_acheteur("DIRECTEUR PROVINCIAL DE L'EQUIPEMENT"), "direction")
        self.assertEqual(acheteurs.type_acheteur("SOCIETE AL OMRANE"), "societe")
        self.assertEqual(acheteurs.noyau("DIRECTEUR PROVINCIAL DE L'EQUIPEMENT TAZA"), "equipement taza")
        self.assertEqual(acheteurs.noyau("DE LA"), "de la")             # que des mots génériques : on garde tout

    def test_direction_provinciale_et_directeur_provincial_se_rapprochent(self):
        p = acheteurs.proposer("DIRECTION PROVINCIALE DE L'EQUIPEMENT ET DU TRANSPORT DE TAZA", self.CONNUS)
        self.assertEqual(p[0]["cle"], "a3")
        self.assertGreaterEqual(p[0]["score"], 95)

    def test_deux_lieux_differents_ne_depassent_pas_le_plafond(self):
        p = {x["cle"]: x for x in acheteurs.proposer("DIRECTEUR PROVINCIAL DE L'EQUIPEMENT ET DU TRANSPORT DE TAZA",
                                                      self.CONNUS)}
        self.assertLessEqual(p["a4"]["score"], acheteurs.PLAFOND_MOTS_DIFFERENTS)
        self.assertTrue(p["a4"]["mots_differents"])

    def test_commune_rurale_et_urbaine_ne_se_confondent_pas(self):
        self.assertEqual([x["cle"] for x in acheteurs.proposer("COMMUNE RURALE DE TAZA", self.CONNUS)], [])
        self.assertEqual(acheteurs.proposer("COMMUNE DE TAZA", self.CONNUS)[0]["cle"], "a1")

    def test_types_differents_ne_se_rapprochent_pas(self):
        cles = [x["cle"] for x in acheteurs.proposer("COMMUNE DE TAZA", self.CONNUS)]
        self.assertNotIn("a5", cles)                                     # la préfecture de Taza n'est pas la commune

    def test_cle_connue_est_directe(self):
        r = acheteurs.rapprocher("a1", "COMMUNE URBAINE DE TAZA", self.CONNUS, self.ret)
        self.assertEqual((r["statut"], r["cles"]), ("direct", ["a1"]))

    def test_proposition_sure_est_utilisee_et_rangee(self):
        r = acheteurs.rapprocher("zz", "DIRECTION PROVINCIALE DE L'EQUIPEMENT ET DU TRANSPORT DE TAZA", self.CONNUS, self.ret)
        self.assertEqual((r["statut"], r["cles"]), ("correspondance", ["a3"]))
        lignes = acheteurs.lister(self.ret)
        self.assertEqual(lignes[0]["statut"], "proposee")
        self.assertEqual(lignes[0]["cle_ao"], "zz")

    def test_refus_humain_conserve_et_jamais_utilise(self):
        acheteurs.rapprocher("zz", "DIRECTION PROVINCIALE DE L'EQUIPEMENT ET DU TRANSPORT DE TAZA", self.CONNUS, self.ret)
        acheteurs.decider(self.ret, "zz", "a3", "refusee", "Alice", "autre entité")
        r = acheteurs.rapprocher("zz", "DIRECTION PROVINCIALE DE L'EQUIPEMENT ET DU TRANSPORT DE TAZA", self.CONNUS, self.ret)
        self.assertEqual((r["statut"], r["cles"]), ("aucun", []))
        ligne = acheteurs.lister(self.ret, cle_ao="zz")[0]
        self.assertEqual((ligne["statut"], ligne["decide_par"], ligne["commentaire"]), ("refusee", "Alice", "autre entité"))

    def test_validation_a_la_main_prime_sur_les_propositions(self):
        acheteurs.decider(self.ret, "zz", "a5", "validee", "Bob", "", nom_ao="ZZ", nom_historique="PREFECTURE DE TAZA")
        r = acheteurs.rapprocher("zz", "DIRECTION PROVINCIALE DE L'EQUIPEMENT ET DU TRANSPORT DE TAZA", self.CONNUS, self.ret)
        self.assertEqual((r["statut"], r["cles"]), ("correspondance", ["a5"]))
        self.assertEqual(acheteurs.lister(self.ret, "validee")[0]["origine"], "main")

    def test_proposition_ambigue_n_est_pas_utilisee(self):
        connus = [dict(self.CONNUS[2], cle="b1"), dict(self.CONNUS[2], cle="b2", nom=self.CONNUS[2]["nom"].replace("TAZA", "TAZZA"))]
        for c in connus:
            c["noyau"] = acheteurs.noyau(c["nom"])
        r = acheteurs.rapprocher("zz", "DIRECTION PROVINCIALE DE L'EQUIPEMENT ET DU TRANSPORT DE TAZA", connus, self.ret)
        self.assertEqual(r["statut"], "aucun")
        self.assertEqual(len(r["propositions"]), 2)

    def test_une_proposition_ne_remplace_jamais_une_decision(self):
        acheteurs.decider(self.ret, "zz", "a3", "validee", "Alice")
        acheteurs.enregistrer_propositions(self.ret, "zz", "ZZ", [{"cle": "a3", "nom": "x", "score": 99.0}])
        self.assertEqual(acheteurs.lister(self.ret)[0]["statut"], "validee")

    def test_statut_invalide(self):
        with self.assertRaises(ValueError):
            acheteurs.decider(self.ret, "zz", "a3", "peut-etre", "Alice")

    def test_mesure_des_ao_sans_acheteur_reconnu(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            hist = Path(tmp.name) / "pv.db"
            base_similaire(hist)
            ao = Path(tmp.name) / "ao.db"
            base_ao(ao, [("A1", "x", "COMMUNE DE LIEU1", 1e6, None),            # connu
                         ("A2", "x", "COMMUNE LIEU2", 1e6, None),                # proche d'un acheteur connu
                         ("A3", "x", "SOCIETE INCONNUE DU TOUT", 1e6, None),     # rien
                         ("REJEU-01", "x", "COMMUNE DE LIEU1", 1e6, None)])      # les rejeux ne comptent pas
            h = sqlite3.connect(f"file:{hist}?mode=ro", uri=True)
            h.row_factory = sqlite3.Row
            a = sqlite3.connect(f"file:{ao}?mode=ro", uri=True)
            a.row_factory = sqlite3.Row
            m = acheteurs.mesurer_ao_ouverts(a, h, None)
            for x in (h, a):
                x.close()
        finally:
            tmp.cleanup()
        self.assertEqual((m["ao"], m["direct"], m["correspondance"], m["aucun"]), (3, 1, 1, 1))


class Api(unittest.TestCase):
    """L'application complète sur une base de poche et une base d'AO temporaires."""

    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.d = Path(self.dossier.name)
        base_similaire(self.d / "pv_travaux.db")
        base_ao(self.d / "ao_ouverts.db",
                lignes=[("AO1", "Travaux de construction de pistes rurales reliant les douars de la commune LIEU1",
                         "COMMUNE DE LIEU1", 5e6, "Taza"),
                        ("AO2", "Travaux d'alimentation en eau potable", "COMMUNE LIEU3", 2e6, None),
                        ("AO3", "Travaux d'amenagement en deux lots", "COMMUNE DE LIEU2", None, None),
                        ("REJEU-01", "Travaux d'alimentation en eau potable du douar LIEU2", "COMMUNE DE LIEU2", 2e6, None),
                        ("REJEU-02", "Travaux d'alimentation en eau potable du douar LIEU2", "COMMUNE DE LIEU2", 2e6, None)],
                lots=[("AO3", "1", "Terrassements", 1e6), ("AO3", "2", "Chaussee", 3e6)],
                quals=[("AO1", "B.1", "3"), ("AO2", "F.2", "3")])
        self.patchs = [mock.patch.dict("os.environ", {"COPILOTE_ACCES": CODES}),
                       mock.patch.object(config, "COPILOTE_CALCUL", self.d / "calcul.db"),
                       mock.patch.object(config, "COPILOTE_RETOURS", self.d / "retours.db")]
        for p in self.patchs:
            p.start()
        similarite.oublier()
        self.app = FastAPI()
        self.app.include_router(creer_routeur(SECRET, self.d / "pv_travaux.db", self.d / "ao_ouverts.db"))
        self.alice = self.client("code-alice")
        # Le rejeu REJEU-01 voit jusqu'au 2025-06-30 ; REJEU-02 n'a pas de date de coupure.
        calc = bases.calcul()
        calc.executescript(rejeu.SCHEMA_AO)
        calc.execute("INSERT INTO rejeu_ao (ref_annonce, date_coupure, annonce, cree_le) VALUES ('REJEU-01', '2025-06-30', '{}', 'x')")
        calc.commit()
        calc.close()

    def tearDown(self):
        similarite.oublier()
        for p in self.patchs:
            p.stop()
        bases._sources.clear()
        self.dossier.cleanup()

    def client(self, code=None):
        c = TestClient(self.app, follow_redirects=False)
        c.cookies.set("pv", cookie_visiteur())
        if code:
            self.assertEqual(c.post("/copilote/entrer", data={"code": code}).status_code, 303)
        return c

    def test_acces_refuse_sans_code(self):
        self.assertEqual(self.client().get("/api/copilote/similaires/AO1").status_code, 401)
        self.assertEqual(TestClient(self.app).get("/api/copilote/similaires/AO1").status_code, 401)
        self.assertEqual(self.client().get("/api/copilote/correspondances").status_code, 401)
        self.assertEqual(self.client().post("/api/copilote/correspondance",
                                            json={"cle_ao": "x", "cle_historique": "y", "statut": "validee"}).status_code, 401)

    def test_reponse_complete(self):
        r = self.alice.get("/api/copilote/similaires/AO1")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["ao"]["mode"], "reel")
        self.assertEqual(d["ao"]["famille"]["code"], "route_piste")
        self.assertEqual(d["ao"]["qualifications"], [{"code": "B.1", "classe": 3}])
        self.assertEqual(d["acheteur"]["statut"], "direct")
        self.assertEqual(d["poids"], reglages.charger()["poids"])
        self.assertEqual(d["index"]["origine"], "calcule")
        calc = sqlite3.connect(self.d / "calcul.db")
        try:
            self.assertEqual(calc.execute("SELECT calcul, nb_docs FROM sim_index").fetchall(), [("courant", 78)])
        finally:
            calc.close()
        similarite.oublier()                                           # « redémarrage » : l'index se recharge, il ne se refait pas
        self.assertEqual(self.alice.get("/api/copilote/similaires/AO1").json()["index"]["origine"], "recharge")
        self.assertEqual(d["marches_comparables"], 26)                  # les 26 marchés « B.1 » de la base de poche
        self.assertEqual(len(d["similaires"]), 26)
        premier = d["similaires"][0]
        self.assertTrue(all(a["score"] >= b["score"] for a, b in zip(d["similaires"], d["similaires"][1:])))
        for cle in ("score", "detail", "gagnant", "reference_taux", "nb_concurrents", "nb_offres"):
            self.assertIn(cle, premier)
        self.assertEqual(d["resume"]["nb"], 20)
        self.assertEqual(premier["acheteur"], "COMMUNE DE LIEU1")        # même acheteur en tête
        self.assertTrue(0.8 < d["resume"]["mediane"] < 1.1)

    def test_les_poids_en_parametre_changent_le_classement(self):
        d = self.alice.get("/api/copilote/similaires/AO1", params={"poids": "acheteur:0,objet_nature:0,montant:100"}).json()
        self.assertEqual(d["poids"]["montant"], 100)
        ratios = [abs(np.log(x["estimation"] / 5e6)) for x in d["similaires"][:5]]
        self.assertEqual(ratios, sorted(ratios))
        self.assertEqual(self.alice.get("/api/copilote/similaires/AO1", params={"poids": "bidule:3"}).status_code, 422)

    def test_erreurs(self):
        self.assertEqual(self.alice.get("/api/copilote/similaires/NOPE").status_code, 404)
        r = self.alice.get("/api/copilote/similaires/AO3")
        self.assertEqual(r.status_code, 400)
        self.assertEqual([x["lot"] for x in r.json()["lots"]], ["1", "2"])
        self.assertEqual(self.alice.get("/api/copilote/similaires/AO3", params={"lot": "9"}).status_code, 404)
        self.assertEqual(self.alice.get("/api/copilote/similaires/AO1", params={"lot": "1"}).status_code, 404)

    def test_ao_alloti_se_lit_lot_par_lot(self):
        d1 = self.alice.get("/api/copilote/similaires/AO3", params={"lot": "1"}).json()
        d2 = self.alice.get("/api/copilote/similaires/AO3", params={"lot": "2"}).json()
        self.assertEqual((d1["ao"]["estimation"], d2["ao"]["estimation"]), (1e6, 3e6))
        self.assertEqual((d1["ao"]["lot"], d2["ao"]["lot"]), ("1", "2"))
        self.assertTrue(all(0.3 <= x["estimation"] / 1e6 <= 3 for x in d1["similaires"]))
        self.assertTrue(all(0.3 <= x["estimation"] / 3e6 <= 3 for x in d2["similaires"]))

    def test_acheteur_inconnu_propose_et_range_les_correspondances(self):
        d = self.alice.get("/api/copilote/similaires/AO2").json()
        self.assertEqual((d["acheteur"]["statut"], d["acheteur"]["cles_utilisees"]),
                         ("correspondance", [bases.cle_acheteur("COMMUNE DE LIEU3")]))    # proposition sûre : score 100
        self.assertTrue(d["acheteur"]["propositions"])
        lignes = self.alice.get("/api/copilote/correspondances").json()["correspondances"]
        self.assertTrue(lignes and all(x["statut"] == "proposee" and x["cle_ao"] == d["acheteur"]["cle"] for x in lignes))

    def test_correspondance_validee_par_un_humain_sert_au_score(self):
        cle = bases.cle_acheteur("COMMUNE LIEU3")
        cible = bases.cle_acheteur("COMMUNE DE LIEU4")
        r = self.alice.post("/api/copilote/correspondance",
                            json={"cle_ao": cle, "cle_historique": cible, "statut": "validee", "commentaire": "même commune"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["correspondances"][0]["decide_par"], "Alice")
        d = self.alice.get("/api/copilote/similaires/AO2").json()
        self.assertEqual((d["acheteur"]["statut"], d["acheteur"]["cles_utilisees"]), ("correspondance", [cible]))   # et non la proposition LIEU3
        self.assertTrue(any(x["detail"]["acheteur"]["valeur"] == 1 for x in d["similaires"]))

    def test_correspondance_vers_un_acheteur_inexistant_refusee(self):
        r = self.alice.post("/api/copilote/correspondance",
                            json={"cle_ao": "x", "cle_historique": "n-existe-pas", "statut": "validee"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.alice.get("/api/copilote/correspondances").json()["correspondances"], [])

    def test_l_auteur_vient_de_la_session(self):
        cible = bases.cle_acheteur("COMMUNE DE LIEU4")
        self.alice.post("/api/copilote/correspondance", json={"cle_ao": "x", "cle_historique": cible, "statut": "refusee",
                                                              "auteur": "Mallory"})
        self.assertEqual(self.alice.get("/api/copilote/correspondances").json()["correspondances"][0]["decide_par"], "Alice")

    def test_chercher_un_acheteur(self):
        r = self.alice.get("/api/copilote/acheteurs", params={"q": "lieu3"}).json()["acheteurs"]
        self.assertTrue(r and all("LIEU3" in x["nom"] for x in r))

    def test_rejeu_ne_voit_que_le_passe_de_sa_coupure(self):
        d = self.alice.get("/api/copilote/similaires/REJEU-01").json()
        self.assertEqual(d["ao"]["mode"], "rejeu")
        self.assertEqual(d["ao"]["date_reference"], "2025-07-01")
        self.assertTrue(d["similaires"])
        self.assertTrue(all(x["date"] <= "2025-06-30" for x in d["similaires"]))
        self.assertEqual(d["index"]["calcul"], "jusqua-2025-06-30")
        self.assertLess(d["marches_lus"], 72)

    def test_rejeu_sans_coupure_est_une_erreur(self):
        r = self.alice.get("/api/copilote/similaires/REJEU-02")
        self.assertEqual(r.status_code, 409)

    def test_rejeu_n_ecrit_pas_de_proposition_ni_d_index_courant(self):
        self.alice.get("/api/copilote/similaires/REJEU-01")
        calc = sqlite3.connect(self.d / "calcul.db")
        try:
            tables = {r[0] for r in calc.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertFalse("sim_index" in tables and calc.execute("SELECT count(*) FROM sim_index").fetchone()[0])
        finally:
            calc.close()

    def test_la_base_d_historique_n_est_jamais_modifiee(self):
        avant = hashlib.sha256((self.d / "pv_travaux.db").read_bytes()).hexdigest()
        for ref, params in (("AO1", {}), ("AO2", {}), ("AO3", {"lot": "1"}), ("REJEU-01", {})):
            self.alice.get(f"/api/copilote/similaires/{ref}", params=params)
        self.assertEqual(hashlib.sha256((self.d / "pv_travaux.db").read_bytes()).hexdigest(), avant)

    def test_pas_de_vocabulaire_accusatoire(self):
        import re
        d = json.dumps(self.alice.get("/api/copilote/similaires/AO1").json(), ensure_ascii=False)
        self.assertIsNone(re.search(r"entente|collusion|fraude|cartel|complicit|truqu", d, re.I))

    def test_les_routes_existent_dans_le_schema(self):
        chemins = set(self.app.openapi()["paths"])
        for attendu in ("/api/copilote/similaires/{ref_annonce}", "/api/copilote/correspondances",
                        "/api/copilote/correspondance", "/api/copilote/acheteurs"):
            self.assertIn(attendu, chemins)


class Regions(unittest.TestCase):
    def test_region_par_le_nom_ou_le_lieu(self):
        self.assertEqual(regions.region_de("COMMUNE DE TEMARA"), "Rabat-Salé-Kénitra")
        self.assertEqual(regions.region_de("COMMUNE X", "Taza"), "Fès-Meknès")


if __name__ == "__main__":
    unittest.main()
