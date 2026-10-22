import tempfile
import unittest
from pathlib import Path

import construire_base
from copilote import bases, donnees
from copilote.donnees import CLE_FICTIVE, charger_unites, concordance, rejouer_attribution
from tests import outils

REELLE = Path(__file__).resolve().parent.parent / "pv_travaux.db"


class Rejeu(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.chemin = Path(self.dossier.name) / "h.db"
        self.db = outils.creer(self.chemin)
        # estimation 100, offres 90 / 100 / 110 : moyenne 100, prix de référence 100.
        outils.marche(self.db, "m1", 100, [("a", 90), ("b", 100), ("c", 110)], gagnant="b")
        # Toutes les offres au-dessus du prix de référence : la plus proche par excès gagne.
        outils.marche(self.db, "m2", 100, [("a", 150), ("b", 160), ("c", 170)], gagnant="a")
        # Une offre écartée au dossier n'entre ni dans la moyenne ni dans le classement.
        outils.marche(self.db, "m3", 100, [("a", 50, "ecarte_dossier_administratif_technique"),
                                           ("b", 100), ("c", 110)], gagnant="b")
        # Republication : pas principale, donc jamais lue.
        outils.marche(self.db, "m4", 100, [("a", 100)], gagnant="a", principale=0)
        self.db.commit()
        self.db.close()
        self.lecture = bases._lecture_seule(self.chemin)
        self.unites = {u.identifiant: u for u in charger_unites(self.lecture)}

    def tearDown(self):
        self.lecture.close()
        self.dossier.cleanup()

    def test_seules_les_principales_sont_lues(self):
        self.assertEqual(set(self.unites), {"m1", "m2", "m3"})

    def test_taux_sont_montant_sur_estimation(self):
        taux = [c["taux"] for c in self.unites["m1"].concurrents]
        self.assertEqual(taux, [0.9, 1.0, 1.1])

    def test_gagnant_le_plus_proche_du_prix_de_reference_sans_le_depasser(self):
        a = rejouer_attribution(self.unites["m1"])
        self.assertEqual((a.moyenne, a.reference, a.nb_offres, a.cle_gagnant), (100, 100, 3, "b"))

    def test_gagnant_par_exces_quand_tout_est_au_dessus(self):
        a = rejouer_attribution(self.unites["m2"])
        self.assertEqual((a.reference, a.cle_gagnant), (130, "a"))

    def test_offre_ecartee_hors_du_calcul(self):
        a = rejouer_attribution(self.unites["m3"])
        self.assertEqual((a.moyenne, a.nb_offres, a.cle_gagnant), (105, 2, "b"))

    def test_offre_fictive_entre_dans_la_moyenne_et_peut_gagner(self):
        # offres 90 / 95 / 100 / 110 : moyenne 98,75, référence 99,375 ; 95 est la plus proche par défaut.
        a = rejouer_attribution(self.unites["m1"], 0.95)
        self.assertEqual((a.nb_offres, a.cle_gagnant), (4, CLE_FICTIVE))
        self.assertAlmostEqual(a.reference, 99.375, places=2)        # prix_de_reference arrondit à 0,01

    def test_offre_fictive_a_egalite_ne_passe_pas_devant(self):
        # À 1,00 elle ex aequo avec b (100) : le classement garde l'ordre de dépôt, b reste premier.
        self.assertEqual(rejouer_attribution(self.unites["m1"], 1.0).cle_gagnant, "b")

    def test_offre_fictive_ignoree_sans_estimation(self):
        u = self.unites["m1"]
        sans = donnees.Unite(**{**u.__dict__, "estimation": None})
        a = rejouer_attribution(sans, 0.95)
        self.assertIsNone(a.reference)
        self.assertNotEqual(a.cle_gagnant, CLE_FICTIVE)

    def test_la_logique_est_importee_pas_recopiee(self):
        for nom in ("prix_de_reference", "classement_commission", "offres_du_marche",
                    "dans_la_fourchette", "offre_de"):
            self.assertIs(getattr(donnees, nom), getattr(construire_base, nom), nom)
        self.assertNotIn("def prix_de_reference", Path(donnees.__file__).read_text(encoding="utf-8"))


class Lots(unittest.TestCase):
    def test_un_marche_alloti_se_lit_lot_par_lot(self):
        with tempfile.TemporaryDirectory() as d:
            chemin = Path(d) / "h.db"
            db = outils.creer(chemin)
            outils.marche(db, "L1", None, [("a", 90, "admis", "1"), ("b", 110, "admis", "1"),
                                           ("c", 200, "admis", "2"), ("a", 180, "admis", "1,2"),
                                           ("d", 120, "depose", "")],
                          nb_lots=2, gagnant=None)
            outils.lot(db, "L1", "1", 100, gagnant="a")
            outils.lot(db, "L1", "2", 200, gagnant="c")
            db.commit()
            db.close()
            lecture = bases._lecture_seule(chemin)
            try:
                unites = {u.identifiant: u for u in charger_unites(lecture)}
            finally:
                lecture.close()
        self.assertEqual(set(unites), {"L1#1", "L1#2"})                # jamais le marché lui-même
        self.assertEqual(unites["L1#1"].estimation, 100)
        self.assertEqual(unites["L1#2"].estimation, 200)
        self.assertEqual([c["cle"] for c in unites["L1#1"].concurrents], ["a", "b", "a"])
        self.assertEqual([c["cle"] for c in unites["L1#2"].concurrents], ["c", "a"])
        self.assertEqual(unites["L1#1"].concurrents[2]["taux"], 1.8)   # 180 / 100 : l'estimation du lot 1
        self.assertEqual(unites["L1#1"].cle_gagnant, "a")


class Concordance(unittest.TestCase):
    def test_comptes_et_motifs_d_exclusion(self):
        with tempfile.TemporaryDirectory() as d:
            chemin = Path(d) / "h.db"
            db = outils.creer(chemin)
            outils.marche(db, "ok", 100, [("a", 90), ("b", 100), ("c", 110)], gagnant="b",
                          prix_reference=100)
            outils.marche(db, "faux", 100, [("a", 90), ("b", 100), ("c", 110)], gagnant="c",
                          prix_reference=100)
            outils.marche(db, "libre", 100, [("a", 90), ("b", 100)], gagnant=None)
            outils.marche(db, "sans_estimation", None, [("a", 90), ("b", 100)], gagnant="a")
            outils.marche(db, "douteuse", 100, [("a", 100)], gagnant="a", douteuse=1,
                          prix_reference=100)
            db.commit()
            db.close()
            lecture = bases._lecture_seule(chemin)
            try:
                c = concordance(charger_unites(lecture))
            finally:
                lecture.close()
        g = c["global"]
        self.assertEqual((g["unites"], g["non_attribuees"], g["sans_reference"], g["comparables"],
                          g["concordants"]), (5, 1, 1, 3, 2))
        self.assertAlmostEqual(g["taux"], 2 / 3)
        self.assertEqual(c["par_annee"]["2026"]["comparables"], 2)
        self.assertEqual(c["par_annee"]["date douteuse"]["comparables"], 1)
        self.assertEqual(g["reference_differente"], 0)
        self.assertEqual(c["par_tranche"]["3-4"]["comparables"], 2)
        self.assertEqual(c["par_tranche"]["1-2"]["comparables"], 1)


@unittest.skipUnless(REELLE.exists(), "pv_travaux.db absente")
class SurLaVraieBase(unittest.TestCase):
    """Le rejeu doit retrouver, sur toute la base, ce que construire_base.py a écrit."""

    @classmethod
    def setUpClass(cls):
        db = bases._lecture_seule(REELLE)
        try:
            cls.unites = charger_unites(db)
        finally:
            db.close()

    def test_il_y_a_des_marches_et_des_lots(self):
        self.assertGreater(len(self.unites), 15000)
        self.assertTrue(any(u.lot is not None for u in self.unites))

    def test_prix_de_reference_identique_a_celui_de_la_base(self):
        ecarts = []
        for u in self.unites:
            if u.prix_reference_stocke is None:
                continue
            a = rejouer_attribution(u)
            if a.reference is None or abs(a.reference - u.prix_reference_stocke) > 0.01:
                ecarts.append(u.identifiant)
        self.assertEqual(ecarts[:10], [])

    def test_concordance_au_dessus_de_80_pour_cent(self):
        g = concordance(self.unites)["global"]
        self.assertGreater(g["taux"], 0.80)


if __name__ == "__main__":
    unittest.main()
