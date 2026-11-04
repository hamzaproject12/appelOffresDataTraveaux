"""Étape D : éligibilité, décision GO / À ÉTUDIER / NO-GO (deux copies identiques), profil de l'entreprise."""
import json
import math
import shutil
import subprocess
import unittest
from pathlib import Path

from copilote import decision, eligibilite, mesures_decision, simulateur

ICI = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
CAS = json.loads((ICI / "tests" / "cas_decision.json").read_text(encoding="utf-8"))["cas"]
REG = decision.charger_reglages()
TEXTES = decision.charger_textes()
XS = [(1600 + j) / 2000 for j in range(801)]


def courbe(c: dict) -> list[float]:
    """Une chance de gagner décroissante avec le prix, déterministe : logistique de centre m, d'échelle s."""
    return [max(0.0, min(1.0, c["pmax"] / (1 + math.exp((x - c["m"]) / c["s"])) + c["bruit"] * math.sin(0.37 * j)))
            for j, x in enumerate(XS)]


def entree(c: dict) -> dict:
    return {**c, "xs": XS, "chance": courbe(c["courbe"]), "reg": REG, "textes": TEXTES}


def calculer(c: dict) -> dict:
    """Ce que fait lancer_decision.js, en Python."""
    exig = eligibilite.exigences_de([tuple(l) for l in c.get("lignes_ao") or []])
    partenaires = c.get("partenaires") or []
    groupement = [p for p in partenaires if p["id"] == c.get("scenario_partenaire")]
    elig = eligibilite.verifier(exig, c.get("quals") or {}, groupement) if c.get("lignes_ao") else None
    partenaire = next((p for p in partenaires if p["id"] == c.get("scenario_partenaire")), None)
    cout = decision.cout_du_scenario(c["cout_dh"], c["estimation"], partenaire)
    ev = decision.evaluer(XS, courbe(c["courbe"]), c["estimation"], cout, c["marge_min"], REG, (c.get("signaux") or {}).get("prevu"))
    dec = decision.decider(ev, elig, c.get("signaux"), REG)
    return {"exigences": exig, "elig": elig, "apports": eligibilite.apports(exig, c.get("quals") or {}, partenaires),
            "cout": cout, "ev": ev, "dec": dec, "phrases": [decision.phrase(r, TEXTES) for r in dec["raisons"]]}


