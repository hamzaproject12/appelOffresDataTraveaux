"""Étape A2 : présence chez l'acheteur et calibration par segment, historique du nombre de concurrents, groupes
en étoile, prévisions figées (suivi.py), planificateur, page d'analyse d'un AO alloti, REJEU-R (données simulées)."""
import json
import random
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import planificateur
from copilote import bases, config, groupes_ao, mesures_probables, probables, rejeu, rejeu_recent, rejouer_ao, suivi
from copilote import similarite
from tests import outils
from tests.test_probables import Api, candidat, unite
from tests.test_rejeu import base_de_rejeu

ICI = Path(__file__).resolve().parent.parent
PISTE = "Travaux de construction de pistes rurales reliant les douars"
ECOLE = "Construction de l'école primaire du douar"


# --------------------------------------------------------------------------- 1. présence chez l'acheteur, segments

def memoire_acheteur():
    """Chez COMMUNE DE TEST : 5 marchés de pistes (habituee présente sur 4, rare sur 1) et 3 écoles."""
    unites, fiches = [], {}
    for k in range(5):
        offres = [("rare", 101)] if k == 0 else []
        offres += [("habituee", 99), (f"x{k}", 100)] if k < 4 else [(f"x{k}", 100)]
        u = unite(f"p{k}", f"202507{10 + k:02d}", offres, offres[0][0])
        unites.append(u)
        fiches[u.identifiant] = SimpleNamespace(quals=frozenset(), texte=PISTE)
    for k in range(3):
        u = unite(f"e{k}", f"202508{10 + k:02d}", [("habituee", 99), (f"y{k}", 100)], f"y{k}")
        unites.append(u)
        fiches[u.identifiant] = SimpleNamespace(quals=frozenset(), texte=ECOLE)
    mem = probables.Memoire(unites, fiches)
    fam = mem.famille[0]
    ctx = probables.Contexte(n=len(mem.unites), jour=similarite.jour_de("2025-09-01"),
                             acheteur_cles=frozenset({bases.cle_acheteur("COMMUNE DE TEST")}), codes=frozenset(),
                             famille=fam)
    return mem, ctx


class PresenceChezLAcheteur(unittest.TestCase):
    def test_variables_et_segment(self):
        mem, ctx = memoire_acheteur()
        self.assertIsNotNone(ctx.famille)
        sig = {s["cle"]: s for s in probables.signaux(mem, ctx)}
        h, r = sig["habituee"], sig["rare"]
        self.assertEqual((h["brut"]["acheteur_famille"], h["brut"]["acheteur_famille_total"]), (4, 5))
        self.assertAlmostEqual(h["x"]["acheteur_famille"], 4 / (5 + 2))
        self.assertEqual((h["brut"]["acheteur_24"], h["brut"]["acheteur_24_total"]), (7, 8))
        self.assertAlmostEqual(h["x"]["acheteur_24"], 7 / 10)
        self.assertEqual(probables.segment(h["x"]), "habituee")
        self.assertEqual(probables.segment(r["x"]), "autre")             # une seule présence

    def test_la_fenetre_de_24_mois_est_respectee(self):
        mem, ctx = memoire_acheteur()
        ctx.jour = similarite.jour_de("2027-09-01")                      # plus de deux ans après
        sig = {s["cle"]: s for s in probables.signaux(mem, ctx)}
        self.assertEqual(sig["habituee"]["brut"]["acheteur_famille"], 0)
        self.assertEqual(probables.segment(sig["habituee"]["x"]), "autre")

    def test_calibration_propre_au_segment(self):
        coefs = {**probables.DEFAUT, "coefficients": dict(probables.DEFAUT["coefficients"]),
                 "calibration": {"x": [0.0001, 0.9999], "y": [0.0002, 0.5]},
                 "calibration_segments": {"habituee": {"x": [0.0001, 0.9999], "y": [0.5, 0.99]},
                                          "autre": {"x": [0.0001, 0.9999], "y": [0.0001, 0.1]}}}
        x = {v: 0.0 for v in probables.VARIABLES}
        p_autre = probables.probabilite(x, coefs)[0]
        p_hab = probables.probabilite({**x, "acheteur_famille_n": 1.2}, coefs)[0]   # log(1 + 2) ≈ 1,0986
        self.assertLess(p_autre, 0.1)
        self.assertGreater(p_hab, 0.5)

    def test_reglages_propres_a_la_version(self):
        mem, ctx = memoire_acheteur()
        base = {**probables.DEFAUT, "coefficients": dict(probables.DEFAUT["coefficients"])}
        self.assertEqual(probables.analyser(mem, ctx, base)["reglages"]["seuil_allies"], probables.REGLAGES["seuil_allies"])
        a2 = probables.analyser(mem, ctx, {**base, "reglages": {"seuil_allies": 0.07}})
        self.assertEqual(a2["reglages"]["seuil_allies"], 0.07)
        self.assertEqual(probables.charger_coefficients()["reglages"]["seuil_allies"], 0.07)
        self.assertNotIn("reglages", probables.charger_coefficients(probables.FICHIER_A))

    def test_l_ancienne_version_ignore_les_nouvelles_variables(self):
        a = probables.charger_coefficients(probables.FICHIER_A)
        self.assertEqual((a["version"], a["groupes"]), ("A", "union"))
        for v in ("acheteur_famille", "acheteur_famille_n", "acheteur_24"):
            self.assertEqual(a["coefficients"][v], 0.0)
        self.assertNotIn("calibration_segments", a)
        self.assertNotIn("historique", a)


