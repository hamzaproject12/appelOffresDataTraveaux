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


class Redirections(unittest.TestCase):
    """Toutes les redirections du site, par l'application complète : le gestionnaire d'erreurs de
    serveur.py doit laisser passer le Location (en production, /copilote/groupes affichait du JSON)."""

    def va(self, r, code: int, cible: str):
        self.assertEqual(r.status_code, code, r.text)
        self.assertEqual(r.headers.get("location"), cible)
        self.assertNotIn("erreur", r.text)

    def test_accueil_entrer_deconnexion(self):
        client = TestClient(serveur.app, follow_redirects=False)
        with mock.patch.object(serveur, "PROBLEME", ""):          # la base d'essai est vide
            self.assertEqual(client.get("/").status_code, 200)
        self.va(client.post("/entrer", data={"nom": "x"}), 302, "/?erreur=Merci+d%27indiquer+votre+nom.")
        r = client.post("/entrer", data={"nom": "Visiteur d'essai"})
        self.va(r, 302, serveur.ACCUEIL_CONNECTE)
        self.assertIn("pv=", r.headers.get("set-cookie", ""))
        with mock.patch.object(serveur, "PROBLEME", ""):
            self.va(client.get("/"), 302, serveur.ACCUEIL_CONNECTE)
        self.va(client.get("/deconnexion"), 302, "/")

    def test_pages_sans_cookie_renvoient_a_l_accueil(self):
        client = TestClient(serveur.app, follow_redirects=False)
        for chemin in ("/app", "/ao-ouverts", "/copilote", "/copilote/groupes", "/copilote/entrer"):
            self.va(client.get(chemin), 302, "/")

    def test_copilote_renvoie_au_formulaire_de_code(self):
        client = TestClient(serveur.app, follow_redirects=False)
        client.post("/entrer", data={"nom": "Visiteur d'essai"})
        self.va(client.get("/copilote"), 302, "/copilote/entrer")
        self.assertEqual(client.get("/copilote/groupes").status_code, 200)      # visible sans code copilote
        self.assertEqual(client.get("/copilote/entrer").status_code, 200)
        self.va(client.post("/copilote/entrer", data={"code": "faux"}), 303, "/copilote/entrer?e=1")
        self.va(client.post("/copilote/entrer", data={"code": "code-alice"}), 303, "/copilote")
        self.va(client.get("/copilote/entrer"), 302, "/copilote")
        self.assertEqual(client.get("/copilote/groupes").status_code, 200)
        self.assertEqual(client.get("/api/copilote/etat").status_code, 200)

    def test_administration(self):
        client = TestClient(serveur.app, follow_redirects=False)
        with mock.patch.object(serveur, "ADMIN_MOTDEPASSE", "mdp-essai"):
            self.va(client.get("/admin/journal.csv"), 302, "/admin")
            self.va(client.post("/admin/entrer", data={"motdepasse": "faux"}), 302, "/admin?e=1")
            r = client.post("/admin/entrer", data={"motdepasse": "mdp-essai"})
            self.va(r, 302, "/admin")
            self.assertIn("adm=", r.headers.get("set-cookie", ""))
            self.assertEqual(client.get("/admin").status_code, 200)
            self.va(client.get("/admin/sortir"), 302, "/admin")

    def test_l_api_garde_ses_reponses_json(self):
        client = TestClient(serveur.app, follow_redirects=False)
        r = client.get("/api/copilote/etat")
        self.assertEqual((r.status_code, r.json()), (401, {"erreur": "Accès au copilote requis"}))


class RejeuContrat(unittest.TestCase):
    """Les annonces rejouées sont acceptées telles quelles par le vrai POST /api/v1/appels_ouverts."""

    def test_les_annonces_rejouees_passent_le_contrat_du_service(self):
        from copilote import rejouer_ao
        from tests.test_rejeu import base_de_rejeu
        with tempfile.TemporaryDirectory() as tmp:
            base_de_rejeu(Path(tmp) / "h.db")
            hist = bases._lecture_seule(Path(tmp) / "h.db")
            import sqlite3
            calc = sqlite3.connect(Path(tmp) / "c.db")
            calc.row_factory = sqlite3.Row
            try:
                rejouer_ao.preparer(calc)
                choisis = rejouer_ao.choisir(rejouer_ao.candidats(hist), 12, 3, 0)
                rejouer_ao.enregistrer(calc, choisis)
                annonces = rejouer_ao.annonces_a_envoyer(calc)
            finally:
                hist.close()
                calc.close()
        client = TestClient(serveur.app, follow_redirects=False)
        corps = {"clients": [rejouer_ao.CLIENT], "appels_ouverts": annonces}
        sans_jeton = client.post("/api/v1/appels_ouverts", json=corps)
        self.assertEqual(sans_jeton.status_code, 401)
        r = client.post("/api/v1/appels_ouverts", json=corps, headers={"Authorization": "Bearer j"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["recus"], r.json()["crees"], r.json()["rejetes"]), (12, 12, []))
        client.post("/entrer", data={"nom": "Visiteur d'essai"})
        lignes = client.get("/api/v1/appels_ouverts", params={"client": "Test", "taille": 50}).json()["lignes"]
        self.assertEqual(len(lignes), 12)                                  # toutes encore ouvertes : date limite à venir
        par_ref = {l["ref_annonce"]: l for l in lignes}
        for a in annonces:
            l = par_ref[a["ref_annonce"]]
            self.assertEqual(l["clients"], ["Test"])
            self.assertEqual(l["acheteur"], a["acheteur"])
            self.assertAlmostEqual(l["estimation"], a["estimation"])
            self.assertEqual([x["lot"] for x in l["lots"]], [x["lot"] for x in a.get("lots", [])])
            self.assertEqual(sorted((q["libelle"], q["classe"]) for q in l["qualifications"]),
                             sorted((q["libelle"], q["classe"]) for q in a["qualifications"]))
        # Le renvoi est idempotent : rien de plus en base.
        r2 = client.post("/api/v1/appels_ouverts", json=corps, headers={"Authorization": "Bearer j"})
        self.assertEqual((r2.json()["crees"], r2.json()["maj"]), (0, 12))


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
