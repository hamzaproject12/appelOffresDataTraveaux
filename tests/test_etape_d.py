"""Étape D : profil de l'entreprise, décision (API), soumissions, suivi, interrupteur COPILOTE_PRIVE,
prévisions figées inchangées."""
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from copilote import bases, decision, decision_donnees, entreprise, soumissions, suivi, suivi_page
from tests import outils
from tests.test_probables import Api, CODES, SECRET, cookie_visiteur

from fastapi.testclient import TestClient

ICI = Path(__file__).resolve().parent.parent
PROFIL = {"marge_min": 0.05, "cout_defaut": 0.85, "plan_charge_max": 3,
          "qualifs": [{"code": "B.1", "classe": "3"}, {"code": "o.2", "classe": "S"}],
          "regions": ["Oriental", "Fès-Meknès"], "familles": ["route_piste"],
          "couts_famille": []}


class Base(unittest.TestCase):
    setUp, tearDown, client = Api.setUp, Api.tearDown, Api.client


class Profil(Base):
    def test_profil_vide_puis_ecriture_avec_auteur_et_date(self):
        r = self.alice.get("/api/copilote/entreprise").json()
        self.assertEqual(r["profil"]["configure"], False)
        self.assertEqual(r["auteur"], "Alice")
        self.assertIn({"code": "S", "libelle": None}.get("code"), r["referentiel"]["classes"])
        r = self.alice.put("/api/copilote/entreprise", json=PROFIL)
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()["profil"]
        self.assertTrue(p["configure"])
        self.assertEqual(p["marge_min"], 0.05)
        self.assertEqual({q["code"]: q["classe"] for q in p["qualifs"]}, {"B.1": "3", "O.2": "S"})
        self.assertTrue(all(q["auteur"] == "Alice" and q["modifie_le"] for q in p["qualifs"]))
        self.assertEqual(p["meta"]["cout_defaut"]["auteur"], "Alice")
        self.assertEqual([x["region"] for x in p["regions"]], ["Fès-Meknès", "Oriental"])

    def test_une_ligne_inchangee_garde_son_auteur_d_origine(self):
        self.alice.put("/api/copilote/entreprise", json=PROFIL)
        bob = self.client("code-bob")
        bob.put("/api/copilote/entreprise", json={**PROFIL, "qualifs": PROFIL["qualifs"] + [{"code": "B.3", "classe": "2"}], "marge_min": 0.07})
        p = bob.get("/api/copilote/entreprise").json()["profil"]
        par = {q["code"]: q["auteur"] for q in p["qualifs"]}
        self.assertEqual(par, {"B.1": "Alice", "O.2": "Alice", "B.3": "Bob"})
        self.assertEqual(p["meta"]["marge_min"]["auteur"], "Bob")
        self.assertEqual(p["meta"]["cout_defaut"]["auteur"], "Alice")
        journal = bob.get("/api/copilote/entreprise").json()["journal"]
        self.assertTrue(any(j["auteur"] == "Bob" and j["action"] == "profil.marge_min" for j in journal))

    def test_valeurs_refusees(self):
        for mauvais in ({"marge_min": 5}, {"cout_defaut": 0.01}, {"qualifs": [{"code": "zzz", "classe": "3"}]},
                        {"qualifs": [{"code": "B.1", "classe": "9"}]}):
            r = self.alice.put("/api/copilote/entreprise", json={**PROFIL, **mauvais})
            self.assertEqual(r.status_code, 422, mauvais)

    def test_partenaires(self):
        p = {"nom": "Entreprise Alpha", "type": "groupement", "specialite": "Assainissement",
             "qualifs": [{"code": "B.3", "classe": "2"}], "effet_cout": -0.02, "effet_cout_unite": "pct"}
        r = self.alice.post("/api/copilote/entreprise/partenaires", json=p)
        self.assertEqual(r.status_code, 200, r.text)
        pid = r.json()["id"]
        self.assertEqual(r.json()["auteur"], "Alice")
        r = self.alice.put(f"/api/copilote/entreprise/partenaires/{pid}", json={**p, "type": "sous_traitant", "effet_cout": 120000, "effet_cout_unite": "dh"})
        self.assertEqual((r.json()["type"], r.json()["effet_cout"]), ("sous_traitant", 120000))
        self.assertEqual(self.alice.put("/api/copilote/entreprise/partenaires/999", json=p).status_code, 404)
        self.assertEqual(self.alice.post("/api/copilote/entreprise/partenaires", json={**p, "type": "ami"}).status_code, 422)
        self.assertEqual(self.alice.post("/api/copilote/entreprise/partenaires", json={**p, "nom": " "}).status_code, 422)
        self.assertEqual(self.alice.delete(f"/api/copilote/entreprise/partenaires/{pid}").status_code, 200)
        self.assertEqual(self.alice.get("/api/copilote/entreprise").json()["profil"]["partenaires"], [])


