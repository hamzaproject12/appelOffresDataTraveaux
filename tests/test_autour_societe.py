"""Le calcul « autour d'une société » : le même chiffre en Python et en JavaScript.

    - un cas fait à la main, avec ses chiffres écrits ici ;
    - 20 jeux tirés au hasard (graine fixe) : Python et JavaScript donnent les mêmes lignes ;
    - sur la vraie base (ignoré si elle est absente) : 20 sociétés, les contrôles de « awrachtadla » et le
      temps de calcul de la plus grosse société ;
    - la route des données : contenu, gzip, cache.

JavaScript est lancé par Node (tests/lancer_calcul.js). Sans Node, les comparaisons sont ignorées, pas vertes.
"""
import gzip
import json
import random
import shutil
import sqlite3
import subprocess
import unittest
from contextlib import closing
from pathlib import Path

from copilote import autour_societe as A
from copilote import bases, config, fusions
from tests.test_page_groupes import Base

ICI = Path(__file__).resolve().parent
NODE = shutil.which("node")
CHAMPS_ENTIERS = ("communes", "victoires_x_avec", "victoires_y")
CHAMPS_REELS = ("A", "B", "C", "rapport", "position")


def en_js(jeux: list[dict], repetitions: int = 1) -> dict:
    r = subprocess.run([NODE, str(ICI / "lancer_calcul.js")], input=json.dumps(
        {"jeux": jeux, "repetitions": repetitions}).encode(), capture_output=True, timeout=120)
    if r.returncode:
        raise AssertionError(r.stderr.decode("utf-8", "replace"))
    return json.loads(r.stdout)


def meme_resultat(test: unittest.TestCase, py: dict, js: dict, etiquette: str = "") -> None:
    test.assertEqual((py["unites_x"], py["victoires_x"]), (js["unites_x"], js["victoires_x"]), etiquette)
    test.assertEqual(py["groupe"], js["groupe"], etiquette)
    test.assertEqual([l["cle"] for l in py["lignes"]], [l["cle"] for l in js["lignes"]], etiquette)
    for p, j in zip(py["lignes"], js["lignes"]):
        for k in CHAMPS_ENTIERS + ("lecture",):
            test.assertEqual(p[k], j[k], f"{etiquette} {p['cle']} {k}")
        for k in CHAMPS_REELS:
            if p[k] is None:
                test.assertIsNone(j[k], f"{etiquette} {p['cle']} {k}")
            else:
                test.assertAlmostEqual(p[k], j[k], places=12, msg=f"{etiquette} {p['cle']} {k}")


def jeu(unites: list[tuple], totaux: dict[str, int], x: str = "x") -> dict:
    """unites : (gagnant, {cle: taux}) ; chaque offre est « admise »."""
    return {"societe": {"cle": x, "nom": x.upper()},
            "unites": [{"id": f"m{i}", "ref": f"m{i}", "lot": None, "date": f"2026-01-{i + 1:02d}", "acheteur": "",
                        "reference": "", "objet": "", "estimation": 100, "gagnant": g,
                        "offres": [[c, "m", t] for c, t in offres.items()]} for i, (g, offres) in enumerate(unites)],
            "societes": {c: {"nom": c.upper(), "total": n} for c, n in totaux.items()},
            "liens": {}, "avis": {}}


