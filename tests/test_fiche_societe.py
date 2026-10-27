"""La fiche d'une société : comptage par unité (lots compris), profils et marchés en preuve.

Une base de poche de 4 marchés dont un alloti. x gagne 3 unités sur 5, y en gagne 2 ; z ne croise x
que deux fois. Les tests portent sur le calcul lui-même ; l'API et la page sont couvertes par
tests/test_page_groupes.py.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from copilote import bases, config, fiche_societe, groupes
from tests import outils

FUSIONS = {"x2": "x"}


def base_de_poche(chemin: Path) -> None:
    """o1, o2 gagnés par x ; o3 par y ; a1 est alloti : x gagne le lot 1, y le lot 2.

    Sur o2, x est écrit « x2 » (une fusion le ramène à x). z ne dépose que sur o1 et ses 6 marchés
    à elle, qu'elle gagne toutes : son taux de victoire ne doit pas se confondre avec celui de x.
    """
    db = outils.creer(chemin)
    outils.marche(db, "o1", 100, [("x", 90), ("y", 95), ("z", 120)], gagnant="x", date="20250110")
    outils.marche(db, "o2", 100, [("x2", 90), ("y", 100)], gagnant="x", date="20250210")
    outils.marche(db, "o3", 100, [("x", 110), ("y", 90)], gagnant="y", date="20250310")
    outils.marche(db, "a1", None, [("x", 95, "admis", "1"), ("y", 100, "admis", "1"),
                                   ("y", 190, "admis", "2"), ("x", 200, "admis", "2")],
                  gagnant=None, date="20250410", nb_lots=2)
    outils.lot(db, "a1", "1", 100, gagnant="x")
    outils.lot(db, "a1", "2", 200, gagnant="y")
    for n in range(6):
        outils.marche(db, f"z{n}", 100, [("z", 90), ("y", 130)], gagnant="z", date=f"20250{n + 1}20")
    db.commit()
    db.close()


class Fiche(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.d = Path(self.dossier.name)
        base_de_poche(self.d / "pv_travaux.db")
        self.patchs = [mock.patch.object(config, "COPILOTE_CALCUL", self.d / "calcul.db"),
                       mock.patch.object(config, "COPILOTE_RETOURS", self.d / "retours.db")]
        for p in self.patchs:
            p.start()
        bases.definir_sources(self.d / "pv_travaux.db", self.d / "ao_ouverts.db")
        fiche_societe._victoires_memo.clear()
        self.hist, self.calc, self.ret = bases.historique(), bases.calcul(), bases.retours()
        donnees = groupes.charger(self.hist, None, FUSIONS)
        groupes.enregistrer(self.calc, groupes.detecter(donnees, tirages=20))

    def tearDown(self):
        for c in (self.hist, self.calc, self.ret):
            c.close()
        for p in self.patchs:
            p.stop()
        bases._sources.clear()
        fiche_societe._victoires_memo.clear()
        self.dossier.cleanup()

    def fiche(self, cle="x"):
        f = fiche_societe.fiche(self.hist, self.calc, self.ret, cle, FUSIONS)
        self.assertIsNotNone(f, cle)
        return f, {x["code"]: x for x in f["sections"]}

    def ligne(self, section, cle):
        return next(x for x in section["lignes"] if x["societe"]["cle"] == cle)

    # ------------------------------------------------------------------ comptage

    def test_un_marche_alloti_se_compte_lot_par_lot(self):
        f, _ = self.fiche("x")
        s = f["societe"]
        self.assertEqual((s["marches"], s["unites"]), (4, 5))          # a1 donne deux unités
        self.assertEqual((s["victoires"], s["defaites"], s["non_attribuees"]), (3, 2, 0))
        self.assertEqual(s["victoires_totales"], 3)                    # o1, o2 et le lot 1 de a1

    def test_fusion_appliquee_aux_offres_et_a_l_attributaire(self):
        """Sur o2, x est écrit « x2 » : sans la fusion, x n'aurait que 3 marchés et 2 victoires."""
        f, sec = self.fiche("x")
        self.assertEqual(f["societe"]["marches"], 4)
        y = self.ligne(sec["gagne_avec"], "y")
        self.assertEqual((y["marches_communs"], y["unites_communes"]), (4, 5))
        # Sans la fusion, l'offre de o2 appartient à une autre société : x n'y est plus présente.
        sans = fiche_societe.fiche(self.hist, self.calc, self.ret, "x", {})
        self.assertEqual((sans["societe"]["marches"], sans["societe"]["unites"],
                          sans["societe"]["victoires"]), (3, 4, 2))
        self.assertEqual(self.ligne({"lignes": sans["sections"][0]["lignes"]}, "y")["unites_communes"], 4)

    def test_presentes_quand_elle_gagne_et_position_des_offres(self):
        _, sec = self.fiche("x")
        a = sec["gagne_avec"]
        self.assertEqual([x["societe"]["cle"] for x in a["lignes"]], ["y"])   # z n'y est que 1 fois
        y = self.ligne(a, "y")
        self.assertEqual((y["victoires_societe"], y["victoires_societe_total"]), (3, 3))
        self.assertEqual((y["victoires_partenaire"], y["victoires_partenaire_total"]), (2, 2))
        self.assertEqual(y["part_marches_societe"], 1.0)               # y est sur les 4 marchés de x
        # o1 95/90, o2 100/90, lot 1 100/95 : la médiane est celle de o1
        self.assertAlmostEqual(y["position_quand_elle_gagne"], 95 / 90 - 1, places=6)
        # o3 110/90 et lot 2 200/190 : la médiane des deux
        self.assertAlmostEqual(y["position_quand_il_gagne"], ((110 / 90 - 1) + (200 / 190 - 1)) / 2, places=6)
        self.assertEqual(y["profil"]["code"], "tour_de_role")

    def test_sections_accompagne_et_concurrents(self):
        _, sec = self.fiche("x")
        y = self.ligne(sec["accompagne"], "y")
        self.assertEqual(y["victoires_partenaire"], 2)                 # o3 et le lot 2 de a1
        self.assertEqual([x["societe"]["cle"] for x in sec["concurrents"]["lignes"]], ["y"])
        # Vue depuis y : z gagne 6 marchés où y est présente et perd, x en gagne 3
        _, sy = self.fiche("y")
        self.assertEqual([x["societe"]["cle"] for x in sy["concurrents"]["lignes"]], ["z", "x"])
        z = self.ligne(sy["concurrents"], "z")
        self.assertEqual((z["victoires_partenaire"], z["victoires_societe"]), (6, 0))
        self.assertEqual(z["profil"]["code"], "concurrent")

    def test_marches_en_preuve(self):
        """Les preuves sont rangées à part : une société vue dans deux sections ne les envoie qu'une fois."""
        f, sec = self.fiche("x")
        y = self.ligne(sec["gagne_avec"], "y")
        self.assertEqual(y["preuves_total"], 5)
        preuves = f["preuves"]["y"]
        self.assertEqual({x["societe"]["cle"] for s in f["sections"] for x in s["lignes"]}, set(f["preuves"]))
        self.assertEqual([(m["ref"], m["lot"]) for m in preuves],
                         [("a1", "2"), ("a1", "1"), ("o3", None), ("o2", None), ("o1", None)])
        lot2 = preuves[0]
        self.assertEqual((lot2["gagnant"]["cle"], lot2["gagne"]), ("y", "partenaire"))
        self.assertEqual((lot2["taux_societe"], lot2["taux_partenaire"]), (1.0, 0.95))
        o1 = preuves[-1]
        self.assertEqual((o1["gagne"], o1["taux_societe"], o1["taux_partenaire"]), ("societe", 0.9, 0.95))
        self.assertEqual(o1["date"], "2025-01-10")

    def test_une_cle_inconnue_n_a_pas_de_fiche(self):
        self.assertIsNone(fiche_societe.fiche(self.hist, self.calc, self.ret, "personne", FUSIONS))

    def test_les_victoires_de_chacun_sont_gardees_en_memoire(self):
        total = fiche_societe.victoires_par_societe(self.hist, FUSIONS)
        self.assertEqual((total["x"], total["y"], total["z"]), (3, 2, 6))
        self.assertIs(fiche_societe.victoires_par_societe(self.hist, FUSIONS), total)
        autre = fiche_societe.victoires_par_societe(self.hist, {})     # autre table : on recompte
        self.assertEqual(autre["x"], 3)                                # l'attributaire de o2 est déjà « x »

    def test_la_lecture_n_ecrit_rien_dans_l_historique(self):
        avant = (self.d / "pv_travaux.db").read_bytes()
        self.fiche("x")
        self.assertEqual((self.d / "pv_travaux.db").read_bytes(), avant)
        with self.assertRaises(sqlite3.OperationalError):
            self.hist.execute("DELETE FROM marches")


class Profils(unittest.TestCase):
    """Les phrases de profil : factuelles, jamais une accusation."""

    def test_echelle_des_profils(self):
        p = fiche_societe.profil
        self.assertEqual(p(7, 1, 9)["code"], "accompagne")             # présente partout, gagne rarement
        self.assertIn("la plupart de ses victoires", p(7, 1, 9)["phrase"])
        self.assertIn("une partie de ses victoires", p(3, 1, 20)["phrase"])
        self.assertEqual(p(4, 10, 14)["code"], "concurrent")           # elle gagne deux fois plus
        self.assertEqual(p(5, 4, 13)["code"], "tour_de_role")
        self.assertEqual(p(0, 3, 10)["code"], "concurrent")
        self.assertEqual(p(1, 0, 10)["code"], "partage")
        self.assertEqual(p(0, 0, 10)["code"], "sans_resultat")
        for vx in range(0, 8):
            for vy in range(0, 12):
                self.assertIn(p(vx, vy, 10)["code"],
                              {"accompagne", "concurrent", "tour_de_role", "partage", "sans_resultat"})


if __name__ == "__main__":
    unittest.main()