# --------------------------------------------------------------------------- 2. nombre de concurrents

class HistoriqueDuNombre(unittest.TestCase):
    PS = [0.5, 0.4, 0.3, 0.2, 0.1]

    def test_esperance_exacte(self):
        for s, dlt in ((0.0, 0.0), (0.7, 0.0), (0.4, 1.1), (0.0, -0.8)):
            d = probables.distribution(self.PS, 1.5, s, dlt)
            self.assertAlmostEqual(sum(k * v for k, v in enumerate(d)), probables.esperance(self.PS, 1.5, s, dlt), places=6)

    def test_sans_historique_rien_ne_change(self):
        h = probables.corriger_par_historique(self.PS, 1.0, 0.7, [], {"k": 4, "dispersion": 0.3})
        self.assertEqual((h["decalage"], h["dispersion"], h["poids"]), (0.0, 0.7, 0.0))

    def test_l_historique_tire_l_esperance_et_resserre(self):
        nombres = [20, 22, 18, 21, 19, 20, 23, 20]
        h = probables.corriger_par_historique(self.PS, 1.0, 0.7, nombres, {"k": 4, "dispersion": 0.3})
        self.assertAlmostEqual(h["poids"], 8 / 12)
        self.assertGreater(h["decalage"], 0)
        self.assertLess(h["dispersion"], 0.7)
        self.assertAlmostEqual(probables.esperance(self.PS, 1.0, h["dispersion"], h["decalage"]), h["cible"], places=4)
        m0 = probables.esperance(self.PS, 1.0, 0.7)
        self.assertGreater(h["cible"], m0)
        self.assertLess(h["cible"], 20)

    def test_historique_de_l_acheteur_par_famille_et_coupure(self):
        mem, ctx = memoire_acheteur()
        self.assertEqual(probables.historique_acheteur(mem, ctx), [3, 2, 2, 2, 1])     # les 5 pistes
        ctx.n = 2
        self.assertEqual(probables.historique_acheteur(mem, ctx), [3, 2])

    def test_copie_numpy_de_l_ajustement(self):
        rng = random.Random(5)
        ps = [rng.random() ** 2 for _ in range(40)]
        a = probables.distribution(ps, 2.0, 0.6, 0.4)
        b = mesures_probables.distribution_np(ps, 2.0, 0.6, 0.4)
        for x, y in zip(a, b):
            self.assertAlmostEqual(x, y, places=8)


# --------------------------------------------------------------------------- 3. groupes en étoile

def ligne(cle, lecture="accompagnatrice", B=0.5, C=0.3, v=4):
    return {"cle": cle, "lecture": lecture, "B": B, "C": C, "A": 0.5, "rapport": 2.0, "communes": 6,
            "victoires_x_avec": v, "part_x_sur_y": 0.2}


