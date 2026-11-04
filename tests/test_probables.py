"""Étape A : sociétés probables, groupes de l'AO, nombre de concurrents, API et page d'analyse."""
import hashlib
import itertools
import json
import math
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from copilote import bases, config, groupes_ao, probables, rejeu, rejouer_ao, similarite
from copilote.donnees import Unite
from copilote.routes import creer_routeur
from tests import outils
from tests.test_acces import CODES, SECRET, cookie_visiteur
from tests.test_rejeu import base_de_rejeu
from tests.test_similarite import base_ao, base_similaire

ICI = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
CAS = json.loads((ICI / "tests" / "cas_concurrents.json").read_text(encoding="utf-8"))["cas"]


# --------------------------------------------------------------------------- outils

def unite(ref, date, offres, gagnant, acheteur="COMMUNE DE TEST", estimation=100.0):
    """offres : [(cle, montant)] ou [(cle, montant, statut)]."""
    conc = []
    for o in offres:
        cle, montant, *statut = o
        conc.append({"nom": cle.upper(), "cle": cle, "statut": statut[0] if statut else "admis", "lots": "",
                     "montant_verifie": montant, "montant_acte": montant, "taux": montant / estimation})
    return Unite(ref=ref, lot=None, acheteur=acheteur, date=date, annee=date[:4], estimation=estimation,
                 cle_gagnant=gagnant, prix_reference_stocke=None, concurrents=conc)


def historique_de_groupe() -> list[Unite]:
    """a gagne 6 fois avec b présente ; b gagne 2 fois avec a, 1 fois sans ; a perd 8 fois sans b.
    Lecture attendue de b autour de a : noyau (A ÷ B = 2, a présente sur 2/3 des victoires de b)."""
    u, k = [], 0
    def jour():
        nonlocal k
        k += 1
        return f"2025{1 + k // 28:02d}{1 + k % 28:02d}"
    for i in range(6):
        u.append(unite(f"w{i}", jour(), [("a", 95), ("b", 99), (f"z{i}", 104)], "a"))
    for i in range(2):
        u.append(unite(f"v{i}", jour(), [("b", 95), ("a", 99), (f"y{i}", 104)], "b"))
    for i in range(8):
        u.append(unite(f"p{i}", jour(), [("a", 102), (f"x{i}", 97)], f"x{i}"))
    u.append(unite("solo", jour(), [("b", 96), ("q", 101)], "b"))
    return u


def candidat(cle, p, dans_liste=True):
    return {"cle": cle, "p": p, "p_formule": p, "x": {}, "brut": {}, "contributions": {}, "dans_liste": dans_liste,
            "association": None}


# --------------------------------------------------------------------------- loi du nombre de concurrents

