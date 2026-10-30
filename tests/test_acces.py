import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from copilote import acces, bases, config
from copilote.routes import creer_routeur

SECRET = b"secret-d-essai"
CODES = "Alice:code-alice,Bob:code-bob"


def cookie_visiteur(secret=SECRET, nom="Visiteur", age=0):
    return acces.signer(secret, {"n": nom, "t": time.time() - age, "s": "x"})


class Acces(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        d = Path(self.dossier.name)
        self.patchs = [mock.patch.dict("os.environ", {"COPILOTE_ACCES": CODES}),
                       mock.patch.object(config, "COPILOTE_CALCUL", d / "calcul.db"),
                       mock.patch.object(config, "COPILOTE_RETOURS", d / "retours.db")]
        for p in self.patchs:
            p.start()
        acces._echecs.clear()
        self.app = FastAPI()
        self.app.include_router(creer_routeur(SECRET, d / "pv_travaux.db", d / "ao_ouverts.db"))
        self.client = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        for p in self.patchs:
            p.stop()
        bases._sources.clear()
        self.dossier.cleanup()

    def visiteur(self, client=None):
        (client or self.client).cookies.set("pv", cookie_visiteur())

    def entrer(self, code, client=None):
        return (client or self.client).post("/copilote/entrer", data={"code": code})

    # ------------------------------------------------------------------ refus
    def test_sans_aucun_cookie_tout_est_refuse(self):
        self.assertEqual(self.client.get("/api/copilote/etat").status_code, 401)
        r = self.client.get("/copilote")
        self.assertEqual((r.status_code, r.headers["location"]), (302, "/"))

    def test_le_cookie_visiteur_seul_ne_suffit_pas(self):
        self.visiteur()
        self.assertEqual(self.client.get("/api/copilote/etat").status_code, 401)
        r = self.client.get("/copilote")
        self.assertEqual((r.status_code, r.headers["location"]), (302, "/copilote/entrer"))

    def test_le_cookie_copilote_seul_ne_suffit_pas(self):
        self.client.cookies.set("pvc", acces.cookie_copilote(SECRET, "Alice"))
        self.assertEqual(self.client.get("/api/copilote/etat").status_code, 401)

    def test_un_cookie_visiteur_recopie_en_cookie_copilote_est_refuse(self):
        self.visiteur()
        self.client.cookies.set("pvc", cookie_visiteur())
        self.assertEqual(self.client.get("/api/copilote/etat").status_code, 401)

    def test_cookie_copilote_falsifie_ou_expire(self):
        self.visiteur()
        bon = acces.cookie_copilote(SECRET, "Alice")
        self.client.cookies.set("pvc", bon[:-2] + ("aa" if bon[-2:] != "aa" else "bb"))
        self.assertEqual(self.client.get("/api/copilote/etat").status_code, 401)
        vieux = acces.signer(SECRET, {"c": "Alice", "t": time.time() - config.DUREE_SESSION - 5},
                             acces.CONTEXTE_COPILOTE)
        self.client.cookies.set("pvc", vieux)
        self.assertEqual(self.client.get("/api/copilote/etat").status_code, 401)

    def test_cookie_copilote_signe_avec_un_autre_secret(self):
        self.visiteur()
        self.client.cookies.set("pvc", acces.cookie_copilote(b"autre-secret", "Alice"))
        self.assertEqual(self.client.get("/api/copilote/etat").status_code, 401)

    def test_mauvais_code(self):
        self.visiteur()
        r = self.entrer("code-faux")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/copilote/entrer?e=1"))
        self.assertNotIn("pvc", self.client.cookies)

    def test_la_page_de_saisie_exige_le_cookie_visiteur(self):
        self.assertEqual(self.client.get("/copilote/entrer").status_code, 302)
        self.assertEqual(self.entrer("code-alice").status_code, 302)       # renvoyé vers « / »
        self.assertNotIn("pvc", self.client.cookies)

    def test_sans_comptes_configures_personne_n_entre(self):
        self.visiteur()
        with mock.patch.dict("os.environ", {"COPILOTE_ACCES": ""}):
            self.assertEqual(self.entrer("code-alice").status_code, 503)
            self.client.cookies.set("pvc", acces.cookie_copilote(SECRET, "Alice"))
            self.assertEqual(self.client.get("/api/copilote/etat").status_code, 401)

    def test_un_compte_retire_perd_l_acces_tout_de_suite(self):
        self.visiteur()
        self.entrer("code-bob")
        self.assertEqual(self.client.get("/api/copilote/etat").status_code, 200)
        with mock.patch.dict("os.environ", {"COPILOTE_ACCES": "Alice:code-alice"}):
            self.assertEqual(self.client.get("/api/copilote/etat").status_code, 401)

    def test_essais_limites_par_adresse(self):
        self.visiteur()
        for _ in range(acces.ESSAIS_MAX):
            self.assertEqual(self.entrer("faux").status_code, 303)
        self.assertEqual(self.entrer("code-alice").status_code, 429)       # même le bon code

    def test_chaque_route_du_copilote_est_gardee(self):
        """Aucune route /copilote* ou /api/copilote* hors /copilote/entrer n'est ouverte."""
        # Les routes d'un routeur inclus ne sont pas toutes listées dans app.routes selon la
        # version de FastAPI : le schéma OpenAPI, lui, les donne toutes.
        chemins = {c: methodes for c, methodes in self.app.openapi()["paths"].items()
                   if c.startswith(("/copilote", "/api/copilote")) and c != "/copilote/entrer"}
        self.assertTrue(chemins)
        for chemin, methodes in chemins.items():
            for methode in methodes:           # chaque méthode déclarée : une route POST seule répondrait 405 à un GET
                self.assertIn(self.client.request(methode.upper(), chemin).status_code, (301, 302, 401),
                              f"{methode} {chemin}")

    # Les seules routes ouvertes au cookie visiteur seul (page des groupes et ses API). Toute autre route
    # du copilote exige le code COPILOTE_ACCES : ajouter une route ici est une décision, pas un oubli.
    ROUTES_VISITEUR = {("GET", "/copilote/groupes"), ("GET", "/copilote/groupes_calcul.js"),
                       ("GET", "/api/copilote/groupes"), ("GET", "/api/copilote/societes"),
                       ("GET", "/api/copilote/societe/{cle}/liens"), ("GET", "/api/groupes/societe/{cle}/donnees"),
                       ("GET", "/api/copilote/famille/{empreinte}"), ("GET", "/api/copilote/groupe/{gid}"),
                       ("GET", "/api/copilote/avis"), ("POST", "/api/copilote/avis")}

    def test_le_cookie_visiteur_n_ouvre_que_la_page_des_groupes(self):
        self.visiteur()
        publiques = {("GET", "/copilote/entrer"), ("POST", "/copilote/entrer")}
        vues = set()
        for chemin, methodes in self.app.openapi()["paths"].items():
            if not chemin.startswith(("/copilote", "/api/copilote", "/api/groupes")):
                continue
            for methode in methodes:
                vues.add((methode.upper(), chemin))
        self.assertEqual({v for v in vues if v in self.ROUTES_VISITEUR}, self.ROUTES_VISITEUR)
        for methode, chemin in sorted(vues - publiques - self.ROUTES_VISITEUR):
            concret = chemin.replace("{cle}", "a").replace("{empreinte}", "0123abcd").replace("{gid}", "1")
            self.assertIn(self.client.request(methode, concret).status_code, (301, 302, 401),
                          f"{methode} {chemin} devrait exiger le code copilote")
        # …et elles ne répondent ni 401 ni 302 sans code (404 ou 409 : la base d'essai est vide).
        for methode, chemin in sorted(self.ROUTES_VISITEUR):
            concret = chemin.replace("{cle}", "a").replace("{empreinte}", "0123abcd").replace("{gid}", "1")
            r = self.client.request(methode, concret, json={"reponse": "nsp", "groupe": 1} if methode == "POST" else None)
            self.assertNotIn(r.status_code, (302, 401, 403), f"{methode} {chemin}")

    # ------------------------------------------------------------------ accès
    def test_le_code_donne_le_nom_de_son_titulaire(self):
        self.visiteur()
        r = self.entrer("code-bob")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/copilote"))
        cookie = r.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("samesite=lax", cookie.lower())
        etat = self.client.get("/api/copilote/etat")
        self.assertEqual(etat.status_code, 200)
        self.assertEqual(etat.json()["auteur"], "Bob")
        self.assertEqual(self.client.get("/copilote").status_code, 200)

    def test_etat_decrit_les_quatre_bases(self):
        self.visiteur()
        self.entrer("code-alice")
        bases_ = self.client.get("/api/copilote/etat").json()["bases"]
        self.assertEqual(set(bases_), {"pv_travaux.db", "ao_ouverts.db", "copilote_calcul.db",
                                       "copilote_retours.db"})
        self.assertEqual(bases_["pv_travaux.db"]["mode"], "lecture seule")
        self.assertEqual(bases_["ao_ouverts.db"]["mode"], "lecture seule")
        self.assertEqual(bases_["copilote_calcul.db"]["mode"], "lecture-écriture")
        # Les deux bases du copilote sont créées par le premier appel ; les deux autres restent absentes.
        self.assertTrue(bases_["copilote_calcul.db"]["existe"])
        self.assertTrue(bases_["copilote_retours.db"]["existe"])
        self.assertFalse(bases_["pv_travaux.db"]["existe"])

    def test_deja_entre_la_page_de_saisie_renvoie_a_l_accueil(self):
        self.visiteur()
        self.entrer("code-alice")
        r = self.client.get("/copilote/entrer")
        self.assertEqual((r.status_code, r.headers["location"]), (302, "/copilote"))


class Comptes(unittest.TestCase):
    def test_lecture_de_copilote_acces(self):
        with mock.patch.dict("os.environ", {"COPILOTE_ACCES": " Alice : a:b ,, mal formé, :x, Bob:  ,Alice:autre"}):
            self.assertEqual(acces.comptes(), {"Alice": "a:b"})      # le code peut contenir « : »
        with mock.patch.dict("os.environ", {"COPILOTE_ACCES": ""}):
            self.assertEqual(acces.comptes(), {})

    def test_identifier(self):
        with mock.patch.dict("os.environ", {"COPILOTE_ACCES": CODES}):
            self.assertEqual(acces.identifier("code-alice"), "Alice")
            self.assertIsNone(acces.identifier("code-alic"))
            self.assertIsNone(acces.identifier(""))


if __name__ == "__main__":
    unittest.main()
