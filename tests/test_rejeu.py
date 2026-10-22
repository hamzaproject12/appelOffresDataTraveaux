"""Étape 3 bis : les AO rejoués. Choix, annonce, vérité à part, coupure temporelle, envoi."""
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from copilote import bases, groupes, rejeu, rejouer_ao
from tests import outils

ICI = Path(__file__).resolve().parent.parent
POOL = ["alphaconstruction", "betatravaux", "gammaroutes", "deltabatiment", "epsilonvrd", "zetaservices"]
MAINTENANT = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _offres(i, estimation, k=4, lot=None):
    """k soumissionnaires parmi POOL, à des taux de 0,92 à 1,08 ; le premier est le gagnant."""
    noms = [POOL[(i + j) % len(POOL)] for j in range(k)]
    offres = [(n, round(estimation * (0.92 + 0.05 * j), 2), "admis", *([str(lot)] if lot else [])) for j, n in enumerate(noms)]
    return noms[0], offres


def base_de_rejeu(chemin: Path) -> None:
    """15 marchés simples (classes 1 à 5), 4 allotis, 5 marchés à exclure, 6 marchés de 2025."""
    db = outils.creer(chemin)
    for i in range(15):
        est = 100_000 * (1 + 3 * i)
        gagnant, offres = _offres(i, est)
        outils.marche(db, f"s{i:02d}", est, offres, gagnant=gagnant, date=f"2026{1 + i % 9:02d}{10 + i:02d}",
                      acheteur=f"COMMUNE DE VILLE{i}", objet=f"Travaux de voirie numero {i}",
                      reference=f"{i}/2026/XYZ")
        outils.qualification(db, f"s{i:02d}", f"B.{1 + i % 3}- Travaux de terrassements routiers courants",
                             "B- Travaux routiers", classe=str(1 + i % 5))
    for i, nb in enumerate([2, 3, 4, 2]):
        ref = f"a{i}"
        outils.marche(db, ref, None, [], date=f"2026{2 + 2 * i:02d}20", nb_lots=nb, acheteur=f"PROVINCE DE ZONE{i}",
                      objet=f"Travaux d'amenagement en {nb} lots", reference=f"{i}/2026/LOTS")
        for n in range(1, nb + 1):
            est = 200_000 * (n + i)
            gagnant, offres = _offres(i + n, est, lot=n)
            outils.lot(db, ref, str(n), est, gagnant=gagnant, intitule=f"LOT N°{n}", classes="3")
            for o in offres:
                db.execute("INSERT INTO concurrents (ref, nom, cle, montant_acte, montant_verifie, statut, lots)"
                           " VALUES (?,?,?,?,?,?,?)", (ref, o[0].upper(), o[0], o[1], o[1], o[2], o[3]))
    # À exclure : infructueux, deux offres seulement, sans attributaire, date douteuse, objet qui nomme un concurrent.
    mauvais = {"x_inf": dict(infructueux=1), "x_peu": dict(k=2), "x_sans": dict(gagnant=""),
               "x_dou": dict(douteuse=1), "x_nom": dict(objet="Travaux pour ALPHACONSTRUCTION")}
    for n, (ref, o) in enumerate(mauvais.items()):
        gagnant, offres = _offres(n, 500_000, k=o.pop("k", 4))
        outils.marche(db, ref, 500_000, offres, gagnant=o.pop("gagnant", gagnant), date=f"20260{3 + n}05",
                      acheteur=f"AUTRE ACHETEUR{n}", objet=o.pop("objet", "Travaux divers"), **o)
        outils.qualification(db, ref, "B.1- Travaux de terrassements", "B- Travaux routiers", classe="3")
    for i in range(6):
        gagnant, offres = _offres(i, 300_000)
        outils.marche(db, f"h{i}", 300_000, offres, gagnant=gagnant, date=f"2025{i + 1:02d}15",
                      acheteur=f"COMMUNE DE VILLE{i}", objet="Historique")
    db.commit()
    db.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.d = Path(self.dossier.name)
        base_de_rejeu(self.d / "h.db")
        self.hist = bases._lecture_seule(self.d / "h.db")
        self.calc = sqlite3.connect(self.d / "calcul.db")
        self.calc.row_factory = sqlite3.Row
        rejouer_ao.preparer(self.calc)

    def tearDown(self):
        self.hist.close()
        self.calc.close()
        self.dossier.cleanup()

    def choix(self, n=12, allotis=3, graine=0):
        return rejouer_ao.choisir(rejouer_ao.candidats(self.hist), n, allotis, graine)


