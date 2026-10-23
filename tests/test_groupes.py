import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import planificateur
from copilote import bases, config, fusions, groupes, recalculer, reglages
from copilote.regions import region_de
from tests import outils

ACHETEURS = ["COMMUNE DE TAZA", "COMMUNE DE SAFI", "COMMUNE DE TEMARA"]


def base_de_poche(chemin: Path) -> None:
    """a, b, c soumissionnent ensemble sur 10 marchés de 2025 ; a en a 6 de plus, seul.

    d accompagne a, b, c sur 6 de ces marchés et en a 8 autres : 6/16 des marchés de a, 6/10 de
    ceux de b et c, 6/14 des siens — toujours sous 50 % d'un côté, au-dessus de 30 % des deux.
    e, f, g se croisent sur 5 marchés de 2026 seulement. Sur le marché m09, a est écrit « a2 ».
    Les « remplissages » r1…r8 donnent au hasard de quoi tirer.
    """
    db = outils.creer(chemin)
    remplir = [f"r{i}" for i in range(1, 9)]
    gagnants = ["a"] * 6 + ["b"] * 2 + ["c"] * 2
    for n in range(10):
        ref = f"m{n:02d}"
        a = "a2" if n == 9 else "a"
        offres = [(a, 95), ("b", 100), ("c", 105), (remplir[n % 8], 110), (remplir[(n + 3) % 8], 120)]
        if n < 6:
            offres.append(("d", 115))
        outils.marche(db, ref, 100, offres, gagnant=gagnants[n], prix_reference=100,
                      date=f"2025{n % 12 + 1:02d}10", acheteur=ACHETEURS[n % 3])
        outils.qualification(db, ref, "B.1- Travaux de terrassements routiers courants")
    for n in range(6):
        outils.marche(db, f"s{n}", 100, [("a", 100), (remplir[n], 110)], gagnant="a",
                      date=f"2025{n + 1:02d}20", acheteur=ACHETEURS[n % 3])
    for n in range(8):
        outils.marche(db, f"t{n}", 100, [("d", 100), (remplir[n], 110)], gagnant="d",
                      date=f"2025{n + 1:02d}25", acheteur=ACHETEURS[n % 3])
    for n in range(5):
        outils.marche(db, f"n{n}", 100, [("e", 100), ("f", 101), ("g", 102), (remplir[n], 103)],
                      gagnant="e", date=f"2026{n + 1:02d}05", acheteur=ACHETEURS[n % 3])
    db.commit()
    db.close()