def proche(t, a, b, ou):
    if isinstance(a, dict) and isinstance(b, dict):
        t.assertEqual(set(a), set(b), ou)
        for k in a:
            proche(t, a[k], b[k], f"{ou}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        t.assertEqual(len(a), len(b), ou)
        for k, (x, y) in enumerate(zip(a, b)):
            proche(t, x, y, f"{ou}[{k}]")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        t.assertTrue(abs(a - b) <= 1e-12 * max(1.0, abs(a)), f"{ou} : {a} != {b}")
    else:
        t.assertEqual(a, b, ou)


@unittest.skipUnless(NODE, "node absent")
class DeuxCopies(unittest.TestCase):
    def test_python_et_javascript_donnent_le_meme_resultat(self):
        sortie = subprocess.run([NODE, str(ICI / "tests" / "lancer_decision.js")],
                                input=json.dumps({"cas": [entree(c) for c in CAS]}), capture_output=True, text=True,
                                encoding="utf-8", check=True).stdout
        js = json.loads(sortie)["resultats"]
        self.assertEqual(len(js), len(CAS))
        for c, rj in zip(CAS, js):
            rp = json.loads(json.dumps(calculer(c)))
            proche(self, rp, rj, c["nom"])

    def test_les_cas_couvrent_les_trois_decisions(self):
        vues = {calculer(c)["dec"]["decision"] for c in CAS}
        self.assertEqual(vues, {decision.GO, decision.ETUDIER, decision.NOGO})


class Eligibilite(unittest.TestCase):
    def verifier(self, lignes, quals, partenaires=None):
        return eligibilite.verifier(eligibilite.exigences_de(lignes), quals, partenaires)

    def test_ordre_des_classes_s_est_la_plus_haute(self):
        r = eligibilite.rang_classe
        self.assertEqual([r("S"), r("1"), r("5"), r("Classe 3"), r(3)], [0, 1, 5, 3, 3])
        self.assertIsNone(r("6"))
        self.assertIsNone(r(None))
        # Une entreprise de classe 3 répond aux classes 3, 4, 5, pas à la 2 ni à la 1 ni à S.
        for exigee, attendu in (("5", True), ("4", True), ("3", True), ("2", False), ("1", False), ("S", False)):
            self.assertEqual(eligibilite.suffit(r("3"), r(exigee)), attendu, exigee)
        self.assertTrue(eligibilite.suffit(r("S"), r("1")))

    def test_classe_insuffisante_d_un_cran_est_non_eligible(self):
        r = self.verifier([("B.1", "3")], {"B.1": "4"})
        self.assertEqual(r["statut"], "non_eligible")
        self.assertEqual(r["lignes"][0]["etat"], "classe")

    def test_classe_suffisante_ou_superieure(self):
        self.assertEqual(self.verifier([("B.1", "3")], {"B.1": "3"})["statut"], "eligible")
        self.assertEqual(self.verifier([("B.1", "3")], {"B.1": "1"})["statut"], "eligible")

    def test_qualification_absente(self):
        r = self.verifier([("B.1", "3"), ("B.3", "3")], {"B.1": "3"})
        self.assertEqual((r["statut"], r["manque"]), ("non_eligible", 1))

    def test_toutes_les_qualifications_sont_exigees(self):
        self.assertEqual(self.verifier([("B.1", "3"), ("B.3", "3")], {"B.1": "2", "B.3": "4"})["statut"], "non_eligible")

    def test_alternative_ou_est_a_etudier(self):
        r = self.verifier([("B.1 ou B.3", "4")], {"B.1": "3"})
        self.assertEqual(r["statut"], "a_etudier")
        # même si nous n'avons aucune des deux : l'alternative n'est pas un refus, c'est une question
        self.assertEqual(self.verifier([("B.1 ou B.3", "4")], {})["statut"], "a_etudier")

    def test_qualification_non_reconnue_est_a_etudier(self):
        r = self.verifier([("4.1.Travaux de séguia et khettara", "4")], {"B.1": "1"})
        self.assertEqual(r["statut"], "a_etudier")
        self.assertEqual(r["lignes"][0]["etat"], "inconnu")

    def test_seul_un_groupement_rend_eligible(self):
        ex = [("B.1", "3"), ("B.3", "3")]
        groupement = {"nom": "G", "type": "groupement", "qualifs": [{"code": "B.3", "classe": "3"}]}
        sous_traitant = {"nom": "ST", "type": "sous_traitant", "qualifs": [{"code": "B.3", "classe": "1"}]}
        fournisseur = {"nom": "F", "type": "fournisseur", "qualifs": [{"code": "B.3", "classe": "1"}]}
        nous = {"B.1": "3"}
        self.assertEqual(self.verifier(ex, nous, [groupement])["statut"], "eligible")
        self.assertEqual(self.verifier(ex, nous, [sous_traitant])["statut"], "non_eligible")
        self.assertEqual(self.verifier(ex, nous, [fournisseur])["statut"], "non_eligible")
        # le groupement doit avoir la classe, pas seulement le code
        faible = {"nom": "G", "type": "groupement", "qualifs": [{"code": "B.3", "classe": "4"}]}
        self.assertEqual(self.verifier(ex, nous, [faible])["statut"], "non_eligible")
        apports = eligibilite.apports(eligibilite.exigences_de(ex), nous, [groupement, sous_traitant])
        self.assertEqual(apports, [{"code": "B.3", "classe": "3", "partenaires": ["G"]}])

    def test_lecture_des_codes(self):
        self.assertEqual(eligibilite.codes_de("B.1- Travaux de terrassements"), ["B.1"])
        self.assertEqual(eligibilite.codes_de("B.1 ou b.3"), ["B.1", "B.3"])
        self.assertEqual(eligibilite.codes_de("4.1.Travaux de séguia"), [])
        self.assertEqual(eligibilite.codes_de(None), [])


class Decision(unittest.TestCase):
    def evaluer(self, c, cout, marge=0.05):
        return decision.evaluer(XS, courbe(c), 10_000_000, cout, marge, REG)

    def test_la_plage_commence_au_plus_grand_du_plancher_et_de_la_marge(self):
        c = {"m": 1.0, "s": 0.05, "pmax": 0.6, "bruit": 0.0}
        ev = self.evaluer(c, 7_000_000)                       # 0,70 × 1,05 = 0,735 < 0,82
        self.assertAlmostEqual(ev["xmin"], 0.82)
        ev = self.evaluer(c, 9_000_000)                       # 0,90 × 1,05 = 0,945
        self.assertAlmostEqual(ev["xmin"], 0.945)
        self.assertTrue(ev["reco"]["x"] >= 0.945 - 1e-9)
        self.assertTrue(ev["reco"]["marge_pct"] >= 0.05 - 1e-9)
        self.assertLessEqual(ev["reco"]["x"], 1.2)

    def test_jamais_sous_le_plancher_ni_au_dessus_du_plafond(self):
        for cout in (5e6, 8e6, 9.5e6, 11e6):
            ev = self.evaluer({"m": 1.1, "s": 0.2, "pmax": 0.9, "bruit": 0.0}, cout)
            if ev["reco"]:
                self.assertTrue(0.82 - 1e-9 <= ev["reco"]["x"] <= 1.2 + 1e-9)
            if ev["zone"]:
                self.assertTrue(ev["zone"]["de"] >= 0.82 - 1e-9 and ev["zone"]["a"] <= 1.2 + 1e-9)

    def test_le_prix_recommande_maximise_le_gain_espere_pas_la_chance(self):
        c = {"m": 1.0, "s": 0.06, "pmax": 0.7, "bruit": 0.0}
        ev = self.evaluer(c, 8_000_000)
        meilleur_chance = ev["meilleure_chance"]["x"]
        self.assertAlmostEqual(meilleur_chance, 0.82, places=2)           # la chance décroît : le plancher
        self.assertGreater(ev["reco"]["x"], meilleur_chance + 0.05)       # le gain espéré pousse plus haut
        self.assertGreater(ev["reco"]["gain"], ev["tableau"][0]["gain"] * 0.99)

    def test_point_de_meilleure_chance_comme_la_zone_de_c2(self):
        c = {"m": 0.97, "s": 0.05, "pmax": 0.5, "bruit": 0.01}
        ch = courbe(c)
        z = simulateur.zone_recommandee(XS, ch, 0, 0.82)
        ev = decision.evaluer(XS, ch, 10_000_000, 8_000_000, 0.05, REG)
        self.assertAlmostEqual(ev["meilleure_chance"]["x"], z["x"])

    def test_aucun_gain_suffisant_donne_nogo_avec_la_raison(self):
        ev = self.evaluer({"m": 0.84, "s": 0.02, "pmax": 0.04, "bruit": 0.0}, 9_500_000)
        d = decision.decider(ev, None, {"prevu": 14}, REG)
        self.assertEqual(d["decision"], decision.NOGO)
        self.assertIn("aucun_gain", [r["code"] for r in d["raisons"]])

    def test_plage_vide_donne_nogo(self):
        ev = self.evaluer({"m": 1.0, "s": 0.05, "pmax": 0.6, "bruit": 0.0}, 13_000_000)
        self.assertTrue(ev["vide"])
        self.assertEqual(decision.decider(ev, None, {}, REG)["decision"], decision.NOGO)

    def test_un_plan_plein_ne_fait_jamais_un_nogo(self):
        ev = self.evaluer({"m": 0.98, "s": 0.04, "pmax": 0.7, "bruit": 0.0}, 8_500_000)
        seul = decision.decider(ev, None, {}, REG)["decision"]
        plein = decision.decider(ev, None, {"en_cours": 5, "plan_max": 5}, REG)
        self.assertEqual(seul, decision.GO)
        self.assertEqual(plein["decision"], decision.ETUDIER)

    def test_les_signaux_de_risque_ne_vont_jamais_jusqu_au_nogo(self):
        ev = self.evaluer({"m": 0.98, "s": 0.04, "pmax": 0.7, "bruit": 0.0}, 8_500_000)
        s = {"acheteur_n": 0, "identifies_part": 0.0, "infructueux": {"n": 9, "total": 10, "taux": 0.9},
             "groupes_forts": [{"numero": 1, "nom": "X", "p": 0.9}]}
        self.assertEqual(decision.decider(ev, None, s, REG)["decision"], decision.ETUDIER)

    def test_non_eligible_est_un_nogo_meme_avec_un_bon_gain(self):
        ev = self.evaluer({"m": 0.98, "s": 0.04, "pmax": 0.7, "bruit": 0.0}, 8_500_000)
        elig = eligibilite.verifier(eligibilite.exigences_de([("B.1", "2")]), {"B.1": "3"})
        self.assertEqual(decision.decider(ev, elig, {}, REG)["decision"], decision.NOGO)

    def test_delai_depasse_nogo_et_delai_court_a_etudier(self):
        ev = self.evaluer({"m": 0.98, "s": 0.04, "pmax": 0.7, "bruit": 0.0}, 8_500_000)
        self.assertEqual(decision.decider(ev, None, {"jours": -1}, REG)["decision"], decision.NOGO)
        self.assertEqual(decision.decider(ev, None, {"jours": 1}, REG)["decision"], decision.ETUDIER)
        self.assertEqual(decision.decider(ev, None, {"jours": 20}, REG)["decision"], decision.GO)

    def test_petit_marche_et_gros_marche(self):
        petit = decision.decider(self.evaluer({"m": 0.95, "s": 0.07, "pmax": 0.8, "bruit": 0.0}, 8_000_000), None,
                                 {"prevu": 2.2}, REG)
        gros = decision.decider(self.evaluer({"m": 1.02, "s": 0.05, "pmax": 0.09, "bruit": 0.0}, 8_500_000), None,
                                {"prevu": 16.4}, REG)
        self.assertIn("marche_petit", [r["code"] for r in petit["raisons"]])
        self.assertIn("marche_gros_faible", [r["code"] for r in gros["raisons"]])

    def test_un_partenaire_ne_change_que_le_cout(self):
        p = {"effet_cout": -0.02, "effet_cout_unite": "pct"}
        self.assertAlmostEqual(decision.cout_du_scenario(8_500_000, 10_000_000, p), 8_300_000)
        self.assertAlmostEqual(decision.cout_du_scenario(8_500_000, 10_000_000, {"effet_cout": 100_000, "effet_cout_unite": "dh"}),
                               8_600_000)
        self.assertEqual(decision.cout_du_scenario(8_500_000, 10_000_000, None), 8_500_000)

    def test_les_phrases_ne_contiennent_que_des_chiffres_calcules(self):
        c = CAS[0]
        r = calculer(c)
        for p in r["phrases"]:
            self.assertNotIn("{", p)
        texte = " ".join(r["phrases"])
        reco = r["ev"]["reco"]
        self.assertIn(decision._decimal(reco["x"], 3), texte)
        self.assertIn(decision.FORMATS["pct"](reco["chance"]), texte)

    def test_arrondi_des_milliemes_sans_banquier(self):
        self.assertEqual(decision.FORMATS["t3"](0.8125), "0,813")
        self.assertEqual(decision.FORMATS["dh"](1234567.5), "1 234 568")
        self.assertEqual(decision.FORMATS["pct"](0.0575), "5,8 %")

    def test_les_textes_couvrent_tous_les_codes_de_raison(self):
        codes = {r["code"] for c in CAS for r in calculer(c)["dec"]["raisons"]}
        self.assertTrue(codes <= set(TEXTES["raisons"]), codes - set(TEXTES["raisons"]))


class Backtest(unittest.TestCase):
    def lignes(self):
        """Quatre marchés fabriqués : la chance baisse avec le prix ; notre offre gagne tant que x ≤ 0,97."""
        out = []
        for k, prevu in enumerate((3.0, 3.0, 12.0, 12.0)):
            c = {"m": 0.98, "s": 0.04, "pmax": 0.7 if prevu < 5 else 0.12, "bruit": 0.0}
            out.append({"courbe": courbe(c), "gagne_grille": [x <= 0.97 for x in XS], "estimation": 1_000_000.0 * (k + 1),
                        "fixes": [x <= 0.97 for x in mesures_decision.FIXES], "nombre_prevu": prevu})
        return out

    def test_gain_compte_en_dirhams_et_seulement_les_victoires(self):
        ls = self.lignes()
        r = mesures_decision.toujours(ls, 0.95, 0.85)
        self.assertEqual((r["joues"], r["victoires"]), (4, 4))
        self.assertEqual(r["gain_dh"], round(0.10 * (1 + 2 + 3 + 4) * 1_000_000))
        r = mesures_decision.toujours(ls, 1.0, 0.85)
        self.assertEqual((r["victoires"], r["gain_dh"]), (0, 0))

    def test_la_regle_ne_joue_que_les_go_et_separe_petits_et_gros_marches(self):
        reg = decision.charger_reglages()
        r = mesures_decision.jouer_regle(self.lignes(), 0.85, reg)
        self.assertEqual(sum(r["decisions"].values()), 4)
        self.assertEqual(r["joues"], r["decisions"]["GO"])
        self.assertEqual(r["joues"], r["petits_marches"]["joues"] + r["gros_marches"]["joues"])
        self.assertTrue(all(l["prevu"] < 5 for l in [{"prevu": 3.0}]))

    def test_taux_fixe_choisi_sur_2025_respecte_plancher_et_marge(self):
        f = mesures_decision.meilleur_fixe_2025(self.lignes(), 0.90)
        self.assertGreaterEqual(f["global"], 0.90 * 1.05 - 1e-9)
        f = mesures_decision.meilleur_fixe_2025(self.lignes(), 0.70)
        self.assertGreaterEqual(f["global"], 0.82 - 1e-9)


class RegleSimple(unittest.TestCase):
    """La ligne « règle simple apprise sur 2025 » du tableau de choix."""

    def lignes(self):
        out = []
        for prevu, limite, E in ((3.0, 0.97, 1e6), (4.0, 0.99, 2e6), (7.0, 0.93, 1e6), (12.0, 0.90, 3e6), (13.0, 0.88, 1e6)):
            out.append({"courbe": courbe({"m": 0.98, "s": 0.04, "pmax": 0.5, "bruit": 0.0}), "estimation": E,
                        "gagne_grille": [x <= limite for x in XS], "fixes": [x <= limite for x in mesures_decision.FIXES],
                        "nombre_prevu": prevu})
        return out

    def reg(self):
        return {**decision.charger_reglages(), "regle_simple": mesures_decision.apprendre_regle_simple(self.lignes())}

    def test_meme_choix_que_le_meilleur_taux_fixe_de_2025(self):
        ls, reg = self.lignes(), self.reg()
        ch = courbe({"m": 0.98, "s": 0.04, "pmax": 0.5, "bruit": 0.0})
        for c in (0.70, 0.80, 0.85, 0.90):
            attendu = mesures_decision.meilleur_fixe_2025(ls, c)
            xmin = max(0.82, c * 1.05)
            for prevu, tranche in ((3.0, 0), (7.0, 1), (12.0, 2)):
                pt = decision.regle_simple(XS, ch, c, 1e7, xmin, 1.2, reg, prevu)
                self.assertAlmostEqual(pt["x"], attendu["tiers"][tranche], places=9, msg=f"coût {c} tranche {tranche}")
            self.assertAlmostEqual(decision.regle_simple(XS, ch, c, 1e7, xmin, 1.2, reg, None)["x"], attendu["global"], places=9)

    def test_la_ligne_est_la_derniere_du_tableau_et_respecte_plancher_et_marge(self):
        reg = self.reg()
        for cout in (7_000_000, 8_500_000, 9_300_000):
            ev = decision.evaluer(XS, courbe({"m": 0.98, "s": 0.04, "pmax": 0.6, "bruit": 0.0}), 1e7, cout, 0.05, reg, 3.0)
            fixe = ev["tableau"][-1]
            self.assertEqual(fixe["cle"], "fixe")
            self.assertGreaterEqual(fixe["x"], max(0.82, cout / 1e7 * 1.05) - 1e-9)
            self.assertFalse(fixe["sous_marge"] or fixe["hors_plage"])
            self.assertAlmostEqual(fixe["gain"], (fixe["x"] - cout / 1e7) * fixe["chance"], places=12)

    def test_la_ligne_ne_change_ni_la_recommandation_ni_la_decision(self):
        reg, sans = self.reg(), {k: v for k, v in self.reg().items() if k != "regle_simple"}
        ch = courbe({"m": 0.98, "s": 0.04, "pmax": 0.7, "bruit": 0.01})
        a = decision.evaluer(XS, ch, 1e7, 8_500_000, 0.05, reg, 3.0)
        b = decision.evaluer(XS, ch, 1e7, 8_500_000, 0.05, sans, 3.0)
        self.assertEqual(a["reco"], b["reco"])
        self.assertEqual(a["tableau"][:-1], b["tableau"])
        self.assertEqual(decision.decider(a, None, {"prevu": 3.0}, reg), decision.decider(b, None, {"prevu": 3.0}, sans))

    def test_pas_de_ligne_quand_la_plage_est_vide(self):
        ev = decision.evaluer(XS, courbe({"m": 1.0, "s": 0.05, "pmax": 0.6, "bruit": 0.0}), 1e7, 13_000_000, 0.05, self.reg(), 3.0)
        self.assertTrue(ev["vide"])
        self.assertFalse(any(t["cle"] == "fixe" for t in ev["tableau"]))

    def test_le_fichier_livre_couvre_toute_la_grille(self):
        rs = decision.charger_reglages()["regle_simple"]
        self.assertEqual(rs["appris_sur"], "2025")
        self.assertEqual(len(rs["tranches"]), 3)
        for t in [*rs["tranches"], rs["tout"]]:
            self.assertEqual(len(t["w"]), len(rs["taux"]))
            self.assertTrue(all(0.0 <= w <= 1.0 for w in t["w"]))


if __name__ == "__main__":
    unittest.main()