class Choix(Base):
    def test_candidats_exclus_et_retenus(self):
        refs = {c.ref for c in rejouer_ao.candidats(self.hist)}
        self.assertEqual(refs, {f"s{i:02d}" for i in range(15)} | {f"a{i}" for i in range(4)})
        for exclu in ("x_inf", "x_peu", "x_sans", "x_dou", "x_nom"):
            self.assertNotIn(exclu, refs)
        self.assertFalse({"h0", "h1"} & refs)                       # 2025 : pas dans le jeu

    def test_choix_varie_en_acheteur_classe_montant_et_allotis(self):
        c = self.choix()
        self.assertEqual(len(c), 12)
        self.assertEqual(sum(1 for x in c if x.nb_lots), 3)
        self.assertEqual(len({x.cle_acheteur for x in c}), 12)       # un acheteur une seule fois
        self.assertEqual({x.classe for x in c if not x.nb_lots}, {1, 2, 3, 4, 5})
        montants = sorted(x.montant for x in c if not x.nb_lots)
        self.assertGreater(montants[-1] / montants[0], 5)           # des petits et des gros marchés
        self.assertGreaterEqual(len({x.mois for x in c}), 6)         # étalé sur l'année
        self.assertTrue(all(x.date.startswith("2026") for x in c))

    def test_meme_graine_meme_choix_autre_graine_autre_choix(self):
        refs = lambda g: [x.ref for x in self.choix(graine=g)]
        self.assertEqual(refs(0), refs(0))
        self.assertNotEqual(set(refs(0)), set(refs(7)))

    def test_trop_peu_de_candidats_donne_ce_qu_on_a(self):
        self.assertEqual(len(self.choix(n=40, allotis=3)), 18)       # 3 allotis demandés + les 15 simples qui existent


