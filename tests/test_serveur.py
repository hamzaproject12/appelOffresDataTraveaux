"""Le copilote tel que serveur.py l'embarque : inclusion du routeur et contrôle de séparation."""
import contextlib
import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from copilote import bases
from tests import outils

ICI = Path(__file__).resolve().parent.parent
serveur = None
dossier = None
ENV_AVANT = dict(os.environ)


def setUpModule():
    global serveur, dossier
    dossier = Path(tempfile.mkdtemp(prefix="essai_copilote_"))
    outils.creer(dossier / "pv_travaux.db").close()          # présente : serveur ne dépliera rien
    os.environ.update({
        "BASE": str(dossier / "pv_travaux.db"), "JOURNAL": str(dossier / "journal.db"),
        "AO_OUVERTS": str(dossier / "ao_ouverts.db"),
        "COPILOTE_CALCUL": str(dossier / "copilote_calcul.db"),
        "COPILOTE_RETOURS": str(dossier / "copilote_retours.db"),
        "COPILOTE_ACCES": "Alice:code-alice", "SECRET": "secret-d-essai", "JETON_SERVICE": "j"})
    sys.path.insert(0, str(ICI))
    import copilote.config
    importlib.reload(copilote.config)                       # relit COPILOTE_CALCUL / COPILOTE_RETOURS
    import serveur as s
    serveur = importlib.reload(s) if "serveur" in sys.modules and hasattr(s, "app") else s


def tearDownModule():
    import copilote.config
    os.environ.clear()
    os.environ.update(ENV_AVANT)
    importlib.reload(copilote.config)
    sys.modules.pop("serveur", None)
    bases._sources.clear()
    shutil.rmtree(dossier, ignore_errors=True)


class Integration(unittest.TestCase):
    def test_le_routeur_est_inclus_et_protege(self):
        client = TestClient(serveur.app, follow_redirects=False)
        self.assertEqual(client.get("/api/copilote/etat").status_code, 401)
        self.assertEqual(client.get("/copilote").status_code, 302)

    def test_parcours_complet_visiteur_puis_code(self):
        client = TestClient(serveur.app, follow_redirects=False)
        client.post("/entrer", data={"nom": "Visiteur d'essai"})                 # cookie « pv » de serveur.py
        self.assertEqual(client.get("/api/copilote/etat").status_code, 401)       # encore refusé
        r = client.post("/copilote/entrer", data={"code": "code-alice"})
        self.assertEqual(r.status_code, 303)
        etat = client.get("/api/copilote/etat")
        self.assertEqual((etat.status_code, etat.json()["auteur"]), (200, "Alice"))
        self.assertTrue((dossier / "copilote_calcul.db").exists())
        self.assertTrue((dossier / "copilote_retours.db").exists())

    def test_les_routes_existantes_ne_changent_pas(self):
        client = TestClient(serveur.app, follow_redirects=False)
        self.assertEqual(client.get("/api/v1/appels_ouverts").status_code, 401)
        self.assertEqual(client.get("/ao-ouverts").status_code, 302)

    def test_cle_acheteur_identique_a_celle_du_serveur(self):
        for nom in ("M4 / ENAM - OFFICE NATIONAL", "COMMUNE D'ESSAI", "", None, "ONEE - BRANCHE EAU"):
            self.assertEqual(bases.cle_acheteur(nom), serveur.cle_acheteur(nom), nom)


class Separation(unittest.TestCase):
    def refuse(self, **remplacements) -> str | None:
        with contextlib.ExitStack() as pile:
            for nom, valeur in remplacements.items():
                pile.enter_context(mock.patch.object(serveur, nom, valeur))
            try:
                serveur.verifier_separation_ao()
            except RuntimeError as e:
                return str(e)
        return None

    def test_configuration_saine_acceptee(self):
        self.assertIsNone(self.refuse())

    def test_copilote_sur_la_base_servie_ou_son_journal_ou_les_ao(self):
        for variable in ("COPILOTE_CALCUL", "COPILOTE_RETOURS"):
            for cible in (serveur.BASE, serveur.JOURNAL_BASE, serveur.AO_BASE,
                          Path(str(serveur.BASE) + "-wal"), Path(str(serveur.BASE) + ".tmp"),
                          Path(str(serveur.BASE) + ".amorce"), Path(str(serveur.BASE) + ".gz")):
                message = self.refuse(**{variable: cible})
                self.assertIsNotNone(message, f"{variable} -> {cible.name}")
                self.assertIn(variable, message)

    def test_calcul_et_retours_ne_partagent_pas_un_fichier(self):
        message = self.refuse(COPILOTE_RETOURS=serveur.COPILOTE_CALCUL)
        self.assertIsNotNone(message)
        self.assertIn("COPILOTE_CALCUL et COPILOTE_RETOURS", message)

    def test_chemin_relatif_equivalent_est_reconnu(self):
        detour = serveur.BASE.parent / "sous" / ".." / serveur.BASE.name
        self.assertIsNotNone(self.refuse(COPILOTE_RETOURS=detour))

    def test_le_controle_des_ao_est_conserve(self):
        self.assertIsNotNone(self.refuse(AO_BASE=serveur.BASE))


if __name__ == "__main__":
    unittest.main()
