import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from copilote import bases, config
from tests import outils


class Connexions(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.d = Path(self.dossier.name)
        outils.creer(self.d / "pv_travaux.db").close()
        ao = sqlite3.connect(self.d / "ao_ouverts.db")
        ao.executescript("CREATE TABLE appels_ouverts (ref_annonce TEXT);"
                         " INSERT INTO appels_ouverts VALUES ('x');")
        ao.close()
        bases.definir_sources(self.d / "pv_travaux.db", self.d / "ao_ouverts.db")
        self.patchs = [mock.patch.object(config, "COPILOTE_CALCUL", self.d / "vol" / "calcul.db"),
                       mock.patch.object(config, "COPILOTE_RETOURS", self.d / "vol" / "retours.db")]
        for p in self.patchs:
            p.start()

    def tearDown(self):
        for p in self.patchs:
            p.stop()
        bases._sources.clear()
        self.dossier.cleanup()

    def test_pv_travaux_est_en_lecture_seule(self):
        db = bases.historique()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO lots VALUES ('r','1',1,'x',1)")
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("CREATE TABLE intrus (a)")
        finally:
            db.close()

    def test_ao_ouverts_est_en_lecture_seule(self):
        db = bases.ao_ouverts()
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM appels_ouverts").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM appels_ouverts")
        finally:
            db.close()

    def test_les_fichiers_lus_ne_sont_jamais_crees(self):
        bases.definir_sources(self.d / "absente.db", self.d / "absente_ao.db")
        with self.assertRaises(FileNotFoundError):
            bases.historique()
        with self.assertRaises(FileNotFoundError):
            bases.ao_ouverts()
        self.assertFalse((self.d / "absente.db").exists())

    def test_les_deux_bases_du_copilote_se_creent_et_s_ecrivent(self):
        for ouvrir, nom in ((bases.calcul, "calcul.db"), (bases.retours, "retours.db")):
            db = ouvrir()
            try:
                db.execute("INSERT INTO meta VALUES ('essai', '1')")
                db.commit()
            finally:
                db.close()
            self.assertTrue((self.d / "vol" / nom).exists())

    def test_calcul_et_retours_sont_deux_fichiers_distincts(self):
        self.assertNotEqual(config.COPILOTE_CALCUL, config.COPILOTE_RETOURS)
        a, b = bases.calcul(), bases.retours()
        try:
            a.execute("INSERT INTO meta VALUES ('seulement_calcul', '1')")
            a.commit()
            self.assertIsNone(b.execute("SELECT 1 FROM meta WHERE cle = 'seulement_calcul'").fetchone())
        finally:
            a.close()
            b.close()

    def test_cle_acheteur_enregistree_en_sql(self):
        db = bases.historique()
        try:
            v = db.execute("SELECT cle_acheteur('M4 / ENAM - OFFICE NATIONAL')").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(v, "officenational")


class Chemins(unittest.TestCase):
    def test_variable_d_environnement_prioritaire(self):
        with mock.patch.dict("os.environ", {"COPILOTE_CALCUL": "/ailleurs/c.db"}):
            self.assertEqual(config._chemin("COPILOTE_CALCUL", "copilote_calcul.db"),
                             Path("/ailleurs/c.db"))

    def test_defaut_a_cote_du_script_ou_sur_le_volume(self):
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("COPILOTE_RETOURS", None)
            p = config._chemin("COPILOTE_RETOURS", "copilote_retours.db")
        self.assertEqual(p.name, "copilote_retours.db")
        self.assertIn(p.parent, (config.ICI, Path("/data")))

    def test_les_deux_chemins_par_defaut_different(self):
        self.assertNotEqual(config.COPILOTE_CALCUL.name, config.COPILOTE_RETOURS.name)


if __name__ == "__main__":
    unittest.main()