class Annonce(Base):
    def setUp(self):
        super().setUp()
        self.choisis = self.choix()
        self.refs = rejouer_ao.enregistrer(self.calc, self.choisis)

    def test_format_de_l_api(self):
        for c, ref in zip(self.choisis, self.refs):
            a = rejouer_ao.annonce(c, ref)
            self.assertTrue(re.fullmatch(r"REJEU-\d\d", a["ref_annonce"]))
            self.assertEqual((a["clients"], a["reference"], a["source"]), (["Test"], ref, "rejeu"))
            self.assertGreater(a["estimation"], 0)
            self.assertEqual(bool(c.nb_lots), "lots" in a)
            for q in a["qualifications"]:
                self.assertEqual(set(q), {"libelle", "classe"})
                self.assertTrue(q["libelle"] is None or re.fullmatch(r"[A-Z]{1,2}\.\d+", q["libelle"]), q)
        simple = next(rejouer_ao.annonce(c, r) for c, r in zip(self.choisis, self.refs) if not c.nb_lots)
        self.assertRegex(simple["qualifications"][0]["libelle"], r"^B\.[123]$")       # code, pas « B.1- Travaux… »
        alloti = next((rejouer_ao.annonce(c, r), c) for c, r in zip(self.choisis, self.refs) if c.nb_lots)
        self.assertEqual(len(alloti[0]["lots"]), alloti[1].nb_lots)
        self.assertAlmostEqual(alloti[0]["estimation"], sum(l["estimation"] for l in alloti[0]["lots"]))
        self.assertEqual(alloti[0]["qualifications"], [{"libelle": None, "classe": "3"}])    # classes des lots

    def test_aucune_fuite_dans_l_annonce(self):
        """Ni la référence réelle, ni un résultat, ni un nom de concurrent dans ce qui part vers l'API."""
        for a in rejouer_ao.annonces_a_envoyer(self.calc, MAINTENANT):
            texte = json.dumps(a, ensure_ascii=False).lower()
            for v in rejouer_ao.lire_verite(self.calc, a["ref_annonce"]):
                self.assertNotIn(v["ref_reel"].lower(), texte)
                self.assertNotIn((v["reference_reelle"] or "x/x").lower(), texte)
                self.assertNotIn(v["nom_gagnant"].lower(), texte)
                for k in v["concurrents"]:
                    self.assertNotIn(k["cle"], texte)
                    self.assertNotIn(str(int(k["montant"])), texte)
            self.assertNotIn("gagnant", texte)
            self.assertNotIn("attributaire", texte)

    def test_dates_posees_a_l_envoi_toujours_dans_le_futur(self):
        annonces = rejouer_ao.annonces_a_envoyer(self.calc, MAINTENANT)
        self.assertEqual(len(annonces), 12)
        for a in annonces:
            self.assertGreater(a["date_limite"], "2026-11-10")       # au moins 20 jours
            self.assertLessEqual(a["date_limite"], "2026-12-11T10:00:00")
            self.assertEqual(a["publie_le"], "2026-10-21")
        self.assertGreater(len({a["date_limite"] for a in annonces}), 3)                    # pas tous le même jour
        dans_un_an = datetime(2027, 10, 22, tzinfo=timezone.utc)
        self.assertGreater(rejouer_ao.annonces_a_envoyer(self.calc, dans_un_an)[0]["date_limite"], "2027-11-10")

    def test_la_verite_est_complete_et_sous_le_bon_ref_annonce(self):
        lignes = self.calc.execute("SELECT count(*) FROM rejeu_verite").fetchone()[0]
        self.assertEqual(lignes, sum(max(1, c.nb_lots) for c in self.choisis))
        for c, ref in zip(self.choisis, self.refs):
            vs = rejouer_ao.lire_verite(self.calc, ref)
            self.assertEqual([v["lot"] for v in vs], [l["lot"] for l in c.lots] if c.nb_lots else [""])
            for v, u in zip(vs, c.unites):
                self.assertEqual((v["ref_reel"], v["date_ouverture"], v["cle_gagnant"]), (c.ref, c.date, u.cle_gagnant))
                self.assertEqual(len(v["concurrents"]), len(u.concurrents))
                self.assertIsNotNone(v["prix_reference"])
                self.assertAlmostEqual(v["taux_gagnant"], next(k["taux"] for k in v["concurrents"] if k["cle"] == v["cle_gagnant"]))
                self.assertEqual([k["rang"] for k in v["concurrents"]][0], 1)        # classés : le rang 1 en tête

    def test_verite_vide_pour_un_vrai_ao_et_pour_un_rejeu_inconnu(self):
        self.assertEqual(rejouer_ao.lire_verite(self.calc, "AO-2026-45"), [])
        self.assertEqual(rejouer_ao.lire_verite(self.calc, "REJEU-99"), [])

    def test_renouveler_remplace_tout(self):
        rejouer_ao.enregistrer(self.calc, self.choix(n=5, allotis=1), renouveler=True)
        self.assertEqual(self.calc.execute("SELECT count(*) FROM rejeu_ao").fetchone()[0], 5)
        self.assertEqual(self.calc.execute("SELECT count(DISTINCT ref_annonce) FROM rejeu_verite").fetchone()[0], 5)