class GroupesEnEtoile(unittest.TestCase):
    """Le calcul « autour d'une société » est remplacé par une table : on teste la règle des groupes seule."""

    def setUp(self):
        self.mem = probables.Memoire([], {})
        self.r = dict(probables.REGLAGES)

    def former(self, autour: dict, tous: list, union=False):
        def faux(mem, n, cle, taille=9):
            return {"groupe": [cle] + [l["cle"] for l in autour.get(cle, [])], "lignes": autour.get(cle, [])}
        with mock.patch.object(groupes_ao, "autour", faux):
            f = groupes_ao.former_union if union else groupes_ao.former
            return f(self.mem, 0, tous, self.r)

    @staticmethod
    def cand(cle, p, victoires=0, presences=0, dans_liste=True):
        c = candidat(cle, p, dans_liste)
        c["brut"] = {"victoires_proches": victoires, "proches": presences}
        return c

    def test_pas_de_chainage(self):
        """a—b et b—c : l'union-find mettait a, b, c ensemble ; en étoile, b (une seule alliée dans la liste,
        a) accompagne a, et c n'est pas dans le groupe de a."""
        autour = {"a": [ligne("b")], "b": [ligne("a"), ligne("c", C=0.1)]}
        tous = [self.cand("a", 0.6, 5, 10), self.cand("b", 0.4, 1, 10), self.cand("c", 0.05, dans_liste=False)]
        g = self.former(autour, tous)
        self.assertEqual([gr["membres"] for gr in g["groupes"]], [["a", "b"]])
        u = self.former(autour, [self.cand("a", 0.6, 5, 10), self.cand("b", 0.4, 1, 10), self.cand("c", 0.2)],
                        union=True)
        self.assertEqual(sorted(u["groupes"][0]["membres"]), ["a", "b", "c"])

    def test_chef_de_file_probabilite_fois_taux_de_victoire(self):
        autour = {"a": [ligne("b")], "b": [ligne("a")]}
        tous = [self.cand("a", 0.6, 0, 20), self.cand("b", 0.4, 10, 20)]   # 0,6 × 0,01 contre 0,4 × 0,47
        g = self.former(autour, tous)
        self.assertEqual(g["groupes"][0]["chef"], "b")
        self.assertEqual(g["groupes"][0]["membres"], ["b", "a"])
        sc = g["groupes"][0]["score_chef"]
        self.assertAlmostEqual(sc["taux_victoire"], 10.25 / 22)

    def test_un_chef_probable_n_est_pas_absorbe(self):
        """b est l'alliée de a, mais b a elle-même 2 alliées dans la liste (c, d) : elle garde son groupe ;
        a et b « travaillent aussi » l'une avec l'autre."""
        autour = {"a": [ligne("b"), ligne("e")], "b": [ligne("a"), ligne("c"), ligne("d")]}
        tous = [self.cand("a", 0.7, 6, 10), self.cand("b", 0.5, 2, 10), self.cand("c", 0.3), self.cand("d", 0.3),
                self.cand("e", 0.2)]
        g = self.former(autour, tous)
        par_chef = {gr["chef"]: gr for gr in g["groupes"]}
        self.assertEqual(set(par_chef), {"a", "b"})
        self.assertEqual(par_chef["a"]["membres"], ["a", "e"])
        self.assertEqual(sorted(par_chef["b"]["membres"]), ["b", "c", "d"])
        self.assertEqual([t["cle"] for t in par_chef["a"]["travaille_avec"]], ["b"])
        self.assertEqual([t["cle"] for t in par_chef["b"]["travaille_avec"]], ["a"])
        self.assertEqual([t["groupe"] for t in par_chef["a"]["travaille_avec"]], [par_chef["b"]["numero"]])

    def test_alliee_d_un_chef_avec_une_seule_alliee_est_absorbee(self):
        autour = {"a": [ligne("b"), ligne("e")], "b": [ligne("a"), ligne("c")]}      # c hors liste, C faible
        tous = [self.cand("a", 0.7, 6, 10), self.cand("b", 0.5, 2, 10), self.cand("e", 0.2),
                self.cand("c", 0.02, dans_liste=False)]
        c = tous[-1]
        c["dans_liste"] = False
        autour["b"][1]["C"] = 0.1
        g = self.former(autour, tous)
        self.assertEqual([gr["membres"] for gr in g["groupes"]], [["a", "b", "e"]])

    def test_accompagne_aussi(self):
        """g accompagne a et h : elle va sous le chef le plus probable (a) ; le groupe de h la rappelle."""
        autour = {"a": [ligne("g"), ligne("x")], "h": [ligne("g"), ligne("y")]}
        tous = [self.cand("a", 0.8, 5, 10), self.cand("h", 0.5, 5, 10), self.cand("g", 0.3), self.cand("x", 0.2),
                self.cand("y", 0.2)]
        g = self.former(autour, tous)
        par_chef = {gr["chef"]: gr for gr in g["groupes"]}
        self.assertIn("g", par_chef["a"]["membres"])
        self.assertNotIn("g", par_chef["h"]["membres"])
        self.assertEqual([(x["cle"], x["chef"]) for x in par_chef["h"]["aussi"]], [("g", "a")])
        self.assertEqual(par_chef["h"]["aussi"][0]["groupe"], par_chef["a"]["numero"])
        self.assertEqual(tous[2]["accompagne_aussi"], ["h"])
        membres = [m for gr in g["groupes"] for m in gr["membres"]]
        self.assertEqual(len(membres), len(set(membres)))

    def test_association_et_individuelles(self):
        autour = {"a": [ligne("z", lecture="noyau", B=0.6, C=0.2)]}
        tous = [self.cand("a", 0.5, 2, 10), self.cand("solo", 0.3)]
        g = self.former(autour, tous)
        self.assertEqual([c["cle"] for c in g["ajoutees"]], ["z"])
        self.assertAlmostEqual(g["ajoutees"][0]["p"], 0.3)
        self.assertEqual(g["individuelles"], ["solo"])

    def test_sans_association_l_alliee_hors_liste_n_entre_nulle_part(self):
        """reglages.json (03/11/2026) : association = false. L'alliée hors liste n'est ni ajoutée ni dans un
        groupe ; le chef sans autre alliée redevient individuelle. Même chose en version A."""
        self.assertIs(probables.reglages_fichier()["association"], False)
        self.r["association"] = False
        autour = {"a": [ligne("z", lecture="noyau", B=0.6, C=0.2), ligne("b")]}
        for union in (False, True):
            tous = [self.cand("a", 0.5, 2, 10), self.cand("b", 0.2), self.cand("solo", 0.3)]
            g = self.former(autour, tous, union=union)
            self.assertEqual(g["ajoutees"], [])
            self.assertEqual([sorted(x["membres"]) for x in g["groupes"]], [["a", "b"]])
            self.assertFalse(any(c["association"] for c in tous))
        tous = [self.cand("a", 0.5, 2, 10), self.cand("solo", 0.3)]
        g = self.former({"a": [ligne("z", lecture="noyau", B=0.6, C=0.2)]}, tous)
        self.assertEqual((g["groupes"], g["ajoutees"]), ([], []))
        self.assertEqual(sorted(g["individuelles"]), ["a", "solo"])

    def test_reglages_du_fichier_passent_avant_les_defauts(self):
        mem, ctx = memoire_acheteur()
        base = {**probables.DEFAUT, "coefficients": dict(probables.DEFAUT["coefficients"])}
        r = probables.analyser(mem, ctx, base)["reglages"]
        self.assertIs(r["association"], False)
        self.assertEqual(r["seuil_groupe_affiche"], 0.15)


