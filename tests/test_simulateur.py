"""Étape C : taux de chaque société, simulateur (deux copies identiques), prévision figée, page d'analyse."""
import json
import math
import random
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from construire_base import classement_commission, prix_de_reference
from copilote import bases, probables, similarite, simulateur, suivi, taux, verite_rejeu
from copilote.donnees import Unite
from tests.test_probables import Api, unite

ICI = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
CAS = json.loads((ICI / "tests" / "cas_simulation.json").read_text(encoding="utf-8"))["cas"]


def lancer_js(cas: list[dict]) -> dict:
    sortie = subprocess.run([NODE, str(ICI / "tests" / "lancer_simulateur.js")], input=json.dumps({"cas": cas}),
                            capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return json.loads(sortie)


def sans_exact(r: dict) -> dict:
    r = json.loads(json.dumps(r))
    if r.get("central"):
        r["central"].pop("exact", None)
    r.pop("ms", None)
    return r


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


# --------------------------------------------------------------------------- 1. deux copies identiques

@unittest.skipUnless(NODE, "node absent")
class DeuxCopies(unittest.TestCase):
    def test_meme_graine_meme_resultat_en_js_et_en_python(self):
        js = lancer_js(CAS)
        for c, rj in zip(CAS, js["resultats"]):
            mo = simulateur.appliquer(c["modele"], c.get("modifs"))
            rp = sans_exact(simulateur.simuler(mo))
            # Décisions identiques (courbe, zone, gagnants) ; valeurs continues à 1e-12 près : Math.log et Math.cos
            # du navigateur peuvent différer de ceux de Python au dernier chiffre (≈ 1e-16) sur un taux tiré.
            self.assertEqual(rp["chance"], rj["chance"], c["nom"])
            for cle in ("zone", "prix_reference", "chefs", "central", "offres_moyennes"):
                proche(self, rp.get(cle), rj.get(cle), f"{c['nom']} : {cle}")

    def test_courbe_complete_en_moins_de_300_ms(self):
        gros = json.loads(json.dumps(next(c for c in CAS if c["nom"] == "beaucoup d'offres")))
        gros["modele"]["scenarios"] = simulateur.SCENARIOS
        ms = lancer_js([gros, gros])["ms"]
        self.assertLess(min(ms), 300, ms)


class Simulateur(unittest.TestCase):
    def test_sorties_figees(self):
        for c in CAS:
            if "attendu" not in c:
                continue
            r = simulateur.simuler(c["modele"])
            a = c["attendu"]
            self.assertEqual(r["zone"], a["zone"], c["nom"])
            self.assertEqual(r["prix_reference"]["mediane"], a["mediane"], c["nom"])
            self.assertEqual(r["chance"][300], a["chance_0_95"], c["nom"])

    def test_graine_et_grille(self):
        mo = CAS[0]["modele"]
        a, b = simulateur.simuler(mo), simulateur.simuler(mo)
        self.assertEqual(a["chance"], b["chance"])
        c = simulateur.simuler({**mo, "graine": mo["graine"] + 1})
        self.assertNotEqual(a["chance"], c["chance"])
        xs = a["grille"]
        self.assertEqual((len(xs), xs[0], xs[-1]), (801, 0.8, 1.2))
        self.assertAlmostEqual(xs[1] - xs[0], 0.0005)

    def test_personne_d_autre_on_gagne_toujours(self):
        r = simulateur.simuler(next(c for c in CAS if c["nom"] == "personne d'annoncé")["modele"])
        self.assertTrue(all(v == 1.0 for v in r["chance"]))
        self.assertIsNone(r["prix_reference"]["mediane"])

    def test_la_regle_rapide_est_celle_de_construire_base(self):
        """Lecture directe de la règle (courbe) contre prix_de_reference + classement_commission importés,
        sur des milliers de cas, notre offre déposée en dernier (à égalité, l'offre déjà déposée passe)."""
        rng = random.Random(3)
        xs = simulateur.grille()[::20]
        E = 1_000_000.0
        compares = 0
        for _ in range(1500):
            k = rng.choice([0, 1, 2, 3, 5, 8, 15])
            offres = [round(rng.uniform(0.75, 1.25), 3) for _ in range(k)]
            if rng.random() < 0.2 and offres:
                offres.append(offres[0])                   # égalités de montant
            v = simulateur.victoires_sur_grille(offres, xs)
            for j, x in enumerate(xs):
                montants = [t * E for t in offres] + [x * E]
                _, _, ref, _, _ = prix_de_reference(E, montants)
                if any(abs(m - ref) < 0.02 for m in montants):
                    continue                               # arrondi au centime de construire_base : ignoré
                rangs = classement_commission(list(enumerate(montants)), ref)
                premier = next(i for i, r in rangs.items() if r == 1)
                self.assertEqual(bool(v[j]), premier == len(offres), (offres, x))
                compares += 1
        self.assertGreater(compares, 30000)

    def test_egalite_l_offre_deja_deposee_passe_avant(self):
        self.assertFalse(simulateur.victoires_sur_grille([0.95], np.array([0.95]))[0])
        # P = ((0,95 + 0,96) / 2 + 1) / 2 = 0,9775 : 0,96 est la plus proche par défaut.
        self.assertTrue(simulateur.victoires_sur_grille([0.95], np.array([0.96]))[0])
        self.assertFalse(simulateur.victoires_sur_grille([0.95], np.array([0.9495]))[0])

    def test_scenario_central_avec_construire_base(self):
        r = simulateur.simuler(CAS[0]["modele"])
        c = r["central"]
        admises = [o["taux"] for o in c["offres"] if o["admise"]]
        self.assertAlmostEqual(c["moyenne"], (sum(admises) + c["x"]) / (len(admises) + 1))
        self.assertAlmostEqual(c["prix_reference"], (c["moyenne"] + 1) / 2)
        self.assertEqual(c["exact"]["nous_gagnons"], c["nous_gagnons"])
        self.assertAlmostEqual(c["exact"]["prix_reference"] / CAS[0]["modele"]["estimation"], c["prix_reference"], places=6)

    def test_calibration_de_la_chance(self):
        t = {"x": [0.0, 0.5, 1.0], "y": [0.0, 0.4, 0.9]}
        self.assertEqual(simulateur.calibrer([0.0, 0.25, 0.5, 0.75, 1.0], t), [0.0, 0.2, 0.4, 0.65, 0.9])
        self.assertEqual(simulateur.calibrer([0.3], None), [0.3])
        c = next(c for c in CAS if c["nom"] == "calibrée")
        r = simulateur.simuler(c["modele"])
        self.assertEqual(r["chance"], simulateur.calibrer(r["chance_brute"], c["modele"]["calibration"]))
        self.assertNotEqual(r["chance"], r["chance_brute"])

    def test_retirer_une_societe_ou_changer_un_taux_change_la_courbe(self):
        mo = CAS[0]["modele"]
        base = simulateur.simuler(mo)["chance"]
        fort = max(mo["societes"], key=lambda s: s["p"])
        self.assertNotEqual(simulateur.simuler(simulateur.appliquer(mo, {fort["cle"]: {"retiree": True}}))["chance"], base)
        self.assertNotEqual(simulateur.simuler(simulateur.appliquer(mo, {fort["cle"]: {"mu": 0.9}}))["chance"], base)
        self.assertEqual(simulateur.appliquer(mo, {})["societes"], mo["societes"])

    def test_zone_autour_du_maximum_lisse(self):
        xs = [0.8 + j * 0.0005 for j in range(801)]
        plateau = [0.6 if j < 400 else max(0.0, 0.6 - (j - 400) * 0.01) for j in range(801)]
        z = simulateur.zone_recommandee(xs, plateau, 4000)
        self.assertAlmostEqual(z["de"], 0.8)
        self.assertAlmostEqual(z["x"], 0.8)                 # premier maximum de la courbe lissée
        self.assertGreater(z["a"], 0.99)
        pic = [math.exp(-((j - 300) / 40) ** 2) * 0.5 for j in range(801)]
        z = simulateur.zone_recommandee(xs, pic, 4000)
        self.assertLess(abs(z["indice"] - 300), 15)
        self.assertLess(z["de"], z["x"])
        self.assertIsNone(simulateur.zone_recommandee(xs, [0.0] * 801))


# --------------------------------------------------------------------------- 2. taux de chaque société

def memoire_taux():
    """Chez COMMUNE DE TEST : « reg » dépose à 0,90 sur 6 marchés ; un marché APRÈS la coupure à 1,50 ;
    « chef » et « membre » : membre 2 points au-dessus du chef quand il est là."""
    unites, fiches = [], {}
    for k in range(6):
        u = unite(f"r{k}", f"202503{10 + k:02d}", [("reg", 90), ("chef", 95 + k % 2), ("membre", 97 + k % 2), (f"x{k}", 100)],
                  "reg")
        unites.append(u)
    for k in range(3):
        unites.append(unite(f"m{k}", f"202504{10 + k:02d}", [("membre", 99), (f"y{k}", 101), (f"z{k}", 103)], f"y{k}"))
    unites.append(unite("apres", "20250901", [("reg", 150), ("chef", 150), ("w", 150)], "reg"))
    for u in unites:
        fiches[u.identifiant] = SimpleNamespace(quals=frozenset(), texte="Travaux de construction de pistes rurales")
    mem = probables.Memoire(unites, fiches)
    ctx = probables.Contexte(n=mem.coupe("20250831"), jour=similarite.jour_de("2025-09-01"), acheteur_cles=frozenset({bases.cle_acheteur("COMMUNE DE TEST")}), codes=frozenset(),
                             famille=mem.famille[0], estimation=100.0)
    return mem, ctx


class Taux(unittest.TestCase):
    def setUp(self):
        self.mem, self.ctx = memoire_taux()
        self.r = {**taux.DEFAUT, "min_segment": 5, "k_moyenne": 0.0, "k_sigma": 0.0}

    def test_rien_d_apres_la_coupure(self):
        seg = taux.segment(self.mem, self.ctx, self.r)
        p = taux.profil(self.mem, self.ctx, "reg", seg, taux.Similarites(), self.r)
        self.assertEqual(p["n"], 6)
        self.assertAlmostEqual(p["mu"], 0.90)
        self.assertLess(max(seg["quantiles"]), 1.2)          # le marché à 1,50 n'est pas lu

    def test_sans_historique_le_segment(self):
        seg = taux.segment(self.mem, self.ctx, self.r)
        p = taux.profil(self.mem, self.ctx, "inconnue", seg, taux.Similarites(), {**self.r, "k_moyenne": 3.0})
        self.assertEqual(p["n"], 0)
        self.assertAlmostEqual(p["mu"], seg["moyenne"])

    def test_rapprochement_vers_le_segment(self):
        seg = taux.segment(self.mem, self.ctx, self.r)
        a = taux.profil(self.mem, self.ctx, "reg", seg, taux.Similarites(), self.r)["mu"]
        b = taux.profil(self.mem, self.ctx, "reg", seg, taux.Similarites(), {**self.r, "k_moyenne": 6.0})["mu"]
        self.assertGreater(b, a)
        self.assertLess(b, seg["moyenne"])

    def test_segment_elargi_quand_trop_peu_d_offres(self):
        self.assertEqual(taux.segment(self.mem, self.ctx, self.r)["niveau"], "acheteur, famille et tranche de montant")
        self.assertEqual(taux.segment(self.mem, self.ctx, {**self.r, "min_segment": 10 ** 6})["niveau"], "tous les marchés")

    def test_profils_du_membre_et_ecart_au_chef(self):
        groupes = [{"chef": "chef", "membres": ["chef", "membre"], "numero": 1,
                    "score_chef": {"taux_victoire": 0.3}}]
        cands = [{"cle": "chef", "p": 0.5, "dans_liste": True}, {"cle": "membre", "p": 0.4, "dans_liste": True}]
        t = taux.calculer(self.mem, self.ctx, cands, groupes, reglages=self.r)
        m = t["groupes"][0]["membres"][0]
        self.assertEqual((m["chef_present"]["n"], m["chef_absent"]["n"]), (6, 3))
        self.assertLess(m["chef_present"]["mu"], m["chef_absent"]["mu"])
        self.assertAlmostEqual(m["ecart"]["mediane"], 0.02)
        self.assertEqual(t["groupes"][0]["taux_victoire_chef"], 0.3)
        self.assertIn("sous_historique", t["societes"]["membre"])

    def test_effet_de_marche_retrouve(self):
        """60 marchés de 6 sociétés, choc commun σ = 0,05, bruit propre σ = 0,02 : σ_marché ≈ 0,05."""
        rng = random.Random(1)
        unites, fiches = [], {}
        for k in range(60):
            choc = rng.gauss(0, 0.05)
            offres = [(f"s{j}", 100 * (0.9 + 0.02 * j + choc + rng.gauss(0, 0.02))) for j in range(6)]
            u = unite(f"u{k}", f"2025{1 + k // 28:02d}{1 + k % 28:02d}", offres, "s0")
            unites.append(u)
            fiches[u.identifiant] = SimpleNamespace(quals=frozenset(), texte="pistes")
        mem = probables.Memoire(unites, fiches)
        e = taux.effet_marche(mem, len(mem.unites), taux.DEFAUT)
        self.assertAlmostEqual(e["sigma"], 0.05, delta=0.012)
        self.assertGreater(e["part"], 0.7)


# --------------------------------------------------------------------------- 3. dossier, prévision figée, vérité, page

class Dossier(Api):
    def test_dossier_avec_taux_et_modele(self):
        d = self.alice.get("/api/copilote/analyse/AO1").json()
        self.assertIn("taux", d)
        mo = d["simulation"]["modele"]
        self.assertEqual(len(mo["societes"]), len(d["probables"]["candidats"]))
        self.assertEqual({s["cle"] for s in mo["societes"]}, set(d["taux"]["societes"]))
        r = simulateur.simuler(mo)
        self.assertEqual(len(r["chance"]), 801)
        self.assertTrue(0 <= min(r["chance"]) <= max(r["chance"]) <= 1)
        self.assertEqual(simulateur.simuler(mo)["chance"], r["chance"])        # même graine

    def test_prevision_figee_avec_courbe_et_zone(self):
        d = self.alice.get("/api/copilote/analyse/AO1").json()
        ret = bases.retours()
        try:
            copies = suivi.lire(ret, "AO1", None)
        finally:
            ret.close()
        self.assertEqual(len(copies), 1)
        sim = copies[0]["prevision"]["simulation"]
        r = simulateur.simuler(d["simulation"]["modele"], central=False)
        self.assertEqual(sim["chance"], [round(c, 5) for c in r["chance"]])
        self.assertEqual(sim["zone"]["x"], r["zone"]["x"])

    def test_script_du_simulateur_protege(self):
        r = self.client().get("/copilote/simulateur.js")
        self.assertEqual((r.status_code, r.headers["location"]), (302, "/copilote/entrer"))
        self.assertEqual(self.alice.get("/copilote/simulateur.js").status_code, 200)

    def test_page_etapes_4_et_5(self):
        page = (ICI / "statique" / "copilote_analyse.html").read_text(encoding="utf-8")
        for morceau in ('src="/copilote/simulateur.js"', "function blocTaux", "function blocSimulation",
                        "Simulateur.appliquer", "data-action=\"mu\"", "retablir-mu", "Votre prix (DH)",
                        "Groupes peu probables", "VERITE.retenues"):
            self.assertIn(morceau, page)
        self.assertNotIn("À venir", page)


class Verite(unittest.TestCase):
    def test_offres_retenues_admis_seulement(self):
        cs = [{"statut": "attributaire", "montant": 95.0}, {"statut": "admis", "montant": 99.0},
              {"statut": "ecarte", "montant": 80.0}]
        self.assertEqual(verite_rejeu.retenues(cs, 100.0), [0.95, 0.99])
        self.assertEqual(verite_rejeu.retenues([{"statut": None, "montant": 90.0}], 100.0), [0.9])

    def test_zone_reelle_parmi_les_vraies_offres(self):
        u = Unite(ref="c1", lot=None, acheteur="X", date="20260101", annee="2026", estimation=100.0, cle_gagnant="a",
                  prix_reference_stocke=None,
                  concurrents=[{"nom": "A", "cle": "a", "statut": "admis", "lots": "", "montant_verifie": 96.0,
                                "montant_acte": 96.0, "taux": 0.96},
                               {"nom": "B", "cle": "b", "statut": "admis", "lots": "", "montant_verifie": 104.0,
                                "montant_acte": 104.0, "taux": 1.04}])
        # P avec notre offre à 0,97 : ((0,96 + 1,04 + 0,97) / 3 + 1) / 2 = 0,995 → 0,97 est la plus proche par défaut.
        self.assertTrue(suivi.zone_reelle(u, {"zone": {"x": 0.97, "chance": 0.4}})["gagnee"])
        self.assertFalse(suivi.zone_reelle(u, {"zone": {"x": 0.95, "chance": 0.4}})["gagnee"])
        self.assertIsNone(suivi.zone_reelle(u, None))


if __name__ == "__main__":
    unittest.main()