class Coupure(Base):
    """Le moteur ne voit que ce qui précède l'ouverture réelle."""

    def setUp(self):
        super().setUp()
        self.choisis = self.choix()
        self.refs = rejouer_ao.enregistrer(self.calc, self.choisis)
        self.c, self.ref = self.choisis[0], self.refs[0]

    def test_jusqua_est_la_veille_de_l_ouverture(self):
        jusqua = rejeu.jusqua_pour(self.ref, self.calc)
        self.assertEqual(jusqua, rejeu.veille(self.c.date))
        self.assertLess(jusqua.replace("-", ""), self.c.date)
        self.assertEqual(rejeu.veille("20260301"), "2026-02-28")
        self.assertEqual(rejeu.veille("20260101"), "2025-12-31")

    def test_un_vrai_ao_n_a_pas_de_coupure_et_un_rejeu_sans_coupure_est_une_erreur(self):
        self.assertIsNone(rejeu.jusqua_pour("AO-2026-45", self.calc))
        self.assertIsNone(rejeu.jusqua_pour(None, self.calc))
        with self.assertRaises(rejeu.RejeuSansCoupure):
            rejeu.jusqua_pour("REJEU-99", self.calc)
        vide = sqlite3.connect(self.d / "vide.db")                      # aucune table rejeu_ao du tout
        try:
            with self.assertRaises(rejeu.RejeuSansCoupure):
                rejeu.jusqua_pour("REJEU-01", vide)
            with self.assertRaises(rejeu.RejeuSansCoupure):
                rejeu.unites_pour(self.hist, "REJEU-01", vide)
        finally:
            vide.close()

    def test_les_unites_vues_sont_toutes_anterieures_et_sans_le_marche_lui_meme(self):
        vues = rejeu.unites_pour(self.hist, self.ref, self.calc)
        tout = rejeu.unites_pour(self.hist, "AO-2026-45", self.calc)
        self.assertGreater(len(tout), len(vues))
        self.assertTrue(vues)
        limite = self.c.date
        self.assertTrue(all(u.date < limite for u in vues))
        self.assertNotIn(self.c.ref, {u.ref for u in vues})
        self.assertIn(self.c.ref, {u.ref for u in tout})
        self.assertNotIn("x_dou", {u.ref for u in vues})                # date douteuse : écartée, pas supposée ancienne

    def test_la_coupure_est_stricte_le_jour_meme_est_exclu_la_veille_est_vue(self):
        gagnant, offres = _offres(1, 100_000)
        d = datetime.strptime(self.c.date, "%Y%m%d")
        db = sqlite3.connect(self.d / "h.db")
        for ref, jour in (("m_veille", d - timedelta(days=1)), ("m_jour", d), ("m_apres", d + timedelta(days=1))):
            outils.marche(db, ref, 100_000, offres, gagnant=gagnant, date=jour.strftime("%Y%m%d"), acheteur="X")
        db.commit()
        db.close()
        self.hist.close()
        self.hist = bases._lecture_seule(self.d / "h.db")
        vues = {u.ref for u in rejeu.unites_pour(self.hist, self.ref, self.calc)}
        self.assertIn("m_veille", vues)
        self.assertNotIn("m_jour", vues)
        self.assertNotIn("m_apres", vues)

    def test_les_groupes_et_les_profils_suivent_la_meme_coupure(self):
        self.assertEqual(rejeu.calcul_groupes_pour(self.ref, self.calc), f"jusqua-{rejeu.veille(self.c.date)}")
        self.assertEqual(rejeu.calcul_groupes_pour("AO-2026-45", self.calc), "courant")
        d = groupes.charger(self.hist, rejeu.jusqua_pour(self.ref, self.calc), {})
        self.assertNotIn(self.c.ref, {m.ref for m in d.marches})
        self.assertTrue(all(m.date < self.c.date for m in d.marches))

    def test_la_verite_n_est_lue_que_par_rejouer_ao(self):
        """Aucun autre module du copilote ne nomme la table de vérité ni sa fonction de lecture."""
        permis = {"rejouer_ao.py"}
        for chemin in sorted((ICI / "copilote").glob("*.py")):
            texte = chemin.read_text(encoding="utf-8")
            if chemin.name in permis:
                continue
            self.assertNotIn("rejeu_verite", texte, chemin.name)
            self.assertNotIn("lire_verite", texte, chemin.name)
            self.assertIsNone(re.search(r"^\s*(from|import).*rejouer_ao", texte, re.M), chemin.name)   # le moteur n'importe pas le script
        self.assertNotIn("rejeu_verite", (ICI / "serveur.py").read_text(encoding="utf-8"))


