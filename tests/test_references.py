import tempfile
import unittest
from pathlib import Path

from copilote import bases
from copilote.donnees import charger_unites
from copilote.references import GRILLE, meilleur, meilleur_par_tranche, par_tranche, taux_de_victoire, unites_eligibles
from tests import outils


class Grille(unittest.TestCase):
    def test_101_taux_de_080_a_105_par_pas_de_0_0025(self):
        self.assertEqual(len(GRILLE), 101)
        self.assertEqual((GRILLE[0], GRILLE[-1]), (0.80, 1.05))
        pas = {round(b - a, 4) for a, b in zip(GRILLE, GRILLE[1:])}
        self.assertEqual(pas, {0.0025})


class Victoire(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        chemin = Path(self.dossier.name) / "h.db"
        db = outils.creer(chemin)
        # Deux marchés attribués où une offre à 0,95 passe devant (voir test_donnees pour m1)…
        outils.marche(db, "m1", 100, [("a", 90), ("b", 100), ("c", 110)], gagnant="b")
        outils.marche(db, "m2", 100, [("a", 90), ("b", 100), ("c", 110)], gagnant="b")
        # … un infructueux, un sans estimation : tous deux hors de la mesure.
        outils.marche(db, "m3", 100, [("a", 90), ("b", 100)], gagnant=None)
        outils.marche(db, "m4", None, [("a", 90), ("b", 100)], gagnant="a")
        db.commit()
        db.close()
        lecture = bases._lecture_seule(chemin)
        try:
            self.unites = charger_unites(lecture)
        finally:
            lecture.close()

    def tearDown(self):
        self.dossier.cleanup()

    def test_eligibles(self):
        self.assertEqual({u.ref for u in unites_eligibles(self.unites)}, {"m1", "m2"})

    def test_taux_de_victoire(self):
        res = taux_de_victoire(self.unites, [0.95, 1.0, 1.05])
        self.assertEqual(res[0.95], {"n": 2, "victoires": 2, "taux_victoire": 1.0})
        self.assertEqual(res[1.0]["victoires"], 0)          # ex aequo avec b : b reste premier
        # À 1,05 : offres 90 / 100 / 110 / 105, moyenne 101,25, référence 100,625 ; b (100) passe devant.
        self.assertEqual(res[1.05]["victoires"], 0)

    def test_meilleur_prend_le_plus_bas_taux_a_egalite(self):
        res = {0.95: {"taux_victoire": 0.5}, 0.96: {"taux_victoire": 0.5}, 0.97: {"taux_victoire": 0.2}}
        self.assertEqual(meilleur(res)[0], 0.95)
        self.assertIsNone(meilleur({}))

    def test_par_tranche(self):
        t = par_tranche(self.unites, [0.95])
        self.assertEqual(t["3-4"], {"n": 2, 0.95: 1.0})
        self.assertEqual(t["1-2"], {"n": 0, 0.95: None})

    def test_meilleur_par_tranche(self):
        t = meilleur_par_tranche(self.unites, [0.95, 1.0, 1.05])
        self.assertEqual(t["3-4"], {"n": 2, "taux": 0.95, "taux_victoire": 1.0})
        self.assertEqual(t["1-2"], {"n": 0, "taux": None, "taux_victoire": None})
        self.assertEqual(set(t), {"1-2", "3-4", "5-7", "8+"})


if __name__ == "__main__":
    unittest.main()