class Distribution(unittest.TestCase):
    def test_poisson_binomiale_egale_l_enumeration(self):
        ps = [0.3, 0.8, 0.5, 0.1]
        attendu = [0.0] * 5
        for issue in itertools.product((0, 1), repeat=4):
            pr = 1.0
            for p, x in zip(ps, issue):
                pr *= p if x else 1 - p
            attendu[sum(issue)] += pr
        for a, b in zip(probables.distribution(ps), attendu):
            self.assertAlmostEqual(a, b, places=12)

    def test_lambda_seul_donne_une_poisson(self):
        d = probables.distribution([], 2.0)
        self.assertAlmostEqual(d[0], 0.1353352832, places=9)
        self.assertAlmostEqual(d[3], 0.1804470443, places=9)

    def test_la_dispersion_garde_la_somme_et_elargit(self):
        ps = [0.4] * 20
        d0, d1 = probables.distribution(ps, 1.0), probables.distribution(ps, 1.0, 0.8)
        self.assertAlmostEqual(sum(d1), 1.0, places=6)
        f0, f1 = probables.fourchette(d0), probables.fourchette(d1)
        self.assertGreater(f1["haut"] - f1["bas"], f0["haut"] - f0["bas"])
        self.assertLess(abs(f1["moyenne"] - f0["moyenne"]), 1.5)          # centrée : la moyenne bouge peu

    def test_fourchette(self):
        f = probables.fourchette([0.05, 0.1, 0.2, 0.3, 0.2, 0.1, 0.05])
        self.assertEqual((f["bas"], f["haut"], f["probable"]), (1, 5, 3))
        self.assertGreaterEqual(f["couverte"], 0.8)
        self.assertAlmostEqual(f["moyenne"], 3.0)

    def test_cas_communs_python(self):
        for c in CAS:
            rc = probables.recaler(c["ps"], c["lambda"], c["dispersion"], c.get("decalage", 0.0), c.get("recalage"))
            if c.get("recalage"):
                for k, v in c["attendu"]["recale"].items():
                    self.assertAlmostEqual(rc[k], v, places=12, msg=f"{c['nom']} : {k}")
            d = probables.distribution(c["ps"], c["lambda"], rc["dispersion"], rc["decalage"])
            self.assertEqual(len(d), len(c["attendu"]["distribution"]), c["nom"])
            for a, b in zip(d, c["attendu"]["distribution"]):
                self.assertAlmostEqual(a, b, places=10, msg=c["nom"])
            f = probables.fourchette(d, c["niveau"])
            for k in ("bas", "haut", "probable"):
                self.assertEqual(f[k], c["attendu"]["fourchette"][k], c["nom"])

    @unittest.skipUnless(NODE, "node absent")
    def test_cas_communs_javascript_identiques(self):
        r = subprocess.run([NODE, str(ICI / "tests" / "lancer_concurrents.js")], input=json.dumps({"cas": CAS}),
                           capture_output=True, text=True, timeout=60, check=True)
        for c, js in zip(CAS, json.loads(r.stdout)["resultats"]):
            if c.get("recalage"):
                for k, v in c["attendu"]["recale"].items():
                    self.assertAlmostEqual(js["recale"][k], v, places=12, msg=f"{c['nom']} : {k}")
            self.assertEqual(len(js["distribution"]), len(c["attendu"]["distribution"]), c["nom"])
            for a, b in zip(js["distribution"], c["attendu"]["distribution"]):
                self.assertAlmostEqual(a, b, places=10, msg=c["nom"])
            for k in ("bas", "haut", "probable"):
                self.assertEqual(js["fourchette"][k], c["attendu"]["fourchette"][k], c["nom"])
            self.assertAlmostEqual(js["fourchette"]["moyenne"], c["attendu"]["fourchette"]["moyenne"], places=9)

    def test_recalage_sans_table_ne_change_rien(self):
        rc = probables.recaler([0.4, 0.2], 1.0, 0.8, 0.3, None)
        self.assertEqual((rc["decalage"], rc["dispersion"], rc["ajout"], rc["facteur"]), (0.3, 0.8, 0.0, 1.0))
        self.assertAlmostEqual(rc["prevu"], probables.esperance([0.4, 0.2], 1.0, 0.8, 0.3))

    def test_recalage_interpole_et_borne(self):
        t = {"log_prevu": [0.0, 1.0], "ajout": [1.0, 0.0], "facteur": [2.0, 1.0]}
        rc = probables.recaler([], math.exp(0.5), 0.0, 0.0, t)           # λ seul : prévu = e^0,5
        self.assertAlmostEqual(rc["ajout"], 0.5)
        self.assertAlmostEqual(rc["facteur"], 1.5)
        self.assertEqual(probables.recaler([], 100.0, 0.5, 0.0, t)["ajout"], 0.0)
        self.assertEqual(probables.recaler([], 0.2, 0.5, 0.0, t)["facteur"], 2.0)

    def test_analyse_applique_le_recalage_du_fichier(self):
        coefs = probables.charger_coefficients()
        self.assertIn("recalage_nombre", coefs)
        r = coefs["recalage_nombre"]
        self.assertEqual(len(r["log_prevu"]), len(r["ajout"]))
        self.assertEqual(r["log_prevu"], sorted(r["log_prevu"]))
        self.assertEqual(r["appris"]["appris_jusqu_au"], "2025-12-31")

    def test_calibration_strictement_croissante_et_bornee(self):
        cal = probables.charger_coefficients().get("calibration") or {"x": [0.1, 0.5, 0.9], "y": [0.08, 0.2, 0.4]}
        self.assertEqual(cal["x"], sorted(set(cal["x"])))
        grille = [k / 1000 for k in range(1, 1000)]
        ps = [probables.calibrer(p, cal) for p in grille]
        self.assertTrue(all(b > a for a, b in zip(ps, ps[1:])))          # le classement ne change pas
        self.assertTrue(all(0 < p < 1 for p in ps))
        self.assertEqual(probables.calibrer(0.42, None), 0.42)

    def test_les_constantes_de_quadrature_sont_les_memes(self):
        js = (ICI / "statique" / "analyse_calcul.js").read_text(encoding="utf-8")
        for v in probables.HERMITE_X + probables.HERMITE_W:
            self.assertIn(repr(v) if v else "0.0", js)


