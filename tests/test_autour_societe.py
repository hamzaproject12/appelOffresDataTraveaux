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
CHAMPS_ENTIERS = ("communes", "victoires_x_avec", "victoires_y", "victoires_y_total")
CHAMPS_REELS = ("A", "B", "C", "rapport", "position", "part_x_sur_y")


def en_js(jeux: list[dict], repetitions: int = 1) -> dict:
    r = subprocess.run([NODE, str(ICI / "lancer_calcul.js")], input=json.dumps(
        {"jeux": jeux, "repetitions": repetitions}).encode(), capture_output=True, timeout=120)
    if r.returncode:
        raise AssertionError(r.stderr.decode("utf-8", "replace"))
    return json.loads(r.stdout)


def meme_resultat(test: unittest.TestCase, py: dict, js: dict, etiquette: str = "") -> None:
    test.assertEqual((py["unites_x"], py["victoires_x"]), (js["unites_x"], js["victoires_x"]), etiquette)
    for k in ("groupe", "noyau", "accompagnatrices", "autres"):
        test.assertEqual(py[k], js[k], f"{etiquette} {k}")
    test.assertEqual([l["cle"] for l in py["lignes"]], [l["cle"] for l in js["lignes"]], etiquette)
    for p, j in zip(py["lignes"], js["lignes"]):
        for k in CHAMPS_ENTIERS + ("lecture",):
            test.assertEqual(p[k], j[k], f"{etiquette} {p['cle']} {k}")
        for k in CHAMPS_REELS:
            if p[k] is None:
                test.assertIsNone(j[k], f"{etiquette} {p['cle']} {k}")
            else:
                test.assertAlmostEqual(p[k], j[k], places=12, msg=f"{etiquette} {p['cle']} {k}")


def jeu(unites: list[tuple], totaux: dict[str, int], x: str = "x", victoires: dict[str, int] | None = None) -> dict:
    """unites : (gagnant, {cle: taux}) ; chaque offre est « admise ». `victoires` : toutes les victoires
    de chaque société dans la base (par défaut : celles qu'on voit dans les unités de x)."""
    vues = {}
    for g, _ in unites:
        if g:
            vues[g] = vues.get(g, 0) + 1
    return {"societe": {"cle": x, "nom": x.upper()},
            "unites": [{"id": f"m{i}", "ref": f"m{i}", "lot": None, "date": f"2026-01-{i + 1:02d}", "acheteur": "",
                        "reference": "", "objet": "", "estimation": 100, "gagnant": g,
                        "offres": [[c, "m", t] for c, t in offres.items()]} for i, (g, offres) in enumerate(unites)],
            "societes": {c: {"nom": c.upper(), "total": n, "victoires": (victoires or vues).get(c, 0)}
                         for c, n in totaux.items()},
            "liens": {}, "avis": {}}