class Envoi(Base):
    JETON = "jeton-secret-d-essai"

    def setUp(self):
        super().setUp()
        rejouer_ao.enregistrer(self.calc, self.choix())
        self.appels = []

    def faux(self, statut=200, rejetes=()):
        def envoi(url, jeton, corps):
            self.appels.append((url, jeton, corps))
            return statut, {"recus": len(corps["appels_ouverts"]), "crees": 12, "maj": 0, "rejetes": list(rejetes)}
        return envoi

    def test_corps_et_jeton(self):
        r = rejouer_ao.envoyer(self.calc, "http://127.0.0.1:8000", self.JETON, envoi=self.faux())
        url, jeton, corps = self.appels[0]
        self.assertEqual((jeton, corps["clients"]), (self.JETON, ["Test"]))
        self.assertEqual(len(corps["appels_ouverts"]), 12)
        self.assertTrue(all(a["ref_annonce"].startswith("REJEU-") and a["date_limite"] for a in corps["appels_ouverts"]))
        self.assertEqual(r["crees"], 12)
        self.assertEqual(self.calc.execute("SELECT count(*) FROM rejeu_ao WHERE envoye_le IS NOT NULL").fetchone()[0], 12)

    def test_refus_sans_jeton_et_vers_une_adresse_distante(self):
        for jeton, url in (("", "http://127.0.0.1:8000"), (self.JETON, "https://pv.exemple.org")):
            with self.assertRaises(rejouer_ao.ErreurEnvoi):
                rejouer_ao.envoyer(self.calc, url, jeton, envoi=self.faux())
        self.assertEqual(self.appels, [])                                  # rien n'est parti
        rejouer_ao.envoyer(self.calc, "https://pv.exemple.org", self.JETON, distant=True, envoi=self.faux())
        self.assertEqual(len(self.appels), 1)
        for u in ("http://localhost:8000", "http://127.0.0.1:1", "http://[::1]:8000", "http://site.localhost"):
            self.assertTrue(rejouer_ao.est_local(u), u)
        for u in ("http://127.0.0.1.exemple.org", "http://exemple.org/localhost", "https://pv.exemple.org"):
            self.assertFalse(rejouer_ao.est_local(u), u)

    def test_le_jeton_n_apparait_dans_aucun_message(self):
        for statut in (401, 503, 500):
            with self.assertRaises(rejouer_ao.ErreurEnvoi) as e:
                rejouer_ao.envoyer(self.calc, "http://127.0.0.1:8000", self.JETON, envoi=self.faux(statut))
            self.assertNotIn(self.JETON, str(e.exception))

    def test_un_rejet_n_est_pas_marque_envoye(self):
        rejouer_ao.envoyer(self.calc, "http://127.0.0.1:8000", self.JETON,
                           envoi=self.faux(rejetes=[{"ref_annonce": "REJEU-03", "motif": "essai"}]))
        self.assertEqual(self.calc.execute("SELECT count(*) FROM rejeu_ao WHERE envoye_le IS NOT NULL").fetchone()[0], 11)
        self.assertIsNone(self.calc.execute("SELECT envoye_le FROM rejeu_ao WHERE ref_annonce = 'REJEU-03'").fetchone()[0])

    def test_rien_a_envoyer(self):
        self.calc.execute("DELETE FROM rejeu_ao")
        with self.assertRaises(rejouer_ao.ErreurEnvoi):
            rejouer_ao.envoyer(self.calc, "http://127.0.0.1:8000", self.JETON, envoi=self.faux())


class Commande(unittest.TestCase):
    def test_options_avant_ou_apres_la_commande(self):
        for argv in (["--url", "http://127.0.0.1:9", "envoyer"], ["envoyer", "--url", "http://127.0.0.1:9"]):
            with mock.patch.object(rejouer_ao, "commande_envoyer", return_value=0) as cmd:
                self.assertEqual(rejouer_ao.main(argv), 0)
            self.assertEqual(cmd.call_args[0][0].url, "http://127.0.0.1:9", argv)
        with mock.patch.object(rejouer_ao, "commande_envoyer", return_value=0) as cmd:
            rejouer_ao.main(["envoyer"])
        self.assertEqual((cmd.call_args[0][0].n, cmd.call_args[0][0].allotis, cmd.call_args[0][0].distant),
                         (20, 3, False))

    def test_sans_commande_on_choisit_puis_on_envoie(self):
        with mock.patch.object(rejouer_ao, "commande_choisir", return_value=0) as c, \
             mock.patch.object(rejouer_ao, "commande_envoyer", return_value=0) as e:
            self.assertEqual(rejouer_ao.main([]), 0)
        self.assertTrue(c.called and e.called)


if __name__ == "__main__":
    unittest.main()