# --------------------------------------------------------------------------- mémoire et coupure

class Coupure(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        d = Path(self.dossier.name)
        base_de_rejeu(d / "h.db")
        self.hist = bases._lecture_seule(d / "h.db")
        self.calc = sqlite3.connect(d / "calcul.db")
        self.calc.row_factory = sqlite3.Row
        rejouer_ao.preparer(self.calc)
        choisis = rejouer_ao.choisir(rejouer_ao.candidats(self.hist), 8, 2, 0)
        self.refs = rejouer_ao.enregistrer(self.calc, choisis)
        self.mem = probables.construire_memoire(self.hist, {})

    def tearDown(self):
        self.hist.close()
        self.calc.close()
        self.dossier.cleanup()

    def test_la_coupe_de_la_memoire_est_celle_de_rejeu(self):
        for ref in self.refs:
            n = self.mem.coupe(rejeu.limite_pour(ref, self.calc))
            vues = {u.identifiant for u in rejeu.unites_pour(self.hist, ref, self.calc, fusions={})}
            self.assertEqual({u.identifiant for u in self.mem.unites[:n]}, vues, ref)
        self.assertEqual(self.mem.coupe(rejeu.limite_pour("AO-1", self.calc)), len(self.mem.unites))

    def test_un_rejeu_sans_coupure_est_une_erreur(self):
        with self.assertRaises(rejeu.RejeuSansCoupure):
            rejeu.limite_pour("REJEU-99", self.calc)

    def test_rien_d_apres_la_coupure_ne_change_le_resultat(self):
        """Ajouter des marchés après la coupure ne change ni les candidats, ni leurs variables, ni les groupes."""
        u = historique_de_groupe()
        futurs = [unite(f"f{i}", "20260301", [("a", 90), ("b", 91), ("nouvelle", 92)], "nouvelle") for i in range(20)]
        m1, m2 = probables.Memoire(u, {}), probables.Memoire(u + futurs, {})
        limite = "20251231"
        sims = [probables.Similaire(x, 80.0, 0.9) for x in u[:5]]
        for m in (m1, m2):
            m.ctx = probables.Contexte(n=m.coupe(limite), jour=similarite.jour_de("2026-01-01"),
                                       acheteur_cles=frozenset({bases.cle_acheteur("COMMUNE DE TEST")}),
                                       codes=frozenset(), similaires=sims)
        a1, a2 = probables.analyser(m1, m1.ctx), probables.analyser(m2, m2.ctx)
        self.assertEqual([(c["cle"], round(c["p"], 12)) for c in a1["candidats"]],
                         [(c["cle"], round(c["p"], 12)) for c in a2["candidats"]])
        self.assertNotIn("nouvelle", {c["cle"] for c in a2["candidats"]})
        self.assertEqual([g["membres"] for g in a1["groupes"]], [g["membres"] for g in a2["groupes"]])
        self.assertEqual(a1["concurrents"]["distribution"], a2["concurrents"]["distribution"])


# --------------------------------------------------------------------------- groupes de l'AO

class GroupesAo(unittest.TestCase):
    def setUp(self):
        self.mem = probables.Memoire(historique_de_groupe(), {})
        self.n = len(self.mem.unites)
        self.r = dict(probables.REGLAGES)

    def test_lecture_autour_de_a_sur_la_coupure(self):
        res = groupes_ao.autour(self.mem, self.n, "a")
        lignes = {l["cle"]: l for l in res["lignes"]}
        self.assertEqual(lignes["b"]["lecture"], "noyau")
        self.assertAlmostEqual(lignes["b"]["B"], 8 / 16)
        self.assertEqual(res["groupe"], ["a", "b"])

    def test_deux_probables_liees_forment_un_groupe_avec_un_chef_de_file(self):
        tous = [candidat("a", 0.6), candidat("b", 0.4), candidat("x0", 0.3)]
        g = groupes_ao.former(self.mem, self.n, tous, self.r)
        self.assertEqual(len(g["groupes"]), 1)
        gr = g["groupes"][0]
        self.assertEqual(sorted(gr["membres"]), ["a", "b"])
        self.assertEqual(gr["chef"], "a")                         # 6 victoires accompagnées contre 2
        self.assertEqual(gr["victoires_accompagnees"], {"a": 6, "b": 2})
        self.assertEqual(g["individuelles"], ["x0"])
        self.assertEqual(g["ajoutees"], [])

    def test_alliee_absente_ajoutee_par_association(self):
        tous = [candidat("a", 0.5)]
        g = groupes_ao.former(self.mem, self.n, tous, self.r)
        self.assertEqual([c["cle"] for c in g["ajoutees"]], ["b"])
        b = g["ajoutees"][0]
        self.assertAlmostEqual(b["p"], 0.5 * 8 / 16)              # P(b présente | a soumissionne) × P(a vient)
        self.assertEqual(b["association"]["via"], "a")
        self.assertEqual(sorted(g["groupes"][0]["membres"]), ["a", "b"])

    def test_alliee_candidate_hors_liste_garde_sa_probabilite_si_elle_est_plus_forte(self):
        tous = [candidat("a", 0.5), candidat("b", 0.05, dans_liste=False)]
        groupes_ao.former(self.mem, self.n, tous, self.r)
        self.assertAlmostEqual(tous[1]["p"], 0.25)
        self.assertIsNotNone(tous[1]["association"])

    def test_sous_le_seuil_des_allies_on_ne_cherche_pas(self):
        tous = [candidat("a", 0.12), candidat("b", 0.11)]
        g = groupes_ao.former(self.mem, self.n, tous, self.r)
        self.assertEqual(g["groupes"], [])
        self.assertEqual(g["individuelles"], ["a", "b"])

    def test_taille_maximale_et_un_seul_groupe_par_societe(self):
        tous = [candidat("a", 0.6), candidat("b", 0.4)]
        g = groupes_ao.former(self.mem, self.n, tous, {**self.r, "taille_groupe": 1})
        self.assertEqual(g["groupes"], [])
        membres = [m for gr in groupes_ao.former(self.mem, self.n, tous, self.r)["groupes"] for m in gr["membres"]]
        self.assertEqual(len(membres), len(set(membres)))

    def test_la_coupure_efface_le_lien(self):
        g = groupes_ao.former(self.mem, 2, [candidat("a", 0.6), candidat("b", 0.4)], self.r)
        self.assertEqual(g["groupes"], [])                         # 2 unités : moins de 3 victoires accompagnées
        g = groupes_ao.former(self.mem, 3, [candidat("a", 0.6), candidat("b", 0.4)], self.r)
        self.assertEqual(len(g["groupes"]), 1)

    def test_strategie_du_groupe(self):
        s = groupes_ao.strategie(self.mem, self.n, ["a", "b"])
        self.assertEqual((s["unites"], s["jugees"], s["gagnees"], s["defaites"]), (8, 8, 8, 0))
        self.assertEqual(s["gains"], {"a": 6, "b": 2})
        self.assertAlmostEqual(s["taux_victoire"], 1.0)
        b = next(m for m in s["membres"] if m["cle"] == "b")
        self.assertAlmostEqual(b["position_gagnant"], 0.99 / 0.95 - 1)      # quand a gagne, b est 4,2 % au-dessus
        s2 = groupes_ao.strategie(self.mem, self.n, ["a", "q"])
        self.assertEqual(s2["unites"], 0)


# --------------------------------------------------------------------------- séparation du moteur et de la vérité

class Separation(unittest.TestCase):
    def test_le_moteur_n_importe_pas_la_verite(self):
        permis = {"verite_rejeu.py", "rejouer_ao.py", "routes.py", "mesures_probables.py",
                  "mesures_simulation.py"}             # étape C : les mesures lisent la vérité, le moteur jamais
        for chemin in sorted((ICI / "copilote").glob("*.py")):
            if chemin.name in permis:
                continue
            texte = chemin.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"^\s*(from|import).*verite_rejeu", texte, re.M), chemin.name)
            self.assertIsNone(re.search(r"verite_rejeu\.\w+\(|pour_lot\(", texte), chemin.name)

    def test_pas_de_vocabulaire_accusatoire(self):
        mots = re.compile(r"entente|collusion|fraude|cartel|complicit|truqu", re.I)
        for nom in ("copilote/probables.py", "copilote/groupes_ao.py", "copilote/analyse.py",
                    "statique/copilote_analyse.html"):
            self.assertIsNone(mots.search((ICI / nom).read_text(encoding="utf-8")), nom)