class AMain(unittest.TestCase):
    """Cinq unités pour x ; y est présente sur quatre, z sur deux. Tout est compté ici à la main."""
    D = jeu([("x", {"x": .90, "y": .95, "z": 1.0}), ("x", {"x": .85, "y": .90}), ("x", {"x": .90, "y": 1.0}),
             ("y", {"x": 1.0, "y": .80}), ("z", {"x": 1.1, "z": .90})], {"x": 20, "y": 8, "z": 3})

    def test_chiffres_de_y(self):
        r = A.calculer(self.D)
        y = next(l for l in r["lignes"] if l["cle"] == "y")
        self.assertEqual((r["unites_x"], r["victoires_x"]), (5, 3))
        self.assertEqual((y["communes"], y["victoires_x_avec"], y["victoires_y"]), (4, 3, 1))
        self.assertAlmostEqual(y["A"], 1.0)
        self.assertAlmostEqual(y["B"], 0.8)
        self.assertAlmostEqual(y["C"], 0.5)
        self.assertAlmostEqual(y["rapport"], 1.25)
        self.assertAlmostEqual(y["position"], .90 / .85 - 1)         # médiane de 3 écarts : celui du milieu
        z = next(l for l in r["lignes"] if l["cle"] == "z")
        self.assertEqual((z["communes"], z["victoires_x_avec"], z["victoires_y"]), (2, 1, 1))
        self.assertAlmostEqual(z["C"], 2 / 3)
        self.assertAlmostEqual(z["rapport"], (1 / 3) / 0.4)

    def test_classement_et_reglages(self):
        lecture = lambda reglages: {l["cle"]: l["lecture"] for l in A.calculer(self.D, reglages)["lignes"]}
        self.assertEqual(lecture(None), {"y": "neutre", "z": "neutre"})            # A ÷ B = 1,25 < 1,5
        self.assertEqual(lecture({"rapport_min": 1.2})["y"], "presente")
        self.assertEqual(A.calculer(self.D, {"rapport_min": 1.2})["groupe"], ["x", "y"])
        self.assertEqual(lecture({"rapport_min": 1.2, "max_victoires_y": 0})["y"], "neutre")   # y gagne 1 fois contre x
        self.assertEqual(lecture({"rapport_min": 1.2, "min_victoires": 4})["y"], "neutre")     # 3 victoires seulement

    def test_concurrent_direct(self):
        d = jeu([("y", {"x": 1.0, "y": .8}), ("y", {"x": 1.0, "y": .8}), ("y", {"x": 1.0, "y": .8}),
                 ("x", {"x": .9, "y": 1.0})], {"x": 4, "y": 6})
        self.assertEqual(A.calculer(d)["lignes"][0]["lecture"], "concurrent")      # 3 victoires contre x, 1 pour x
        self.assertEqual(A.calculer(d, {"min_victoires_y": 4})["lignes"][0]["lecture"], "neutre")

    def test_sans_victoire_rien_a_diviser(self):
        d = jeu([("y", {"x": 1.0, "y": .8})], {"x": 1, "y": 1})
        l = A.calculer(d)["lignes"][0]
        self.assertEqual((l["A"], l["rapport"], l["lecture"]), (None, None, "neutre"))
        self.assertAlmostEqual(l["B"], 1.0)

    def test_une_cle_ecrite_deux_fois_ne_compte_qu_une(self):
        d = jeu([("x", {"x": .9, "y": .95})], {"x": 1, "y": 1})
        d["unites"][0]["offres"].append(["y", "d", 1.0])
        self.assertEqual(A.calculer(d)["lignes"][0]["communes"], 1)

    @unittest.skipUnless(NODE, "Node n'est pas installé")
    def test_javascript_donne_la_meme_chose(self):
        for reglages in (None, {"rapport_min": 1.2}, {"min_victoires": 1, "max_victoires_y": 0}):
            meme_resultat(self, A.calculer(self.D, reglages),
                          en_js([{"donnees": self.D, "reglages": reglages}])["resultats"][0], str(reglages))


def jeu_au_hasard(graine: int) -> dict:
    """Une société et ses 30 à 120 unités ; 5 à 40 autres sociétés, certaines sans taux (offre sans estimation)."""
    hasard = random.Random(graine)
    autres = [f"s{k:02d}" for k in range(hasard.randint(5, 40))]
    unites = []
    for _ in range(hasard.randint(30, 120)):
        presentes = ["x"] + hasard.sample(autres, hasard.randint(0, min(8, len(autres))))
        offres = {c: (None if hasard.random() < .05 else round(hasard.uniform(.6, 1.2), 6)) for c in presentes}
        unites.append((hasard.choice(presentes + [None]), offres))
    totaux = {c: hasard.randint(5, 300) for c in autres} | {"x": len(unites)}
    return jeu(unites, totaux)