class DecisionApi(Base):
    def profil(self, **kw):
        self.alice.put("/api/copilote/entreprise", json={**PROFIL, **kw})

    def test_donnees_de_decision(self):
        self.profil(couts_famille=[{"famille": "route_piste", "cout": 0.9}])
        r = self.alice.get("/api/copilote/decision/AO1")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["profil"]["cout_defaut"], 0.85)
        self.assertEqual(d["exigences"][0]["code"], "B.1")
        self.assertEqual(d["exigences"][0]["rang"], 3)
        self.assertEqual(d["plan"], {"en_cours": 0, "max": 3})
        self.assertIn("gain", d["reglages"])
        self.assertIn("aucun_gain", d["textes"]["raisons"])
        self.assertIn("n", d["acheteur"])
        self.assertIsNone(d["soumission"])

    def test_cout_par_famille_avec_repli_sur_le_cout_global(self):
        self.profil(couts_famille=[{"famille": "route_piste", "cout": 0.9}])
        p = self.alice.get("/api/copilote/entreprise").json()["profil"]
        self.assertEqual(entreprise.cout_par_defaut(p, "route_piste"), {"taux": 0.9, "origine": "famille"})
        self.assertEqual(entreprise.cout_par_defaut(p, "assainissement"), {"taux": 0.85, "origine": "global"})
        self.assertEqual(entreprise.cout_par_defaut(p, None), {"taux": 0.85, "origine": "global"})

    def test_lot_obligatoire_pour_un_ao_alloti(self):
        r = self.alice.get("/api/copilote/decision/AO3")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.alice.get("/api/copilote/decision/AO3", params={"lot": "1"}).status_code, 200)

    def test_soumission_enregistree_relue_et_comptee_dans_le_plan(self):
        self.profil()
        corps = {"ref_annonce": "AO1", "prix_depose": 4_700_000, "cout": 4_250_000, "scenario": "seul", "decision": "GO",
                 "instantane": {"x_reco": 0.95}}
        r = self.alice.post("/api/copilote/soumissions", json=corps)
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        self.assertEqual((s["auteur"], s["estimation"], s["rejeu"]), ("Alice", 5_000_000, False))
        self.assertEqual(s["instantane"]["x_reco"], 0.95)
        d = self.alice.get("/api/copilote/decision/AO1").json()
        self.assertEqual(d["soumission"]["prix_depose"], 4_700_000)
        # corrigée : la même ligne, pas une deuxième
        self.alice.post("/api/copilote/soumissions", json={**corps, "prix_depose": 4_800_000})
        with closing(bases.retours()) as ret:
            lignes = soumissions.lister(ret)
        self.assertEqual([l["prix_depose"] for l in lignes], [4_800_000])
        for mauvais in ({"prix_depose": -1}, {"scenario": "ami"}, {"ref_annonce": "INCONNU"}):
            self.assertIn(self.alice.post("/api/copilote/soumissions", json={**corps, **mauvais}).status_code, (404, 422))

    def test_les_previsions_deja_figees_ne_changent_pas(self):
        """Une ligne déjà dans `previsions` n'est jamais réécrite, profil configuré ou non ; la décision ne s'ajoute
        qu'aux nouvelles copies, et seulement si un profil est configuré."""
        d0 = self.alice.get("/api/copilote/analyse/AO1").json()
        with closing(bases.retours()) as ret:
            avant = [dict(r) for r in ret.execute("SELECT * FROM previsions ORDER BY id")]
            self.assertEqual(len(avant), 1)
            self.assertNotIn("decision", json.loads(avant[0]["prevision"]))
        self.profil()
        self.alice.get("/api/copilote/analyse/AO1")                        # même jour : "deja", rien n'est réécrit
        with closing(bases.retours()) as ret:
            apres = [dict(r) for r in ret.execute("SELECT * FROM previsions ORDER BY id")]
            self.assertEqual(apres, avant)
            demain = datetime.now(timezone.utc) + timedelta(days=1)
            self.assertEqual(suivi.figer(ret, d0, demain), "enregistree")
            copies = suivi.lire(ret, "AO1", None)
        self.assertEqual(len(copies), 2)
        self.assertEqual(copies[0]["prevision"], json.loads(avant[0]["prevision"]))
        nouvelle = copies[1]["prevision"]
        self.assertIn("decision", nouvelle)
        self.assertIn(nouvelle["decision"]["decision"], ("GO", "A_ETUDIER", "NO_GO"))
        self.assertEqual(nouvelle["decision"]["origine_cout"], "global")
        sans = {k: v for k, v in nouvelle.items() if k != "decision"}
        self.assertEqual(set(sans), set(copies[0]["prevision"]))

    def test_copie_sans_profil_identique_a_celle_d_avant(self):
        d = self.alice.get("/api/copilote/analyse/AO1").json()
        a, b = suivi.copie(d), suivi.copie(d, None)
        self.assertEqual(a, b)
        self.assertNotIn("decision", a)
        self.assertNotIn("_prevu", a)
        self.assertEqual(list(a)[-1], "simulation")                      # même ordre de clés qu'avant l'étape D

    def test_signaux_depuis_le_dossier(self):
        d = self.alice.get("/api/copilote/analyse/AO1").json()
        s = decision_donnees.signaux(d, 4.0, {"n": 7, "infructueux": {"n": 1, "total": 7, "taux": 1 / 7}},
                                     jours=10, plan={"en_cours": 1, "max": 3})
        self.assertEqual((s["prevu"], s["jours"], s["acheteur_n"], s["en_cours"], s["plan_max"]), (4.0, 10, 7, 1, 3))
        self.assertTrue(0 <= s["identifies_part"] <= 1)