class AMain(unittest.TestCase):
    """Cinq unités pour x ; y est présente sur quatre, z sur deux. Tout est compté ici à la main.
    y a gagné 3 unités dans la base, dont 1 où x était présente."""
    D = jeu([("x", {"x": .90, "y": .95, "z": 1.0}), ("x", {"x": .85, "y": .90}), ("x", {"x": .90, "y": 1.0}),
             ("y", {"x": 1.0, "y": .80}), ("z", {"x": 1.1, "z": .90})], {"x": 20, "y": 8, "z": 3},
            victoires={"x": 3, "y": 3, "z": 1})

    def lecture(self, reglages=None, d=None):
        return {l["cle"]: l["lecture"] for l in A.calculer(d or self.D, reglages)["lignes"]}

    def test_chiffres_de_y(self):
        r = A.calculer(self.D)
        y = next(l for l in r["lignes"] if l["cle"] == "y")
        self.assertEqual((r["unites_x"], r["victoires_x"]), (5, 3))
        self.assertEqual((y["communes"], y["victoires_x_avec"], y["victoires_y"], y["victoires_y_total"]), (4, 3, 1, 3))
        self.assertAlmostEqual(y["A"], 1.0)
        self.assertAlmostEqual(y["B"], 0.8)
        self.assertAlmostEqual(y["C"], 0.5)
        self.assertAlmostEqual(y["rapport"], 1.25)
        self.assertAlmostEqual(y["part_x_sur_y"], 1 / 3)             # x présente sur 1 des 3 victoires de y
        self.assertAlmostEqual(y["position"], .90 / .85 - 1)         # médiane de 3 écarts : celui du milieu
        z = next(l for l in r["lignes"] if l["cle"] == "z")
        self.assertEqual((z["communes"], z["victoires_x_avec"], z["victoires_y"]), (2, 1, 1))
        self.assertAlmostEqual(z["C"], 2 / 3)
        self.assertAlmostEqual(z["rapport"], (1 / 3) / 0.4)

    def test_neutre_par_defaut(self):
        self.assertEqual(self.lecture(), {"y": "neutre", "z": "neutre"})       # A ÷ B = 1,25 < 1,3 ; C = 50 % < 60 %

    def test_accompagnatrice_par_le_rapport_ou_par_c(self):
        self.assertEqual(self.lecture({"rapport_min": 1.2})["y"], "accompagnatrice")   # x présente sur 1/3 des victoires de y
        self.assertEqual(self.lecture({"c_min": 0.5})["y"], "accompagnatrice")         # C = 50 %, l'autre voie
        self.assertEqual(self.lecture({"rapport_min": 1.2, "min_victoires": 4})["y"], "neutre")   # 3 victoires seulement

    def test_noyau_demande_que_x_soit_presente_sur_la_moitie_des_victoires_de_y(self):
        self.assertEqual(self.lecture({"rapport_min": 1.2, "part_noyau": 1 / 3})["y"], "noyau")
        self.assertEqual(self.lecture({"rapport_min": 1.2, "part_noyau": 0.34})["y"], "accompagnatrice")
        # y a gagné 1 unité où x était présente, et une seule dans toute la base : x présente sur 100 % de ses victoires
        d = jeu([(u["gagnant"], {o[0]: o[2] for o in u["offres"]}) for u in self.D["unites"]],
                {"x": 20, "y": 8, "z": 3}, victoires={"x": 3, "y": 1, "z": 1})
        self.assertEqual(self.lecture({"rapport_min": 1.2}, d)["y"], "noyau")
        # sans aucune victoire, pas de « tour de rôle » : elle accompagne seulement
        d = jeu([(u["gagnant"], {o[0]: o[2] for o in u["offres"]}) for u in self.D["unites"]],
                {"x": 20, "y": 8, "z": 3}, victoires={"x": 3, "y": 0, "z": 1})
        self.assertEqual(self.lecture({"rapport_min": 1.2}, d)["y"], "accompagnatrice")

    def test_la_condition_sur_les_victoires_contre_x_n_existe_plus(self):
        # y gagne 4 fois contre x et reste noyau : x présente sur 4 de ses 4 victoires, A ÷ B = 1 ÷ 0,7 = 1,43
        unites = [("x", {"x": .9, "y": .95})] * 3 + [("y", {"x": 1.0, "y": .8})] * 4 + [("z", {"x": 1.0, "z": .9})] * 3
        d = jeu(unites, {"x": 10, "y": 7, "z": 3}, victoires={"x": 3, "y": 4, "z": 3})
        l = A.calculer(d)["lignes"][0]
        self.assertEqual((l["victoires_x_avec"], l["victoires_y"], l["lecture"]), (3, 4, "noyau"))

    def test_concurrent_direct(self):
        # x gagne 2 unités avec y présente, y en gagne 3 : A ÷ B = 0,5 ÷ 0,8 < 1
        d = jeu([("x", {"x": .9, "y": .95}), ("x", {"x": .9, "y": .95}), ("y", {"x": 1.0, "y": .8}),
                 ("y", {"x": 1.0, "y": .8}), ("y", {"x": 1.0, "y": .8}), ("x", {"x": .9})], {"x": 6, "y": 6})
        self.assertEqual(self.lecture(None, d), {"y": "concurrent"})
        self.assertEqual(self.lecture({"min_victoires_y": 4}, d), {"y": "neutre"})
        self.assertEqual(self.lecture({"rapport_concurrent": 0.5}, d), {"y": "neutre"})       # A ÷ B = 0,625

    def test_taille_du_groupe_et_autres_societes_proches(self):
        # 6 sociétés présentes à chacune des 4 victoires de x, sans jamais gagner : six accompagnatrices (C = 100 %)
        unites = [("x", {"x": .9} | {f"s{k}": 1.0 for k in range(6)})] * 4
        totaux = {"x": 20} | {f"s{k}": 4 for k in range(6)}
        d = jeu(unites, totaux, victoires={"x": 4} | {f"s{k}": 0 for k in range(6)})
        r = A.calculer(d, {"taille_groupe": 4})
        self.assertEqual(len(r["groupe"]), 4)                          # x et trois sociétés
        self.assertEqual(r["groupe"][0], "x")
        self.assertEqual(len(r["autres"]), 3)
        self.assertEqual(r["noyau"], ["x"])                            # personne ne gagne : accompagnatrices seulement
        self.assertEqual(len(r["accompagnatrices"]), 3)
        self.assertEqual(len(A.calculer(d)["groupe"]), 7)              # taille 9 : tout le monde entre
        self.assertEqual(A.calculer(d, {"taille_groupe": 1})["groupe"], ["x"])

    def test_classement_des_proches(self):
        """Par victoires accompagnées d'abord, puis A ÷ B, puis clé."""
        unites = [("x", {"x": .9, "a": 1, "b": 1, "c": 1})] * 3 + [("x", {"x": .9, "a": 1, "b": 1})] * 2 \
            + [("x", {"x": .9, "a": 1})] + [(None, {"x": 1.0, "c": 1})] * 4
        d = jeu(unites, {"x": 20, "a": 6, "b": 5, "c": 7}, victoires={"x": 6, "a": 0, "b": 0, "c": 0})
        r = A.calculer(d, {"rapport_min": 0.5})
        self.assertEqual([l for l in r["groupe"]], ["x", "a", "b", "c"])        # 6, 5, 3 victoires accompagnées

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
        reglages = (None, {"rapport_min": 1.2}, {"rapport_min": 1.2, "part_noyau": 1 / 3}, {"c_min": 0.5},
                    {"min_victoires": 1, "taille_groupe": 2}, {"min_victoires_y": 1, "rapport_concurrent": 2})
        for r in reglages:
            meme_resultat(self, A.calculer(self.D, r),
                          en_js([{"donnees": self.D, "reglages": r}])["resultats"][0], str(r))


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
    vues = {}
    for g, _ in unites:
        if g:
            vues[g] = vues.get(g, 0) + 1
    # leurs victoires dans toute la base : au moins celles qu'on voit ici, plus d'autres ailleurs
    victoires = {c: vues.get(c, 0) + hasard.randint(0, 20) for c in autres} | {"x": vues.get("x", 0)}
    return jeu(unites, totaux, victoires=victoires)