# --------------------------------------------------------------------------- 4. prévisions figées, page, lots

class Suivi(unittest.TestCase):
    setUp, tearDown, client = Api.setUp, Api.tearDown, Api.client

    def preparer_reel(self):
        """L'AO 777, expiré depuis la veille, et son PV « c777 » dans l'historique (3 offres)."""
        db = sqlite3.connect(self.d / "ao_ouverts.db")
        hier = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00")
        db.execute("INSERT INTO appels_ouverts (ref_annonce, reference, objet, acheteur, cle_acheteur, estimation,"
                   " date_limite) VALUES ('777', '12/2026', ?, 'COMMUNE DE LIEU1', ?, 5e6, ?)",
                   ("Travaux de construction de pistes rurales reliant les douars de la commune LIEU1",
                    bases.cle_acheteur("COMMUNE DE LIEU1"), hier))
        db.commit()
        db.close()

    def ajouter_pv(self):
        db = sqlite3.connect(self.d / "pv_travaux.db")
        outils.marche(db, "c777", 5e6, [("soc1", 4.6e6), ("soc2", 4.8e6), ("nouvelle", 4.9e6)], gagnant="soc1",
                      date="20261103", acheteur="COMMUNE DE LIEU1", objet="Pistes")
        db.commit()
        db.close()
        similarite.oublier()
        probables.oublier()

    def test_une_copie_par_jour_puis_prevu_reel(self):
        self.preparer_reel()
        self.assertEqual(self.alice.get("/api/copilote/prevision/777").status_code, 404)
        r = self.alice.get("/api/copilote/analyse/777")                  # AO expiré : l'analyse marche encore
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["ao"]["mode"], "reel")
        self.alice.get("/api/copilote/analyse/777")
        with closing(bases.retours()) as ret:
            copies = suivi.lire(ret, "777", None)
        self.assertEqual(len(copies), 1)
        prev = copies[0]["prevision"]
        self.assertTrue(prev["candidats"] and prev["concurrents"]["distribution"])
        self.assertEqual(copies[0]["version"], r.json()["probables"]["coefficients"]["version"])
        e = self.alice.get("/api/copilote/prevision/777").json()
        self.assertIsNone(e["reel"])
        self.ajouter_pv()
        e = self.alice.get("/api/copilote/prevision/777").json()
        self.assertEqual(e["reel"]["nb_concurrents"], 3)
        s = e["score"]
        self.assertEqual(s["gagnant"]["cle"], "soc1")
        self.assertEqual({x["cle"]: x["prevue"] for x in s["societes"]}["nouvelle"], False)
        self.assertEqual(s["nb_concurrents"], 3)

    def test_le_lendemain_une_nouvelle_copie_et_rien_pour_un_rejeu(self):
        self.preparer_reel()
        d = self.alice.get("/api/copilote/analyse/777").json()
        with closing(bases.retours()) as ret:
            demain = datetime.now(timezone.utc) + timedelta(days=1)
            self.assertEqual(suivi.figer(ret, d, demain), "enregistree")
            self.assertEqual(suivi.figer(ret, d, demain), "deja")
            self.assertEqual(len(suivi.lire(ret, "777", None)), 2)
        self.alice.get("/api/copilote/analyse/REJEU-01")
        with closing(bases.retours()) as ret:
            self.assertEqual(suivi.lire(ret, "REJEU-01", None), [])

    def test_alloti_lots_sans_appel_d_analyse_sans_lot(self):
        r = self.alice.get("/api/copilote/analyse/AO3/lots")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["lot"] for x in r.json()["lots"]], ["1", "2"])
        self.assertEqual(self.alice.get("/api/copilote/analyse/AO1/lots").json()["lots"], [])
        self.assertEqual(self.alice.get("/api/copilote/analyse/INCONNU/lots").status_code, 404)
        d = self.alice.get("/api/copilote/analyse/AO3", params={"lot": "2"}).json()
        self.assertEqual([x["lot"] for x in d["ao"]["lots"]], ["1", "2"])
        with closing(bases.retours()) as ret:
            self.assertEqual([c["lot"] for c in suivi.lire(ret, "AO3", "2")], ["2"])

    def test_la_page_n_appelle_l_analyse_qu_avec_un_lot(self):
        html = (ICI / "statique" / "copilote_analyse.html").read_text(encoding="utf-8")
        appels = [l for l in html.splitlines() if "/api/copilote/analyse/" in l]
        self.assertTrue(all("${q}" in l or "/lots" in l for l in appels), appels)
        self.assertNotIn("await lire(`/api/copilote/analyse/${encodeURIComponent(REF)}`)", html)
        self.assertIn("conc.decalage", html)
        ao = (ICI / "statique" / "ao_ouverts.html").read_text(encoding="utf-8")
        self.assertIn("'?lot=' + encodeURIComponent(a.lots[0].lot)", ao)
        self.assertIn("CLIENT_TEST", ao)