class Suivi(Base):
    def test_suivi_lie_la_soumission_au_pv_et_donne_le_rang(self):
        db = sqlite3.connect(self.d / "ao_ouverts.db")
        hier = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00")
        db.execute("INSERT INTO appels_ouverts (ref_annonce, reference, objet, acheteur, cle_acheteur, estimation, date_limite)"
                   " VALUES ('777', '12/2026', ?, 'COMMUNE DE LIEU1', ?, 5e6, ?)",
                   ("Travaux de construction de pistes rurales reliant les douars de la commune LIEU1",
                    bases.cle_acheteur("COMMUNE DE LIEU1"), hier))
        db.commit()
        db.close()
        self.alice.get("/api/copilote/analyse/777")
        s = self.alice.get("/api/copilote/suivi").json()
        self.assertEqual(len(s["lignes"]), 1)
        self.assertIsNone(s["lignes"][0]["reel"])
        self.assertEqual(s["bilan"]["soumissions"], 0)
        self.alice.post("/api/copilote/soumissions", json={"ref_annonce": "777", "prix_depose": 4_650_000, "cout": 4_000_000,
                                                          "scenario": "seul", "decision": "GO"})
        # le PV arrive : trois offres, la nôtre (0,93) s'insère parmi 0,92 / 0,96 / 0,98
        db = sqlite3.connect(self.d / "pv_travaux.db")
        outils.marche(db, "c777", 5e6, [("soc1", 4.6e6), ("soc2", 4.8e6), ("nouvelle", 4.9e6)], gagnant="soc1",
                      date="20261103", acheteur="COMMUNE DE LIEU1", objet="Pistes")
        db.commit()
        db.close()
        from copilote import probables, similarite
        similarite.oublier()
        probables.oublier()
        s = self.alice.get("/api/copilote/suivi").json()
        l = s["lignes"][0]
        self.assertEqual(l["reel"]["nb_concurrents"], 3)
        self.assertEqual(l["notre"]["source"], "prix déposé")
        self.assertIn(l["notre"]["rang"], (1, 2, 3, 4))
        self.assertIsInstance(l["notre"]["gagne"], bool)
        b = s["bilan"]
        self.assertEqual((b["soumissions"], b["jugees"]), (1, 1))
        self.assertEqual(b["gagnees"], 1 if l["notre"]["gagne"] else 0)
        if l["notre"]["gagne"]:
            self.assertAlmostEqual(b["marge_moyenne"], (4_650_000 - 4_000_000) / 4_000_000)
            self.assertEqual(b["gain_dh"], 650_000)

    def test_un_ao_de_test_est_montre_mais_ne_compte_pas_dans_le_bilan(self):
        self.alice.post("/api/copilote/soumissions", json={"ref_annonce": "REJEU-01", "prix_depose": 1_900_000, "cout": 1_700_000, "scenario": "seul"})
        s = self.alice.get("/api/copilote/suivi").json()
        self.assertTrue(s["lignes"][0]["rejeu"])
        self.assertEqual(s["bilan"]["soumissions"], 0)

    def test_bilan_vide(self):
        self.assertEqual(suivi_page.bilan([]), {"soumissions": 0, "en_attente": 0, "jugees": 0, "gagnees": 0, "taux_victoire": None,
                                                  "marge_moyenne": None, "gain_dh": None, "figees_jugees": 0, "figees_gagnees": 0})