@unittest.skipUnless(NODE, "Node n'est pas installé")
class VingtJeux(unittest.TestCase):
    def test_python_et_javascript_donnent_les_memes_chiffres(self):
        jeux = [jeu_au_hasard(g) for g in range(20)]
        reglages = [None, {"min_victoires": 2, "rapport_min": 1.1, "max_victoires_y": 4, "min_victoires_y": 2}]
        for r in reglages:
            sortie = en_js([{"donnees": d, "reglages": r} for d in jeux])["resultats"]
            for g, (d, js) in enumerate(zip(jeux, sortie)):
                meme_resultat(self, A.calculer(d, r), js, f"jeu {g} {r}")
        # Les jeux ne sont pas triviaux : des victoires, des lectures de chaque sorte.
        lectures = {l["lecture"] for d in jeux for l in A.calculer(d)["lignes"]}
        self.assertGreaterEqual(len(lectures), 2)


def base_reelle() -> bool:
    return bases.chemin_historique().exists() and config.COPILOTE_CALCUL.exists() \
        and config.COPILOTE_RETOURS.exists()


@unittest.skipUnless(base_reelle(), "pv_travaux.db et copilote_calcul.db absents")
class SurLaBaseReelle(unittest.TestCase):
    """Lecture seule des trois bases du dossier. La première société paie le parcours complet (3 s)."""

    @classmethod
    def setUpClass(cls):
        # Les bases du copilote aussi en lecture seule : ce test ne doit rien créer ni écrire dans le dossier.
        def lecture(chemin):
            db = sqlite3.connect(f"file:{Path(chemin).as_posix()}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            return db
        cls.h, cls.c, cls.r = bases.historique(), lecture(config.COPILOTE_CALCUL), lecture(config.COPILOTE_RETOURS)
        cls.fusions = fusions.charger()
        cls.cache = {}

    @classmethod
    def tearDownClass(cls):
        for db in (cls.h, cls.c, cls.r):
            db.close()

    def donnees(self, cle):
        if cle not in self.cache:
            self.cache[cle] = A.donnees(self.h, self.c, self.r, cle, self.fusions)
        return self.cache[cle]

    def test_awrachtadla_les_controles(self):
        d = self.donnees("awrachtadla")
        r = A.calculer(d)
        self.assertEqual((r["unites_x"], r["victoires_x"]), (107, 13))
        ligne = {l["cle"]: l for l in r["lignes"]}
        t = ligne["tathnouthassan"]
        self.assertEqual((t["victoires_x_avec"], r["victoires_x"]), (8, 13))                 # A = 8 / 13 = 62 %
        self.assertEqual(round(t["A"] * 100), 62)
        self.assertEqual((t["communes"], r["unites_x"]), (44, 107))                          # B = 44 / 107 = 41 %
        self.assertEqual(round(t["B"] * 100), 41)
        self.assertEqual((t["communes"], d["societes"]["tathnouthassan"]["total"]), (44, 60))  # C = 44 / 60 = 73 %
        self.assertEqual(round(t["C"] * 100), 73)
        self.assertEqual(r["lignes"][0]["cle"], "tathnouthassan")                           # trié par A : elle est en tête
        self.assertEqual(ligne["tibtrav"]["lecture"], "concurrent")                         # TIB TRAV : concurrent direct
        groupe = {d["societes"][c]["nom"] for c in r["groupe"]}
        for nom in ("STE SONACER TRAV", "STE TIZIZOUA", "STE RICANIER", "MARGHICH TRAVAUX",
                    "ENTREPRISE SALMI OMAR", "EQUIPE MAROCAINE CONSTRUCTION", "STE AWRACH TADLA"):
            self.assertIn(nom, groupe)
        self.assertNotIn("TIB TRAV", groupe)

    @unittest.skipUnless(NODE, "Node n'est pas installé")
    def test_vingt_societes_python_et_javascript(self):
        # awrachtadla, puis 19 sociétés régulièrement réparties parmi celles qui ont au moins 30 marchés.
        rangees = [r[0] for r in self.c.execute(
            "SELECT cle FROM groupes_societes WHERE calcul = 'courant' AND marches >= 30 ORDER BY cle")]
        choisies = ["awrachtadla"] + [c for c in rangees[::max(1, len(rangees) // 19)] if c != "awrachtadla"][:19]
        self.assertEqual(len(choisies), 20)
        jeux = [self.donnees(c) for c in choisies]
        for r in (None, {"min_victoires": 2, "rapport_min": 1.2, "max_victoires_y": 3, "min_victoires_y": 2}):
            sortie = en_js([{"donnees": d, "reglages": r} for d in jeux])["resultats"]
            for c, d, js in zip(choisies, jeux, sortie):
                meme_resultat(self, A.calculer(d, r), js, f"{c} {r}")

    @unittest.skipUnless(NODE, "Node n'est pas installé")
    def test_la_plus_grosse_societe_se_calcule_en_moins_de_200_ms(self):
        # La plus grosse = celle dont la réponse est la plus lourde (le plus d'offres dans ses unités).
        total = A.unites_par_societe(self.h, self.fusions)
        cle = max(total, key=lambda c: total[c])
        d = self.donnees(cle)
        sortie = en_js([{"donnees": d}], repetitions=5)
        self.assertLess(sortie["ms"][0], 200, f"{cle} : {sortie['ms'][0]:.1f} ms")


class LaRoute(Base):
    """La route /api/groupes/societe/{cle}/donnees sur la base de poche (a, b, c sur 10 marchés)."""

    def test_contenu(self):
        r = self.client().get("/api/groupes/societe/b/donnees")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["societe"]["cle"], "b")
        self.assertEqual(len(d["unites"]), 10)
        u = d["unites"][0]
        self.assertTrue({"id", "ref", "lot", "date", "acheteur", "objet", "estimation", "gagnant", "offres"} <= set(u))
        self.assertTrue(all(len(o) == 3 for o in u["offres"]))
        self.assertTrue({"a", "b", "c"} <= set(d["societes"]))
        self.assertEqual(d["reglages"], A.REGLAGES)
        self.assertEqual(set(d["lectures"]), {"presente", "concurrent", "neutre"})
        for c in d["societes"].values():
            self.assertIsInstance(c["total"], int)

    def test_le_calcul_de_la_route_est_celui_de_python(self):
        d = self.client().get("/api/groupes/societe/a/donnees").json()      # « a » absorbe « a2 » (fusion)
        r = A.calculer(d)
        self.assertEqual(r["unites_x"], len(d["unites"]))
        self.assertTrue(r["lignes"])

    def test_gzip_cache_et_revalidation(self):
        c = self.client()
        r = c.get("/api/groupes/societe/b/donnees", headers={"accept-encoding": "gzip"})
        self.assertEqual(r.headers["content-encoding"], "gzip")
        etag = r.headers["etag"]
        self.assertEqual(c.get("/api/groupes/societe/b/donnees", headers={"if-none-match": etag}).status_code, 304)
        brut = c.get("/api/groupes/societe/b/donnees", headers={"accept-encoding": "identity"})
        self.assertNotIn("content-encoding", brut.headers)
        self.assertEqual(brut.json(), r.json())

    def test_un_avis_change_l_etag_et_apparait(self):
        c = self.alice
        avant = c.get("/api/groupes/societe/a/donnees")
        self.avis(c, reponse="oui", cle_a="a", cle_b="b", commentaire="partenaires habituels")
        apres = c.get("/api/groupes/societe/a/donnees")
        self.assertNotEqual(avant.headers["etag"], apres.headers["etag"])
        self.assertEqual(apres.json()["avis"]["b"]["reponse"], "oui")
        self.assertEqual(apres.json()["avis"]["b"]["auteur"], "Alice")

    def test_societe_inconnue_et_cle_mal_formee(self):
        c = self.client()
        self.assertEqual(c.get("/api/groupes/societe/zzz/donnees").status_code, 404)
        self.assertEqual(c.get("/api/groupes/societe/A%20B/donnees").status_code, 422)

    def test_la_lecture_ne_touche_pas_l_historique(self):
        from tests.test_page_groupes import empreinte
        avant = empreinte(self.d / "pv_travaux.db")
        self.client().get("/api/groupes/societe/a/donnees")
        self.assertEqual(empreinte(self.d / "pv_travaux.db"), avant)


if __name__ == "__main__":
    unittest.main()