def closing(x):
    from contextlib import closing as c
    return c(x)


# --------------------------------------------------------------------------- 5. planificateur

class Planificateur(unittest.TestCase):
    def essais(self, codes, essais=6):
        suite, dodos = iter(codes), []
        with mock.patch.object(planificateur, "dire"):
            r = planificateur.essais_du_jour(lambda: next(suite), dodos.append, essais=essais, intervalle=3600)
        return r, dodos

    def test_reussite_au_troisieme_essai(self):
        self.assertEqual(self.essais([3, 3, 0]), ((0, 3), [3600, 3600]))

    def test_six_essais_au_plus(self):
        self.assertEqual(self.essais([3] * 10), ((3, 6), [3600] * 5))

    def test_un_autre_echec_ne_relance_pas(self):
        self.assertEqual(self.essais([1, 0]), ((1, 1), []))

    def test_maj_quotidienne_renvoie_3_quand_la_sonde_echoue(self):
        texte = (ICI / "maj_quotidienne.py").read_text(encoding="utf-8")
        self.assertIn("SONDE_EN_ECHEC = 3", texte)
        self.assertIn("sys.exit(SONDE_EN_ECHEC)", texte)
        self.assertEqual(planificateur.SONDE_EN_ECHEC, 3)


# --------------------------------------------------------------------------- 6. REJEU-R, sur une base simulée