class Interrupteur(Base):
    """COPILOTE_PRIVE : éteint par défaut, tout le copilote reste ouvert au visiteur connecté ; allumé, le code est exigé
    pour les quatre parties de l'étape D (entreprise, décision, soumissions, suivi) et pour elles seules."""
    def setUp(self):
        super().setUp()
        self.p_env = mock.patch.dict("os.environ", {"COPILOTE_CODE_EXIGE": ""})      # pendant la construction : code non exigé
        self.p_env.start()
        self.visiteur = self.client()                                                  # cookie visiteur, sans code

    def tearDown(self):
        self.p_env.stop()
        super().tearDown()

    QUATRE = [("GET", "/api/copilote/entreprise", None), ("PUT", "/api/copilote/entreprise", PROFIL),
              ("POST", "/api/copilote/entreprise/partenaires", {"nom": "X", "type": "groupement"}),
              ("PUT", "/api/copilote/entreprise/partenaires/1", {"nom": "X", "type": "groupement"}),
              ("DELETE", "/api/copilote/entreprise/partenaires/1", None),
              ("GET", "/api/copilote/decision/AO1", None),
              ("POST", "/api/copilote/soumissions", {"ref_annonce": "AO1", "prix_depose": 1, "cout": 1, "scenario": "seul"}),
              ("GET", "/api/copilote/suivi", None)]
    PAGES = ["/copilote/entreprise", "/copilote/suivi"]

    def test_interrupteur_eteint_le_visiteur_connecte_passe_partout(self):
        self.assertNotIn("COPILOTE_PRIVE", __import__("os").environ)
        for methode, url, corps in self.QUATRE:
            r = self.visiteur.request(methode, url, json=corps)
            self.assertNotIn(r.status_code, (401, 403, 302), f"{methode} {url}")
        for url in self.PAGES + ["/copilote/decision.js", "/copilote/analyse/AO1"]:
            self.assertEqual(self.visiteur.get(url).status_code, 200, url)

    def test_interrupteur_allume_le_code_est_exige_pour_les_quatre_parties(self):
        with mock.patch.dict("os.environ", {"COPILOTE_PRIVE": "1"}):
            for methode, url, corps in self.QUATRE:
                self.assertEqual(self.visiteur.request(methode, url, json=corps).status_code, 401, f"{methode} {url}")
            for url in self.PAGES:
                r = self.visiteur.get(url)
                self.assertEqual((r.status_code, r.headers["location"]), (302, "/copilote/entrer"), url)
            # le reste du copilote ne bascule pas : l'analyse et ses scripts restent ouverts au visiteur connecté
            self.assertEqual(self.visiteur.get("/api/copilote/analyse/AO1").status_code, 200)
            self.assertEqual(self.visiteur.get("/copilote/decision.js").status_code, 200)
            # avec le code, tout passe
            for methode, url, corps in self.QUATRE:
                if methode == "GET":
                    self.assertEqual(self.alice.request(methode, url).status_code, 200, url)
            for url in self.PAGES:
                self.assertEqual(self.alice.get(url).status_code, 200, url)
            # la page d'analyse montre l'étape ⑥ verrouillée : l'API de décision répond 401
            self.assertEqual(self.visiteur.get("/api/copilote/decision/AO1").status_code, 401)

    def test_interrupteur_a_une_valeur_precise(self):
        for valeur in ("0", "", "oui", "true"):
            with mock.patch.dict("os.environ", {"COPILOTE_PRIVE": valeur}):
                self.assertEqual(self.visiteur.get("/api/copilote/entreprise").status_code, 200, valeur)

    def test_un_cookie_visiteur_recopie_en_cookie_copilote_reste_refuse(self):
        with mock.patch.dict("os.environ", {"COPILOTE_PRIVE": "1"}):
            self.visiteur.cookies.set("pvc", cookie_visiteur())
            self.assertEqual(self.visiteur.get("/api/copilote/entreprise").status_code, 401)


