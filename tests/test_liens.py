"""Liens v2 : attendu par hasard, excès, niveaux, offres écartées, défaites, score, familles plafonnées."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.stats import poisson

from copilote import bases, groupes, liens, reglages
from tests import outils
from tests.test_groupes import groupe_factice


def base_deux_acheteurs(chemin: Path) -> None:
    """Acheteur K1 : 4 marchés ; A et B sur k1 et k2, C sur k3 et k4. Acheteur K2 : 2 marchés, A sur l1, B sur l2.

    Attendu(A, B) = K1 : 2 × 2 / 4 = 1  +  K2 : 1 × 1 / 2 = 0,5  =  1,5 ; observé 2 ; excès 4/3.
    Sur k2, l'offre de B est écartée : elle compte quand même comme une présence.
    """
    db = outils.creer(chemin)
    outils.marche(db, "k1", 100, [("a", 95), ("b", 99)], gagnant="a", acheteur="K1")
    outils.marche(db, "k2", 100, [("a", 95), ("b", 99, "ecarte_dossier_administratif_technique")],
                  gagnant="a", acheteur="K1")
    outils.marche(db, "k3", 100, [("c", 95)], gagnant="c", acheteur="K1")
    outils.marche(db, "k4", 100, [("c", 95)], gagnant="c", acheteur="K1")
    outils.marche(db, "l1", 100, [("a", 95)], gagnant="a", acheteur="K2")
    outils.marche(db, "l2", 100, [("b", 95)], gagnant="b", acheteur="K2")
    db.commit()
    db.close()


class Attendu(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        base_deux_acheteurs(Path(self.dossier.name) / "h.db")
        self.db = bases._lecture_seule(Path(self.dossier.name) / "h.db")
        self.d = groupes.charger(self.db, None, {})

    def tearDown(self):
        self.db.close()
        self.dossier.cleanup()

    def test_attendu_excès_et_poisson(self):
        L = liens.calculer(self.d, min_communs=1)
        k = next(k for k in range(len(L)) if {self.d.societes[L.i[k]], self.d.societes[L.j[k]]} == {"a", "b"})
        self.assertEqual(L.communs[k], 2)                # k2 compte : l'offre écartée de B est une présence
        self.assertAlmostEqual(L.attendu[k], 1.5)
        self.assertAlmostEqual(L.exces[k], 2 / 1.5)
        self.assertAlmostEqual(L.p[k], poisson.sf(1, 1.5))
        self.assertAlmostEqual(liens.paire_isolee(self.d, L.i[k], L.j[k])["attendu"], 1.5)

    def test_niveaux_selon_les_seuils(self):
        seuils = reglages.charger()["groupes"]["niveaux"]
        rang = liens.niveau_de([6, 6, 4, 3, 3, 20], [5, 4.9, 3, 2, 1.9, 1], [1e-5, 1e-5, 5e-4, 5e-3, 5e-3, 1e-9], seuils)
        self.assertEqual(rang.tolist(), [3, 2, 2, 1, 0, 0])
        mutuel = np.array([False, False, False, False, False, True])
        self.assertEqual(liens.niveau_de([6], [5], [1e-5], seuils, np.array([True])).tolist(), [3])   # jamais rabaissé
        self.assertEqual(liens.niveau_de([6, 6, 4, 3, 3, 20], [5, 4.9, 3, 2, 1.9, 1],
                                         [1e-5, 1e-5, 5e-4, 5e-3, 5e-3, 1e-9], seuils, mutuel).tolist()[-1], 2)


class Defaites(unittest.TestCase):
    def test_marche_perdu_gagnant_exterieur_ecart_et_membres_ecartes(self):
        with tempfile.TemporaryDirectory() as t:
            db = outils.creer(Path(t) / "h.db")
            # Estimation 100, P = (moyenne admise + 100) / 2. x gagne m1 ; a et b y sont ; sur m2 a et b sont écartés.
            for n in range(6):
                outils.marche(db, f"g{n}", 100, [("a", 98), ("b", 103), (f"z{n}", 120)], gagnant="a", prix_reference=100,
                              date=f"2025010{n + 1}")
            outils.marche(db, "m1", 100, [("a", 96), ("b", 104), ("x", 99)], gagnant="x", prix_reference=99.5,
                          date="20250201")
            outils.marche(db, "m2", 100, [("a", 96, "ecarte_dossier_administratif_technique"),
                                          ("b", 97, "ecarte_dossier_administratif_technique"), ("x", 99)],
                          gagnant="x", prix_reference=99.5, date="20250301")
            db.commit()
            db.close()
            lecture = bases._lecture_seule(Path(t) / "h.db")
            try:
                d = groupes.charger(lecture, None, {})
            finally:
                lecture.close()
        r = groupes.detecter(d, {("a", "b"): "oui"}, tirages=20)
        ab = next(g for g in r.groupes if g.membres == ["a", "b"])
        v = ab.mesures["victoires"]
        self.assertEqual((v["unites"], v["gagnees"], v["perdues"], v["perdues_ecartes"]), (8, 6, 2, 1))
        m1 = next(x for x in ab.defaites if x["ref"] == "m1")
        self.assertEqual((m1["gagnant_cle"], m1["meilleur_membre"], m1["membres_ecartes"]), ("x", "a", False))
        self.assertAlmostEqual(m1["ecart_meilleure"], 0.99 - 0.96)
        self.assertAlmostEqual(m1["ecart_reference"], 0.99 - 0.995)
        m2 = next(x for x in ab.defaites if x["ref"] == "m2")
        self.assertEqual((m2["membres_ecartes"], m2["meilleur_membre"]), (True, None))
        # La paire a ses propres défaites, rangées dans groupes_defaites sous les deux types.
        lien = next(p for p in r.paires if (p.a, p.b) == ("a", "b"))
        resume, marches, perdues = groupes.mesurer_paire(lien, r.profils)
        self.assertEqual((len(marches), len(perdues), resume["perdues_ecartes"]), (8, 2, 1))
        calc = sqlite3.connect(":memory:")
        groupes.enregistrer(calc, r)
        self.assertEqual(dict(calc.execute("SELECT type, count(*) FROM groupes_defaites GROUP BY type")),
                         {"groupe": 2, "paire": 2})


class Score(unittest.TestCase):
    def test_qui_gagne_ne_change_pas_le_score(self):
        base = {"force": 0.8, "marches_3": 12, "rapport_hasard": 4.0, "p_hasard": 0.01, "acheteurs": 3,
                "regions": 2, "domaines": "AB", "part_hors_profil": 0.05}
        scores = []
        for part in (0.0, 0.3, 0.9):
            g = groupes.Groupe(["a", "b"], mesures={**base, "part_gains": part})
            groupes.score(g)
            scores.append(g.score)
        self.assertEqual(len(set(scores)), 1)
        poids = groupes.poids_effectifs()
        self.assertAlmostEqual(sum(poids.values()), 100)
        self.assertEqual(poids["gains"], 0)
        self.assertAlmostEqual(poids["hasard"], 30 * 100 / 85)


class FamillesPlafonnees(unittest.TestCase):
    def test_une_famille_trop_grande_est_redecoupee(self):
        import networkx as nx
        # Deux paquets denses (a1…a8, b1…b8) reliés par c : Louvain les met ensemble, 17 sociétés.
        cliques = []
        G = nx.Graph()
        for paquet in ("a", "b"):
            membres = [f"{paquet}{k}" for k in range(1, 9)]
            for k in range(0, 8, 2):
                c = sorted(membres[k:k + 4] + ["c"]) if k + 4 <= 8 else sorted(membres[k:] + membres[:2] + ["c"])
                cliques.append(groupe_factice(c, 50 + k))
                G.add_edges_from((x, y, {"weight": 1.0}) for i, x in enumerate(c) for y in c[i + 1:])
        sans = groupes.ranger_familles(cliques)
        self.assertGreater(max(len(f.membres) for f in sans), 12)
        avec = groupes.ranger_familles(cliques, G, 12)
        self.assertLessEqual(max(len(f.membres) for f in avec), 12)
        self.assertEqual(sorted(g.signature for f in avec for g in f.cliques), sorted(g.signature for g in cliques))
        self.assertTrue(all(f.permanents for f in avec))

    def test_une_clique_seule_plus_grande_reste_entiere(self):
        import networkx as nx
        c = [f"s{k:02d}" for k in range(14)]
        G = nx.complete_graph(c)
        f, = groupes.ranger_familles([groupe_factice(c, 60)], G, 12)
        self.assertEqual(len(f.membres), 14)


if __name__ == "__main__":
    unittest.main()