class RejeuRecent(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.d = Path(self.dossier.name)
        base_de_rejeu(self.d / "pv.db")
        self.hist = sqlite3.connect(self.d / "pv.db")
        self.hist.row_factory = sqlite3.Row
        self.calc = sqlite3.connect(self.d / "calc.db")
        self.calc.row_factory = sqlite3.Row
        rejouer_ao.enregistrer(self.calc, rejouer_ao.choisir(rejouer_ao.candidats(self.hist), 4, 1))  # REJEU-01..04

    def tearDown(self):
        self.hist.close()
        self.calc.close()
        self.dossier.cleanup()

    def test_coefficients_appris_avant(self):
        self.assertEqual(rejeu_recent.verifier_coefficients("2026-10-01", {"A": {"ajuste_sur": {"annee": "2025"}},
                                                                            "A2": {"appris_jusqu_au": "2025-12-31"}}),
                         {"A": "2025-12-31", "A2": "2025-12-31"})
        with self.assertRaises(rejeu_recent.Arret):
            rejeu_recent.verifier_coefficients("2026-10-01", {"A2": {"appris_jusqu_au": "2026-10-15"}})
        with self.assertRaises(rejeu_recent.Arret):
            rejeu_recent.verifier_coefficients("2026-10-01", {"A2": {}})
        rejeu_recent.verifier_coefficients("2026-10-01", mesures_probables.versions())       # les vrais fichiers

    def test_choix_recent_varie_avec_allotis_sans_toucher_aux_autres_rejeux(self):
        cands = rejeu_recent.candidats_recents(self.hist, "2026-05-01", aujourdhui=date(2026, 11, 2))
        self.assertTrue(cands and all(c.date >= "20260501" for c in cands))
        choisis = rejeu_recent.choisir(cands, 6, 2)
        self.assertGreaterEqual(sum(1 for c in choisis if c.nb_lots), 2)
        self.assertEqual(len({c.cle_acheteur for c in choisis}), len(choisis))
        refs = rejeu_recent.enregistrer(self.calc, choisis)
        self.assertEqual(refs[0], "REJEU-R-01")
        tous = [r[0] for r in self.calc.execute("SELECT ref_annonce FROM rejeu_ao ORDER BY ref_annonce")]
        self.assertEqual(sum(1 for r in tous if r.startswith("REJEU-R-")), len(choisis))
        self.assertEqual(sum(1 for r in tous if not r.startswith("REJEU-R-")), 4)
        rejeu_recent.enregistrer(self.calc, choisis[:2], renouveler=True)
        self.assertEqual(rejeu_recent.deja_choisis(self.calc), ["REJEU-R-01", "REJEU-R-02"])
        self.assertEqual(self.calc.execute("SELECT COUNT(*) FROM rejeu_ao").fetchone()[0], 6)
        # la coupure est la veille de l'ouverture, et l'annonce ne dit rien du résultat
        c = choisis[0]
        self.assertEqual(rejeu.limite_pour("REJEU-R-01", self.calc), rejeu.veille(c.date).replace("-", ""))
        a = json.loads(self.calc.execute("SELECT annonce FROM rejeu_ao WHERE ref_annonce = 'REJEU-R-01'").fetchone()[0])
        self.assertNotIn(c.ref, json.dumps(a))
        self.assertEqual(a["clients"], ["Test"])

    def test_base_ao_en_memoire_et_envoi_local_seulement(self):
        rejeu_recent.enregistrer(self.calc, rejeu_recent.choisir(
            rejeu_recent.candidats_recents(self.hist, "2026-05-01", date(2026, 11, 2)), 4, 1))
        ao = rejeu_recent.ao_en_memoire(self.calc)()
        refs = [r[0] for r in ao.execute("SELECT ref_annonce FROM appels_ouverts ORDER BY 1")]
        self.assertEqual(refs, rejeu_recent.deja_choisis(self.calc))
        self.assertTrue(ao.execute("SELECT COUNT(*) FROM appels_lots").fetchone()[0] >= 2)
        envoyes = []
        r = rejeu_recent.envoyer(self.calc, "http://127.0.0.1:8000", "jeton",
                                 lambda url, j, corps: (envoyes.append(corps) or (200, {"recus": 4, "rejetes": []})))
        self.assertEqual(r["recus"], 4)
        self.assertTrue(all(a["ref_annonce"].startswith("REJEU-R-") for a in envoyes[0]["appels_ouverts"]))
        with self.assertRaises(rejouer_ao.ErreurEnvoi):
            rejeu_recent.envoyer(self.calc, "https://site.up.railway.app", "jeton", lambda *a: (200, {}))

    def test_portail_injoignable_arrete_tout(self):
        args = SimpleNamespace(depuis="2026-10-01", sans_maj=False, renouveler=False, n=30, allotis=3, graine=0,
                               sans_envoi=True, url="http://127.0.0.1:8000")
        appels = []
        with mock.patch.object(bases, "historique", lambda: _ro(self.d / "pv.db")):
            with self.assertRaises(rejeu_recent.Arret) as e:
                rejeu_recent.tout(args, lancer=lambda cmd, cwd=None: appels.append(cmd) or 3)
        self.assertEqual(e.exception.code, 3)
        self.assertIn("--relance", appels[0])
        self.assertIn("travaux", appels[0])

    def test_compter_marches(self):
        n = rejeu_recent.compter_marches(self.hist, "2026-05-01")
        self.assertGreater(n["marches"], 0)
        self.assertLessEqual(n["avec_pv"], n["marches"])

    def test_rapport(self):
        res = {"versions": {v: {"resume": {"n": 3, "rappel_top15": 0.4, "gagnant_top30": 0.5, "dans_fourchette": 0.8,
                                           "ecart_median": 2.0, "largeur_fourchette": 9.0, "membres_retrouves": 0.6}}
                            for v in ("A", "A2")}, "erreurs": []}
        md = rejeu_recent.rapport_md(res, {"depuis": "2026-10-01", "coefficients": {"A": "2025-12-31"}, "choisis": 3,
                                           "allotis": 1, "maj": {"avant": {"avec_pv": 0}, "apres": {"avec_pv": 40}}})
        self.assertIn("| vrai gagnant dans le top 30 | 50,0 % | 50,0 % |", md)
        self.assertIn("40 nouveaux", md)

    def test_rapport_avec_la_simulation(self):
        res = {"versions": {"A2": {"resume": {"n": 3}}}, "erreurs": [],
               "simulation": {"resume": {"avec_reference": 3, "attribues": 2, "dans80": 0.8, "mae": 0.02,
                                         "mae_etape4": 0.025, "erreur_mediane": 0.015, "erreur_mediane_etape4": 0.02,
                                         "calibration": [{"de": 0.2, "a": 0.3, "n": 5, "annoncee": 0.25, "reelle": 0.2,
                                                          "ecart_points": 5.0}],
                                         "zone_contre_fixes": {"1-2": {"n": 2, "zone": 0.5, "meilleur_fixe": 0.95,
                                                                       "victoires_meilleur_fixe": 0.5}}},
                              "erreurs": []}}
        md = rejeu_recent.rapport_md(res, {"depuis": "2026-10-01", "coefficients": {}, "choisis": 3, "allotis": 0})
        self.assertIn("## Étape C", md)
        self.assertIn("| vrai prix de référence dans l'intervalle 80 % | 80,0 % | 70 à 90 % |", md)
        self.assertIn("| 1-2 | 2 | 50,0 % | 0,95 | 50,0 % |", md)
        self.assertIn("| 20–30 % | 5 | 25,0 % | 20,0 % | +5,0 pt |", md)


def _ro(chemin):
    db = sqlite3.connect(chemin)
    db.row_factory = sqlite3.Row
    return db


if __name__ == "__main__":
    unittest.main()