@unittest.skipUnless(NODE, "Node n'est pas installé")
class VingtJeux(unittest.TestCase):
    def test_python_et_javascript_donnent_les_memes_chiffres(self):
        jeux = [jeu_au_hasard(g) for g in range(20)]
        reglages = [None, {"min_victoires": 2, "rapport_min": 1.1, "part_noyau": 0.3, "c_min": 0.4,
                           "min_victoires_y": 2, "rapport_concurrent": 1.5, "taille_groupe": 5}]
        for r in reglages:
            sortie = en_js([{"donnees": d, "reglages": r} for d in jeux])["resultats"]
            for g, (d, js) in enumerate(zip(jeux, sortie)):
                meme_resultat(self, A.calculer(d, r), js, f"jeu {g} {r}")
        # Les jeux ne sont pas triviaux : des victoires, des lectures de chaque sorte.
        lectures = {l["lecture"] for d in jeux for r in reglages for l in A.calculer(d, r)["lignes"]}
        self.assertEqual(lectures, {"noyau", "accompagnatrice", "concurrent", "neutre"})


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

    def noms(self, d, cles):
        return {d["societes"][c]["nom"] for c in cles}

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
        self.assertEqual(r["lignes"][0]["cle"], "tathnouthassan")                           # trié par A : en tête
        self.assertEqual(ligne["tibtrav"]["lecture"], "concurrent")                         # TIB TRAV : concurrent direct
        self.assertEqual(self.noms(d, r["noyau"]), {"STE AWRACH TADLA", "TATHNOUT HASSAN", "STE SONATRADIC"})
        self.assertLessEqual({"STE SONACER TRAV", "STE TIZIZOUA", "STE RICANIER", "MARGHICH TRAVAUX"},
                             self.noms(d, r["accompagnatrices"]))
        self.assertLessEqual(len(r["groupe"]), 9)
        self.assertNotIn("tibtrav", r["groupe"])
        self.assertEqual(set(r["groupe"]), set(r["noyau"]) | set(r["accompagnatrices"]))
        self.assertTrue(r["autres"])                                                         # des proches au-delà de 9

    def test_tathnouthassan_les_controles(self):
        d = self.donnees("tathnouthassan")
        r = A.calculer(d)
        self.assertEqual((r["unites_x"], r["victoires_x"]), (60, 4))
        ligne = {l["cle"]: l for l in r["lignes"]}
        self.assertEqual(self.noms(d, r["noyau"]), {"TATHNOUT HASSAN", "STE AWRACH TADLA", "STE SONATRADIC"})
        for cle in ("awrachtadla", "sonatradic"):                  # présentes sur 4 de ses 4 victoires
            self.assertEqual((ligne[cle]["victoires_x_avec"], r["victoires_x"]), (4, 4))
        self.assertEqual(ligne["tibtrav"]["lecture"], "concurrent")

    @unittest.skipUnless(NODE, "Node n'est pas installé")
    def test_vingt_societes_python_et_javascript(self):
        # awrachtadla, puis 19 sociétés régulièrement réparties parmi celles qui ont au moins 30 marchés.
        rangees = [r[0] for r in self.c.execute(
            "SELECT cle FROM groupes_societes WHERE calcul = 'courant' AND marches >= 30 ORDER BY cle")]
        choisies = ["awrachtadla"] + [c for c in rangees[::max(1, len(rangees) // 19)] if c != "awrachtadla"][:19]
        self.assertEqual(len(choisies), 20)
        jeux = [self.donnees(c) for c in choisies]
        for r in (None, {"min_victoires": 2, "rapport_min": 1.2, "part_noyau": 0.3, "c_min": 0.5,
                         "min_victoires_y": 2, "rapport_concurrent": 1.2, "taille_groupe": 6}):
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
        self.assertEqual(set(d["lectures"]), {"noyau", "accompagnatrice", "concurrent", "neutre"})
        for c in d["societes"].values():
            self.assertIsInstance(c["total"], int)
            self.assertIsInstance(c["victoires"], int)

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