# --------------------------------------------------------------------------- API et page

class Api(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.d = Path(self.dossier.name)
        base_similaire(self.d / "pv_travaux.db")
        base_ao(self.d / "ao_ouverts.db",
                lignes=[("AO1", "Travaux de construction de pistes rurales reliant les douars de la commune LIEU1",
                         "COMMUNE DE LIEU1", 5e6, "Taza"),
                        ("AO3", "Travaux d'amenagement en deux lots", "COMMUNE DE LIEU2", None, None),
                        ("REJEU-01", "Travaux d'alimentation en eau potable du douar LIEU2", "COMMUNE DE LIEU2", 2e6, None),
                        ("REJEU-02", "Travaux d'alimentation en eau potable du douar LIEU2", "COMMUNE DE LIEU2", 2e6, None)],
                lots=[("AO3", "1", "Terrassements", 1e6), ("AO3", "2", "Chaussee", 3e6)],
                quals=[("AO1", "B.1", "3")])
        self.patchs = [mock.patch.dict("os.environ", {"COPILOTE_ACCES": CODES, "COPILOTE_CODE_EXIGE": "1"}),
                       mock.patch.object(config, "COPILOTE_CALCUL", self.d / "calcul.db"),
                       mock.patch.object(config, "COPILOTE_RETOURS", self.d / "retours.db")]
        for p in self.patchs:
            p.start()
        similarite.oublier()
        probables.oublier()
        self.app = FastAPI()
        self.app.include_router(creer_routeur(SECRET, self.d / "pv_travaux.db", self.d / "ao_ouverts.db"))
        self.alice = self.client("code-alice")
        calc = bases.calcul()
        rejouer_ao.preparer(calc)
        calc.execute("INSERT INTO rejeu_ao (ref_annonce, date_coupure, annonce, cree_le) VALUES ('REJEU-01', '2025-06-30', '{}', 'x')")
        calc.execute("INSERT INTO rejeu_verite (ref_annonce, lot, ref_reel, reference_reelle, date_ouverture, cle_gagnant,"
                     " nom_gagnant, taux_gagnant, concurrents, cree_le) VALUES ('REJEU-01', '', 'm999', '9/2025', '20250701',"
                     " 'soc1', 'SOC1', 0.95, ?, 'x')",
                     (json.dumps([{"nom": "SOC1", "cle": "soc1", "statut": "attributaire", "montant": 1, "taux": 0.95, "rang": 1},
                                  {"nom": "SOC2", "cle": "soc2", "statut": "admis", "montant": 1, "taux": 0.99, "rang": 2}]),))
        calc.commit()
        calc.close()

    def tearDown(self):
        similarite.oublier()
        probables.oublier()
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

    def test_acces_reserve_au_copilote(self):
        for url in ("/api/copilote/analyse/AO1", "/api/copilote/rejeu/REJEU-01/verite"):
            self.assertEqual(self.client().get(url).status_code, 401, url)
            self.assertEqual(TestClient(self.app).get(url).status_code, 401, url)
        for url in ("/copilote/analyse/AO1", "/copilote/analyse_calcul.js"):
            r = self.client().get(url)
            self.assertEqual((r.status_code, r.headers["location"]), (302, "/copilote/entrer"), url)

    def test_dossier_complet(self):
        r = self.alice.get("/api/copilote/analyse/AO1")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        for cle in ("ao", "similaires", "probables", "groupes", "individuelles", "concurrents", "historique"):
            self.assertIn(cle, d)
        self.assertLessEqual(len(d["similaires"]), 20)
        cands = d["probables"]["candidats"]
        self.assertTrue(cands)
        self.assertTrue(all(0 <= c["p"] <= 1 for c in cands))
        self.assertEqual(cands, sorted(cands, key=lambda c: (-c["p"], c["cle"])))
        conc = d["concurrents"]
        self.assertLessEqual(conc["bas"], conc["probable"])
        self.assertLessEqual(conc["probable"], conc["haut"])
        self.assertAlmostEqual(sum(conc["distribution"]), 1.0, places=4)
        membres = [m for g in d["groupes"] for m in g["membres"]]
        self.assertEqual(len(membres), len(set(membres)))
        self.assertTrue(all(len(g["membres"]) <= 9 and g["chef"] in g["membres"] for g in d["groupes"]))
        self.assertFalse(set(d["individuelles"]) & set(membres))
        cles = {c["cle"] for c in cands}
        self.assertTrue(set(membres) <= cles)

    def test_rejeu_ne_lit_que_le_passe(self):
        d = self.alice.get("/api/copilote/analyse/REJEU-01").json()
        self.assertEqual(d["ao"]["mode"], "rejeu")
        self.assertLessEqual(d["historique"]["derniere_date"], "20250630")
        self.assertTrue(all(s["date"] <= "2025-06-30" for s in d["similaires"]))
        self.assertTrue(all((c["brut"] or {}).get("derniere") is None or c["brut"]["derniere"] <= "20250630"
                            for c in d["probables"]["candidats"]))
        self.assertEqual(self.alice.get("/api/copilote/analyse/REJEU-02").status_code, 409)

    def test_alloti_et_erreurs(self):
        r = self.alice.get("/api/copilote/analyse/AO3")
        self.assertEqual(r.status_code, 400)
        self.assertEqual([x["lot"] for x in r.json()["lots"]], ["1", "2"])
        self.assertEqual(self.alice.get("/api/copilote/analyse/AO3", params={"lot": "2"}).status_code, 200)
        self.assertEqual(self.alice.get("/api/copilote/analyse/INCONNU").status_code, 404)

    def test_verite_a_part(self):
        v = self.alice.get("/api/copilote/rejeu/REJEU-01/verite").json()
        self.assertEqual((v["gagnant"]["cle"], v["nb_concurrents"]), ("soc1", 2))
        self.assertEqual(self.alice.get("/api/copilote/rejeu/AO1/verite").status_code, 404)
        self.assertEqual(self.alice.get("/api/copilote/rejeu/REJEU-01/verite", params={"lot": "1"}).status_code, 404)
        texte = self.alice.get("/api/copilote/analyse/REJEU-01").text
        self.assertNotIn("m999", texte)                            # le dossier ne connaît pas le marché réel

    def test_page_et_script(self):
        r = self.alice.get("/copilote/analyse/AO1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("noindex", r.headers.get("x-robots-tag", ""))
        self.assertIn('<meta name="robots" content="noindex, nofollow">', r.text)
        self.assertIn("pv-theme", r.text)
        self.assertEqual(self.alice.get("/copilote/analyse_calcul.js").status_code, 200)

    def test_la_base_d_historique_n_est_jamais_modifiee(self):
        avant = hashlib.sha256((self.d / "pv_travaux.db").read_bytes()).hexdigest()
        for ref, params in (("AO1", {}), ("AO3", {"lot": "1"}), ("REJEU-01", {})):
            self.alice.get(f"/api/copilote/analyse/{ref}", params=params)
        self.assertEqual(hashlib.sha256((self.d / "pv_travaux.db").read_bytes()).hexdigest(), avant)


class PageAoOuverts(unittest.TestCase):
    def test_bouton_analyser_seulement_avec_l_acces(self):
        html = (ICI / "statique" / "ao_ouverts.html").read_text(encoding="utf-8")
        self.assertIn("/copilote/analyse/${encodeURIComponent(a.ref_annonce)}", html)
        self.assertIn("ACCES_COPILOTE = r.ok", html)


if __name__ == "__main__":
    unittest.main()