class Groupes(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.d = Path(self.dossier.name)
        base_de_poche(self.d / "h.db")
        self.lecture = bases._lecture_seule(self.d / "h.db")
        self.fusions = {"a2": "a"}

    def tearDown(self):
        self.lecture.close()
        self.dossier.cleanup()

    def donnees(self, jusqua=None, fusions=None):
        return groupes.charger(self.lecture, jusqua, self.fusions if fusions is None else fusions)

    def membres(self, r):
        return [g.membres for g in r.groupes]

    def test_fusion_appliquee_au_chargement(self):
        d = self.donnees()
        self.assertNotIn("a2", d.societes)
        self.assertEqual(int(d.X[d.index["a"]].sum()), 16)
        sans = self.donnees(fusions={})
        self.assertIn("a2", sans.societes)
        self.assertEqual(int(sans.X[sans.index["a"]].sum()), 15)

    def test_niveaux_et_presence_mutuelle(self):
        d = self.donnees()
        p = {(x.a, x.b): x for x in groupes.paires(d)}
        ab = p[("a", "b")]
        self.assertEqual((ab.communs, ab.part_a, ab.part_b), (10, 10 / 16, 1.0))
        # Trois acheteurs pour tout le monde : le hasard explique l'essentiel (excès < 2), mais a et b
        # se suivent dans les deux sens sur 10 marchés — la seconde voie leur donne le niveau moyen.
        self.assertLess(ab.exces, 2)
        self.assertEqual((ab.mutuel, ab.niveau, ab.retenue), (True, "moyen", True))
        self.assertFalse(p[("a", "d")].retenue)                  # 6/16 et 6/14 : ni excès ni présence mutuelle
        self.assertFalse(p[("b", "d")].retenue)
        self.assertEqual(self.membres(groupes.detecter(d, tirages=20)), [["a", "b", "c"], ["e", "f", "g"]])
        # Sans la seconde voie, il ne reste que l'excès : e, f, g (5 marchés en 2026) n'ont qu'un lien faible.
        r = reglages.charger()
        r["groupes"]["mutuel"]["communs"] = 999
        sans = {(x.a, x.b): x for x in groupes.paires(d, reglages=r)}
        self.assertEqual((sans[("e", "f")].niveau, sans[("a", "b")].niveau), ("faible", None))
        self.assertEqual(self.membres(groupes.detecter(d, tirages=20, reglages=r)), [])

    def test_avis_oui_retient_sous_le_seuil_et_non_exclut(self):
        d = self.donnees()
        p = {(x.a, x.b): x for x in groupes.paires(d, {("a", "d"): "oui"})}
        self.assertEqual((p[("a", "d")].retenue, p[("a", "d")].origine), (True, "avis oui"))
        oui = {("a", "d"): "oui", ("b", "d"): "oui", ("c", "d"): "oui"}
        r = groupes.detecter(d, oui, tirages=20)
        self.assertIn(["a", "b", "c", "d"], self.membres(r))
        abcd = next(g for g in r.groupes if g.membres == ["a", "b", "c", "d"])
        self.assertEqual((abcd.mesures["niveau"], abcd.mesures["confirme"]), ("sans niveau", True))
        # Un « non » sur (a, b) défait la clique : ses deux autres liens restent, en groupes de 2.
        r = groupes.detecter(d, {("a", "b"): "non"}, tirages=20)
        self.assertEqual(sorted(self.membres(r)), [["a", "c"], ["b", "c"], ["e", "f", "g"]])

    def test_une_paire_est_un_groupe_et_son_hasard_est_celui_de_poisson(self):
        r = groupes.detecter(self.donnees(), {("a", "b"): "non"}, tirages=20)
        ac = next(g for g in r.groupes if g.membres == ["a", "c"])
        lien = next(p for p in r.paires if (p.a, p.b) == ("a", "c"))
        m = ac.mesures
        self.assertEqual((m["methode_hasard"], m["marches_3"], m["taille"]), ("poisson", 10, 2))
        self.assertAlmostEqual(m["rapport_hasard"], lien.exces)
        self.assertAlmostEqual(m["p_hasard"], lien.p)
        self.assertAlmostEqual(m["force"], 1.0)                  # c est présente sur tous ses marchés avec a
        self.assertAlmostEqual(m["force_mutuelle"], 10 / 16)

    def test_jusqua_ne_voit_pas_l_avenir(self):
        d = self.donnees("2025-12-31")
        self.assertFalse({"e", "f", "g"} & set(d.societes))
        self.assertTrue(all(m.date <= "20251231" for m in d.marches))
        self.assertEqual(self.membres(groupes.detecter(d, tirages=20)), [["a", "b", "c"]])

    def test_mesures_du_groupe(self):
        r = groupes.detecter(self.donnees(), tirages=50)
        g = next(g for g in r.groupes if g.membres == ["a", "b", "c"])
        m = g.mesures
        self.assertEqual((m["marches_3"], m["marches_complet"], m["acheteurs"], m["regions"], m["domaines"]),
                         (10, 10, 3, 3, "B"))
        self.assertEqual((m["gains"], m["chef_de_file"], m["structure"]), (10, "a", "chef de file"))
        self.assertAlmostEqual(m["part_chef"], 0.6)
        a = next(x for x in m["membres"] if x["cle"] == "a")
        self.assertEqual((a["n_avec"], a["taux_avec"], a["n_sans"], a["taux_sans"]), (10, 0.6, 6, 1.0))
        self.assertGreaterEqual(m["rapport_hasard"], 1.0)
        self.assertTrue(0 <= g.score <= 100)
        self.assertEqual(len(m["preuves"]), 10)

    def test_enregistrer_remplace_le_meme_calcul(self):
        r = groupes.detecter(self.donnees(), tirages=20)
        calc = sqlite3.connect(self.d / "calcul.db")
        try:
            for _ in range(2):
                self.assertEqual(groupes.enregistrer(calc, r), "courant")
            self.assertEqual(calc.execute("SELECT count(*) FROM groupes").fetchone()[0], 2)
            self.assertEqual(calc.execute("SELECT count(*) FROM groupe_preuves").fetchone()[0], 15)
            r2 = groupes.detecter(self.donnees("2025-12-31"), tirages=20)
            self.assertEqual(groupes.enregistrer(calc, r2), "jusqua-2025-12-31")
            self.assertEqual(calc.execute("SELECT count(*) FROM groupes").fetchone()[0], 3)
        finally:
            calc.close()

    def test_accord_entre_score_et_avis(self):
        r = groupes.detecter(self.donnees(), tirages=20)
        a = groupes.accord(r, {("a", "b"): "oui", ("a", "r1"): "non"}, seuil_score=0)
        self.assertEqual((a["avis"], a["oui_score_haut"], a["non_score_bas"]), (2, 1, 0))

    def test_enregistrer_range_familles_liens_societes_et_memoire_des_liens(self):
        r = groupes.detecter(self.donnees(), tirages=20)
        calc = sqlite3.connect(self.d / "calcul.db")
        try:
            groupes.enregistrer(calc, r)
            lignes = calc.execute("SELECT id, permanents, occasionnels, cliques, taille, niveaux FROM familles").fetchall()
            self.assertEqual(sorted(json.loads(x[1]) for x in lignes), [["a", "b", "c"], ["e", "f", "g"]])
            self.assertTrue(all(json.loads(x[2]) == [] and x[3] == 1 and x[4] == 3 and x[5] == "moyen" for x in lignes))
            self.assertEqual(calc.execute("SELECT count(*) FROM famille_cliques").fetchone()[0], 2)
            # Toutes les paires croisées 3 fois ou plus sont rangées, avec ou sans niveau.
            liens = {(a, b): (n, ret) for a, b, n, ret in calc.execute(
                "SELECT cle_a, cle_b, niveau, retenue FROM groupes_liens")}
            self.assertEqual(liens[("a", "b")], ("moyen", 1))
            self.assertEqual(liens[("a", "d")], (None, 0))
            self.assertEqual(calc.execute("SELECT count(*) FROM lien_preuves WHERE cle_a = 'a' AND cle_b = 'b'")
                             .fetchone()[0], 10)
            # Chaque société de la fenêtre a sa ligne, même sans aucun lien.
            soc = dict(calc.execute("SELECT cle, marches FROM groupes_societes"))
            self.assertEqual((soc["a"], soc["d"]), (16, 14))
            self.assertEqual(len(soc), len(r.donnees.societes))
            # Un recalcul sans les marchés de 2026 ne retire pas les familles de la mémoire des liens.
            r2 = groupes.detecter(self.donnees("2025-12-31"), tirages=20)
            groupes.enregistrer(calc, r2)
            self.assertEqual(calc.execute("SELECT count(*) FROM familles WHERE calcul = 'courant'").fetchone()[0], 2)
            groupes.enregistrer(calc, groupes.detecter(self.donnees("2025-12-31"), tirages=20), calcul="courant")
            self.assertEqual(calc.execute("SELECT count(*) FROM familles WHERE calcul = 'courant'").fetchone()[0], 1)
            self.assertEqual(calc.execute("SELECT count(*) FROM familles_anciennes").fetchone()[0], 2)
        finally:
            calc.close()

    def test_les_tables_d_une_ancienne_version_sont_refaites(self):
        calc = sqlite3.connect(self.d / "calcul.db")
        try:
            calc.executescript("CREATE TABLE groupes (calcul TEXT, id INTEGER, seuil REAL);"
                               "INSERT INTO groupes VALUES ('courant', 1, 0.5);"
                               "CREATE TABLE familles_anciennes (id TEXT PRIMARY KEY, permanents TEXT,"
                               " occasionnels TEXT, vue_le TEXT);"
                               "INSERT INTO familles_anciennes VALUES ('abcd1234', '[]', '[]', '');")
            self.assertFalse(groupes.a_jour(calc))
            groupes.enregistrer(calc, groupes.detecter(self.donnees(), tirages=20))
            self.assertTrue(groupes.a_jour(calc))
            self.assertEqual(calc.execute("SELECT count(*) FROM groupes").fetchone()[0], 2)
            self.assertEqual(calc.execute("SELECT count(*) FROM familles_anciennes WHERE id = 'abcd1234'")
                             .fetchone()[0], 1)                           # la mémoire des liens survit
        finally:
            calc.close()


def groupe_factice(membres, score, famille=1, marches=10):
    g = groupes.Groupe(sorted(membres), famille=famille)
    g.score = score
    g.mesures = {"marches_3": marches}
    return g


class Familles(unittest.TestCase):
    def test_permanents_dans_toutes_les_cliques_occasionnels_dans_certaines(self):
        f, = groupes.ranger_familles([groupe_factice("abcd", 60), groupe_factice("abce", 70), groupe_factice("abc", 50)])
        self.assertEqual((f.permanents, f.occasionnels), (["a", "b", "c"], ["d", "e"]))
        self.assertEqual(f.membres, ["a", "b", "c", "d", "e"])
        self.assertEqual(f.score, 70)                                  # score de la meilleure clique
        self.assertEqual(f.meilleure.membres, ["a", "b", "c", "e"])
        self.assertEqual(len(f.cliques), 3)

    def test_une_famille_sans_membre_commun_est_decoupee(self):
        # Louvain a rangé ensemble abc, abd et cde : aucun membre n'est dans les trois.
        familles = groupes.ranger_familles([groupe_factice("abc", 80), groupe_factice("abd", 70), groupe_factice("cde", 60)])
        self.assertEqual([f.permanents for f in familles], [["a", "b"], ["c", "d", "e"]])
        self.assertTrue(all(f.permanents for f in familles))
        self.assertEqual([[g.membres for g in f.cliques] for f in familles], [[["a", "b", "c"], ["a", "b", "d"]], [["c", "d", "e"]]])

    def test_l_empreinte_ne_depend_que_des_permanents(self):
        a, = groupes.ranger_familles([groupe_factice("abcd", 60), groupe_factice("abce", 70)])
        b, = groupes.ranger_familles([groupe_factice("abcf", 10), groupe_factice("abc", 20)])
        self.assertEqual(a.cle, b.cle)                                 # mêmes permanents a, b, c
        self.assertEqual(a.cle, groupes.empreinte(["c", "a", "b"]))    # l'ordre d'entrée ne compte pas
        self.assertRegex(a.cle, r"^[0-9a-f]{8}$")
        autre, = groupes.ranger_familles([groupe_factice("abd", 60)])
        self.assertNotEqual(a.cle, autre.cle)

    def test_deux_familles_de_memes_permanents_gardent_des_identifiants_distincts(self):
        f1, f2 = groupes.ranger_familles([groupe_factice("abc", 60, famille=1), groupe_factice("abc", 50, famille=2)])
        self.assertNotEqual(f1.cle, f2.cle)
        self.assertEqual(f1.cle, groupes.empreinte(["a", "b", "c"]))   # la mieux classée garde l'empreinte simple
        self.assertEqual((f1.numero, f2.numero), (1, 2))


class Avis(unittest.TestCase):
    def test_dernier_avis_tranche_nsp_ignore_jusqua_et_fusions(self):
        db = sqlite3.connect(":memory:")
        groupes.preparer_retours(db)
        lignes = [("a", "b", "oui", "2025-03-01"), ("a", "b", "non", "2026-02-01"),
                  ("a", "c", "nsp", "2025-01-01"), ("x2", "d", "oui", "2025-01-01")]
        for a, b, rep, quand in lignes:
            db.execute("INSERT INTO avis_groupes (cle_a, cle_b, reponse, auteur, cree_le)"
                       " VALUES (?,?,?,?,?)", (a, b, rep, "Hamza", quand))
        self.assertEqual(groupes.lire_avis(db, None, {"x2": "x"}),
                         {("a", "b"): "non", ("d", "x"): "oui"})
        self.assertEqual(groupes.lire_avis(db, "2025-12-31", {})[("a", "b")], "oui")


class Outils(unittest.TestCase):
    def test_lettre_de_qualification(self):
        self.assertEqual(groupes.lettre_de("B.1- Travaux de terrassements routiers courants"), "B")
        self.assertEqual(groupes.lettre_de("B.11- Travaux de dallage"), "B")
        self.assertEqual(groupes.lettre_de("Classe S", "J- Electricite"), "J")
        self.assertIsNone(groupes.lettre_de("Classe S", "2 Travaux De Voirie"))

    def test_region_du_nom_de_l_acheteur(self):
        self.assertEqual(region_de("Commune urbaine de SIDI SLIMANE"), "Rabat-Salé-Kénitra")
        self.assertEqual(region_de("PROVINCE DE BEN SLIMANE"), "Casablanca-Settat")
        self.assertEqual(region_de("REGION DE GUELMIM - OUED NOUN"), "Guelmim-Oued Noun")
        self.assertEqual(region_de("Direction provinciale de Fès"), "Fès-Meknès")
        self.assertIsNone(region_de("OFFICE NATIONAL DES AEROPORTS"))

    def test_fusions_chaines_cycle_et_absence(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "f.csv"
            f.write_text("cle_garder;cle_fusionner;raison\nb;a;x\nc;b;y\n", encoding="utf-8")
            self.assertEqual(fusions.charger(f), {"a": "c", "b": "c"})
            f.write_text("cle_garder;cle_fusionner;raison\nb;a;x\na;b;y\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                fusions.charger(f)
            self.assertEqual(fusions.charger(Path(t) / "absent.csv"), {})

    def test_fichier_des_fusions_validees(self):
        table = fusions.charger()
        self.assertEqual(table["hajdouirisohadu"], "hajduirisohadu")
        self.assertFalse(set(table) & set(table.values()))


class Commande(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.d = Path(self.dossier.name)
        base_de_poche(self.d / "pv_travaux.db")
        bases.definir_sources(self.d / "pv_travaux.db", self.d / "ao.db")
        self.patchs = [mock.patch.object(config, "COPILOTE_CALCUL", self.d / "calcul.db"),
                       mock.patch.object(config, "COPILOTE_RETOURS", self.d / "retours.db")]
        for p in self.patchs:
            p.start()

    def tearDown(self):
        for p in self.patchs:
            p.stop()
        bases._sources.clear()
        self.dossier.cleanup()

    def test_recalculer_groupes_ecrit_dans_la_base_de_calcul(self):
        ret = bases.retours()
        groupes.preparer_retours(ret)
        ret.execute("INSERT INTO avis_groupes (cle_a, cle_b, reponse, auteur, cree_le)"
                    " VALUES ('a', 'b', 'non', 'Hamza', '2026-01-01')")
        ret.commit()
        ret.close()
        with mock.patch("sys.stdout"):
            self.assertEqual(recalculer.recalculer_groupes(tirages=20), "courant")
            self.assertEqual(recalculer.recalculer_groupes("2025-12-31", tirages=20), "jusqua-2025-12-31")
        calc = bases.calcul()
        lignes = dict(calc.execute("SELECT calcul, groupes FROM groupes_calculs"))
        calc.close()
        # Courant : l'avis « non » sur (a, b) défait {a, b, c} ; restent {a, c}, {b, c} et {e, f, g}.
        # Jusqu'à fin 2025 : l'avis n'existait pas, et e, f, g pas encore.
        self.assertEqual(lignes, {"courant": 3, "jusqua-2025-12-31": 1})
        hist = bases.historique()
        self.assertEqual(hist.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'groupe%'")
                         .fetchone()[0], 0)
        hist.close()


class Planificateur(unittest.TestCase):
    def test_recalcul_hebdomadaire_attend_sept_jours(self):
        with tempfile.TemporaryDirectory() as t:
            marque = Path(t) / "m.txt"
            with mock.patch.object(planificateur, "MARQUE_GROUPES", marque), \
                    mock.patch.object(planificateur.subprocess, "Popen") as popen:
                hier = datetime.now(timezone.utc) - timedelta(days=1)
                marque.write_text(hier.date().isoformat(), encoding="utf-8")
                planificateur.recalcul_hebdomadaire()
                popen.assert_not_called()
                marque.write_text((hier - timedelta(days=7)).date().isoformat(), encoding="utf-8")
                popen.return_value.stdout = []
                popen.return_value.wait.return_value = 0
                with mock.patch("builtins.print"):
                    planificateur.recalcul_hebdomadaire()
                popen.assert_called_once()
                self.assertEqual(popen.call_args.args[0][-3:], ["-m", "copilote.recalculer", "groupes"])
                self.assertEqual(marque.read_text(encoding="utf-8"),
                                 datetime.now(timezone.utc).date().isoformat())


if __name__ == "__main__":
    unittest.main()