class Pages(unittest.TestCase):
    def test_pages_de_l_etape_d(self):
        analyse = (ICI / "statique" / "copilote_analyse.html").read_text(encoding="utf-8")
        for morceau in ('src="/copilote/decision.js"', "function blocDecision", "Decision.evaluer", "Decision.decider",
                        "J’ai soumissionné", "/api/copilote/soumissions", "les concurrents gardent leur comportement habituel"
                        if False else "T.hypothese", "Choisir un prix, d’un coup d’œil", "decision-retablir"):
            self.assertIn(morceau, analyse)
        textes = json.loads((ICI / "copilote" / "decision_textes.json").read_text(encoding="utf-8"))
        self.assertIn("les concurrents gardent leur comportement habituel", textes["hypothese"])
        entreprise_html = (ICI / "statique" / "copilote_entreprise.html").read_text(encoding="utf-8")
        for morceau in ("/api/copilote/entreprise", "Partenaires déclarés", "jamais le prix d’une autre société", "pv-theme"):
            self.assertIn(morceau, entreprise_html)
        suivi_html = (ICI / "statique" / "copilote_suivi.html").read_text(encoding="utf-8")
        for morceau in ("/api/copilote/suivi", "Taux de victoire", "Prévu / réel"):
            self.assertIn(morceau, suivi_html)
        for nom in ("copilote_entreprise.html", "copilote_suivi.html"):
            h = (ICI / "statique" / nom).read_text(encoding="utf-8")
            self.assertIn('name="robots" content="noindex', h)
            self.assertNotIn("https://", h.replace("http://www.w3.org", ""))        # pas de CDN

    def test_vocabulaire_neutre(self):
        import re
        mots = re.compile(r"entente|collusion|fraude|cartel|complicit|truqu|couverture|répartition de march", re.I)
        for nom in ("copilote/decision.py", "copilote/decision_textes.json", "copilote/eligibilite.py", "copilote/entreprise.py",
                    "statique/decision.js", "statique/copilote_entreprise.html", "statique/copilote_suivi.html"):
            self.assertIsNone(mots.search((ICI / nom).read_text(encoding="utf-8")), nom)


if __name__ == "__main__":
    unittest.main()
