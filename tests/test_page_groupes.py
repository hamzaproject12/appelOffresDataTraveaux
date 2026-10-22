"""Étape 3 : l'API et la page /copilote/groupes.

Une base de poche de 2 groupes (a, b, c sur 10 marchés ; e, f, g sur 5), calculée pour de vrai puis
rangée dans une copilote_calcul.db temporaire. Aucune base du dossier n'est touchée.
"""
import hashlib
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from copilote import acces, api_groupes, bases, config, groupes
from copilote.routes import creer_routeur
from tests import outils
from tests.test_acces import CODES, SECRET, cookie_visiteur
from tests.test_groupes import base_de_poche

ICI = Path(__file__).resolve().parent.parent
MOTS_INTERDITS = re.compile(r"entente|collusion|fraude|cartel|complicit|truqu", re.I)


def empreinte(chemin: Path) -> str:
    return hashlib.sha256(chemin.read_bytes()).hexdigest()


class Base(unittest.TestCase):
    """L'application, deux comptes (Alice, Bob) et un calcul « courant » déjà rangé."""
    avec_calcul = True

    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.d = Path(self.dossier.name)
        base_de_poche(self.d / "pv_travaux.db")
        self.patchs = [mock.patch.dict("os.environ", {"COPILOTE_ACCES": CODES}),
                       mock.patch.object(config, "COPILOTE_CALCUL", self.d / "calcul.db"),
                       mock.patch.object(config, "COPILOTE_RETOURS", self.d / "retours.db")]
        for p in self.patchs:
            p.start()
        acces._echecs.clear()
        self.app = FastAPI()
        self.app.include_router(creer_routeur(SECRET, self.d / "pv_travaux.db", self.d / "ao_ouverts.db"))
        if self.avec_calcul:
            lecture = bases.historique()
            donnees = groupes.charger(lecture, None, {"a2": "a"})
            lecture.close()
            calc = bases.calcul()
            groupes.enregistrer(calc, groupes.detecter(donnees, 0.5, tirages=20))
            calc.close()
        self.alice, self.bob = self.client("code-alice"), self.client("code-bob")

    def tearDown(self):
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

    def cliques(self, **params):
        """Toutes les cliques de toutes les familles de la liste."""
        d = self.alice.get("/api/copilote/groupes", params=params).json()
        return [c for f in d["familles"] for c in f["cliques"]]

    def groupe_abc(self):
        """L'identifiant du groupe a, b, c (le seul de 3 membres sur 10 marchés)."""
        return next(g for g in self.cliques() if {m["cle"] for m in g["membres"]} == {"a", "b", "c"})["id"]

    def famille_abc(self):
        d = self.alice.get("/api/copilote/groupes").json()
        return next(f for f in d["familles"] if {m["cle"] for m in f["permanents"]} == {"a", "b", "c"})

    def avis(self, client, **corps):
        return client.post("/api/copilote/avis", json=corps)


class Acces(Base):
    ROUTES = [("GET", "/api/copilote/groupes"), ("GET", "/api/copilote/groupe/1"), ("GET", "/api/copilote/avis"),
              ("GET", "/api/copilote/famille/0123abcd"),
              ("GET", "/api/copilote/moi"), ("GET", "/copilote/groupes"),
              ("POST", "/api/copilote/avis")]

    def test_sans_aucun_cookie_tout_est_refuse(self):
        c = TestClient(self.app, follow_redirects=False)
        for methode, chemin in self.ROUTES:
            r = c.request(methode, chemin, json={"reponse": "oui", "groupe": 1} if methode == "POST" else None)
            self.assertIn(r.status_code, (302, 401), chemin)

    def test_le_cookie_visiteur_seul_ne_suffit_pas(self):
        c = self.client()
        for methode, chemin in self.ROUTES:
            r = c.request(methode, chemin, json={"reponse": "oui", "groupe": 1} if methode == "POST" else None)
            self.assertIn(r.status_code, (302, 401), chemin)
        self.assertEqual(c.get("/copilote/groupes").headers["location"], "/copilote/entrer")

    def test_un_avis_refuse_n_ecrit_rien(self):
        c = self.client()
        self.avis(c, reponse="non", groupe=1)
        self.avis(TestClient(self.app), reponse="non", groupe=1)
        retours = bases.retours()
        try:
            groupes.preparer_retours(retours)
            self.assertEqual(retours.execute("SELECT count(*) FROM avis_groupes").fetchone()[0], 0)
        finally:
            retours.close()

    def test_la_page_s_ouvre_avec_le_code_et_revalide(self):
        r = self.alice.get("/copilote/groupes")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Groupes de présence commune", r.text)
        self.assertEqual(r.headers["cache-control"], "no-cache")
        r2 = self.alice.get("/copilote/groupes", headers={"if-none-match": r.headers["etag"]})
        self.assertEqual(r2.status_code, 304)

    def test_chaque_nouvelle_route_est_dans_le_controle_global(self):
        """test_acces.test_chaque_route_du_copilote_est_gardee les couvre : on vérifie qu'elles existent."""
        chemins = set(self.app.openapi()["paths"])
        for attendu in ("/copilote/groupes", "/api/copilote/groupes", "/api/copilote/groupe/{gid}",
                        "/api/copilote/famille/{empreinte}", "/api/copilote/avis", "/api/copilote/moi"):
            self.assertIn(attendu, chemins)


class Liste(Base):
    def test_liste_et_tri(self):
        d = self.alice.get("/api/copilote/groupes").json()
        self.assertEqual(d["total"], 2)                       # deux familles : a, b, c et e, f, g
        self.assertEqual(d["cliques"], 2)
        self.assertEqual(d["calcul"]["calcul"], "courant")
        scores = [f["score"] for f in d["familles"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        f = d["familles"][0]
        self.assertEqual({"id", "numero", "score", "taille", "permanents", "occasionnels", "cliques", "marches",
                          "avis", "premier", "dernier", "meilleure_clique"} <= set(f), True)
        self.assertEqual({"id", "score", "taille", "membres", "marches", "acheteurs", "avis"} <= set(f["cliques"][0]), True)
        par_taille = self.alice.get("/api/copilote/groupes", params={"tri": "marches"}).json()["familles"]
        self.assertEqual([x["marches"] for x in par_taille], sorted([x["marches"] for x in par_taille], reverse=True))
        recent = self.alice.get("/api/copilote/groupes", params={"tri": "recent"}).json()["familles"]
        self.assertEqual([x["dernier"] for x in recent], sorted([x["dernier"] for x in recent], reverse=True))
        self.assertEqual(recent[0]["premier"][:4], "2026")          # e, f, g : marchés de 2026
        self.assertEqual(self.alice.get("/api/copilote/groupes", params={"tri": "au-hasard"}).status_code, 422)

    def test_recherche_par_nom_sans_accent_ni_casse(self):
        g = self.alice.get("/api/copilote/groupes", params={"q": "  b  "}).json()
        self.assertEqual(g["total"], 1)
        self.assertEqual(g["familles"][0]["correspond"], ["b"])
        self.assertEqual(self.alice.get("/api/copilote/groupes", params={"q": "zzz"}).json()["total"], 0)
        self.assertEqual(api_groupes.normaliser("  Société d'Étude – n°1 "), "societe d etude n1")

    def test_sans_calcul_la_liste_est_vide_et_les_avis_refuses(self):
        calc = bases.calcul()
        calc.executescript("DROP TABLE IF EXISTS groupes_calculs")
        calc.close()
        self.assertEqual(self.alice.get("/api/copilote/groupes").json()["calcul"], None)
        self.assertEqual(self.alice.get("/api/copilote/groupe/1").status_code, 404)
        self.assertEqual(self.avis(self.alice, reponse="oui", groupe=1).status_code, 409)


class Famille(Base):
    def test_la_famille_dit_permanents_occasionnels_et_cliques(self):
        f = self.famille_abc()
        self.assertEqual(re.fullmatch(r"[0-9a-f]{8}", f["id"]) is not None, True)
        self.assertEqual([m["cle"] for m in f["permanents"]], ["a", "b", "c"])
        self.assertEqual(f["occasionnels"], [])
        self.assertEqual(f["taille"], 3)
        self.assertEqual(len(f["cliques"]), 1)
        self.assertEqual(f["score"], f["cliques"][0]["score"])                  # score de la meilleure clique
        self.assertEqual(f["meilleure_clique"], f["cliques"][0]["id"])
        un = self.alice.get(f"/api/copilote/famille/{f['id']}").json()
        self.assertEqual((un["id"], un["redirigee_depuis"], un["message"]), (f["id"], None, None))
        self.assertEqual(self.alice.get("/api/copilote/famille/0123abcd").status_code, 404)
        self.assertEqual(self.alice.get("/api/copilote/famille/pas-une-empreinte").status_code, 422)

    def test_la_fiche_d_une_clique_donne_sa_famille(self):
        f = self.alice.get(f"/api/copilote/groupe/{self.groupe_abc()}").json()
        self.assertEqual(f["famille_id"], self.famille_abc()["id"])

    def test_un_lien_ancien_mene_a_la_famille_qui_partage_le_plus_de_membres(self):
        ancien = self.famille_abc()["id"]
        # Recalcul au seuil 30 % : d rejoint a, b, c, donc les permanents changent et l'empreinte aussi.
        lecture = bases.historique()
        donnees = groupes.charger(lecture, None, {"a2": "a"})
        lecture.close()
        calc = bases.calcul()
        groupes.enregistrer(calc, groupes.detecter(donnees, 0.3, tirages=20), calcul="courant")
        calc.close()
        liste = self.alice.get("/api/copilote/groupes").json()["familles"]
        self.assertEqual(sorted(sorted(m["cle"] for m in f["permanents"]) for f in liste),
                         [["a", "b", "c", "d"], ["e", "f", "g"]])
        r = self.alice.get(f"/api/copilote/famille/{ancien}").json()
        self.assertEqual(sorted(m["cle"] for m in r["permanents"]), ["a", "b", "c", "d"])
        self.assertEqual(r["redirigee_depuis"], ancien)
        self.assertIn("composition", r["message"])
        self.assertIn("3 sur 3", r["message"])
        # Le lien de la famille inchangée (e, f, g) reste le même, sans message.
        efg = next(f for f in liste if {m["cle"] for m in f["permanents"]} == {"e", "f", "g"})
        self.assertIsNone(self.alice.get(f"/api/copilote/famille/{efg['id']}").json()["message"])

    def test_un_lien_dont_aucun_membre_n_existe_plus_est_introuvable(self):
        ancien = self.famille_abc()["id"]
        calc = bases.calcul()
        calc.execute("UPDATE familles_anciennes SET permanents = ?, occasionnels = '[]' WHERE id = ?", ('["zz"]', ancien))
        calc.execute("DELETE FROM familles WHERE id = ?", (ancien,))
        calc.commit()
        calc.close()
        self.assertEqual(self.alice.get(f"/api/copilote/famille/{ancien}").status_code, 404)


class Fiche(Base):
    def test_fiche_complete(self):
        f = self.alice.get(f"/api/copilote/groupe/{self.groupe_abc()}").json()
        self.assertEqual([m["cle"] for m in f["membres"]], ["a", "b", "c"])
        self.assertEqual(f["marches"], 10)
        self.assertEqual(len(f["marches_communs"]), 10)
        self.assertEqual(len(f["paires"]), 3)
        self.assertEqual(f["question"], "Ces sociétés sont-elles liées (partenaires habituels, même dirigeant, groupement) ?")
        roles = {m["cle"]: m["role"] for m in f["membres"]}
        self.assertEqual(roles["a"], "Chef de file probable")
        self.assertIn(roles["b"], ("Membre qui gagne", "Membre qui gagne aussi"))
        self.assertEqual(f["marches_communs"] == sorted(f["marches_communs"], key=lambda m: m["date"], reverse=True), True)

    def test_marches_communs_avec_objet_et_taux_des_membres(self):
        d = self.d / "pv_travaux.db"
        db = sqlite3.connect(d)
        db.execute("UPDATE marches SET objet = 'Travaux de voirie, lot unique' WHERE ref = 'm00'")
        db.commit()
        db.close()
        f = self.alice.get(f"/api/copilote/groupe/{self.groupe_abc()}").json()
        m = next(x for x in f["marches_communs"] if x["ref"] == "m00")
        self.assertEqual(m["objet"], "Travaux de voirie, lot unique")
        self.assertEqual({o["cle"]: o["taux"] for o in m["offres"]}, {"a": 0.95, "b": 1.0, "c": 1.05})
        self.assertEqual([o["cle"] for o in m["offres"] if o["gagne"]], ["a"])
        self.assertEqual([(g["cle"], g["membre"]) for g in m["gagnants"]], [("a", True)])
        self.assertEqual(m["presents"], 3)

    def test_historique_illisible_la_fiche_reste_ouverte(self):
        bases.definir_sources(self.d / "absent.db", self.d / "ao_ouverts.db")
        f = self.alice.get(f"/api/copilote/groupe/{self.groupe_abc()}")
        self.assertEqual(f.status_code, 200)
        f = f.json()
        self.assertIn("pas lisible", f["avertissement"])
        self.assertEqual(len(f["marches_communs"]), 10)
        self.assertTrue(all(m["objet"] == "" and m["offres"] == [] for m in f["marches_communs"]))

    def test_explication_en_six_criteres_dont_la_somme_donne_le_score(self):
        f = self.alice.get(f"/api/copilote/groupe/{self.groupe_abc()}").json()
        e = f["explication"]
        self.assertEqual([c["cle"] for c in e], list(groupes.POIDS))
        self.assertEqual(sum(c["max"] for c in e), 100)
        self.assertLessEqual(abs(sum(c["points"] for c in e) - f["score"]), 0.5 + 0.05 * len(e))
        for c in e:
            self.assertTrue(c["phrase"].strip() and c["methode"].strip())
            self.assertTrue(0 <= c["points"] <= c["max"])
        self.assertIn("10 marchés", e[1]["phrase"])

    def test_groupe_inconnu(self):
        self.assertEqual(self.alice.get("/api/copilote/groupe/9999").status_code, 404)
        self.assertEqual(self.alice.get("/api/copilote/groupe/abc").status_code, 422)

    def test_la_lecture_ne_modifie_pas_l_historique(self):
        avant = empreinte(self.d / "pv_travaux.db")
        gid = self.groupe_abc()
        self.alice.get(f"/api/copilote/groupe/{gid}")
        self.avis(self.alice, reponse="oui", groupe=gid)
        self.assertEqual(empreinte(self.d / "pv_travaux.db"), avant)

    def test_vocabulaire_neutre(self):
        f = self.alice.get(f"/api/copilote/groupe/{self.groupe_abc()}").json()
        textes = [c["phrase"] + c["methode"] + c["libelle"] for c in f["explication"]] + [f["question"]]
        textes += [(ICI / "statique" / "copilote_groupes.html").read_text(encoding="utf-8")]
        for t in textes:
            self.assertIsNone(MOTS_INTERDITS.search(t), MOTS_INTERDITS.search(t) and MOTS_INTERDITS.search(t).group())


class DesAvis(Base):
    def test_avis_enregistre_puis_relu(self):
        gid = self.groupe_abc()
        f_id = self.famille_abc()["id"]
        r = self.avis(self.alice, reponse="non", cle_a="b", cle_b="a", commentaire="  Pas liées, je les connais.  ", groupe=gid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["enregistres"], r.json()["auteur"]), (1, "Alice"))
        f = self.bob.get(f"/api/copilote/groupe/{gid}").json()          # relu par un autre utilisateur
        p = next(p for p in f["paires"] if (p["a"]["cle"], p["b"]["cle"]) == ("a", "b"))        # paire rangée a < b
        self.assertEqual((p["avis"]["reponse"], p["avis"]["auteur"]), ("non", "Alice"))
        self.assertEqual(len(f["historique"]), 1)
        h = f["historique"][0]
        self.assertEqual((h["auteur"], h["reponse"], h["commentaire"], h["tout_le_groupe"]),
                         ("Alice", "non", "Pas liées, je les connais.", False))
        self.assertEqual(f["avis"], {"statut": "conteste", "oui": 0, "non": 1, "paires": 3})
        liste = self.bob.get("/api/copilote/groupes").json()["familles"]
        self.assertEqual(next(f for f in liste if f["id"] == f_id)["avis"], "conteste")      # la famille l'est aussi
        self.assertEqual(next(c for f in liste for c in f["cliques"] if c["id"] == gid)["avis"]["statut"], "conteste")
        # Le recalcul lit la même base : un « non » exclut la paire, comme dans le calcul de l'étape 2.
        retours = bases.retours()
        try:
            self.assertEqual(groupes.lire_avis(retours, fusions={}), {("a", "b"): "non"})
        finally:
            retours.close()

    def test_l_auteur_vient_de_la_session_jamais_de_la_requete(self):
        r = self.avis(self.alice, reponse="oui", cle_a="a", cle_b="b", auteur="Mallory")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["auteur"], "Alice")
        retours = bases.retours()
        try:
            self.assertEqual(retours.execute("SELECT DISTINCT auteur FROM avis_groupes").fetchall()[0][0], "Alice")
        finally:
            retours.close()

    def test_le_dernier_avis_tranche_l_emporte_et_je_ne_sais_pas_ne_tranche_pas(self):
        gid = self.groupe_abc()
        self.avis(self.alice, reponse="non", cle_a="a", cle_b="b", groupe=gid)
        self.avis(self.bob, reponse="oui", cle_a="a", cle_b="b", groupe=gid, commentaire="Même dirigeant")
        self.avis(self.alice, reponse="nsp", cle_a="a", cle_b="b", groupe=gid)
        f = self.alice.get(f"/api/copilote/groupe/{gid}").json()
        p = next(p for p in f["paires"] if p["a"]["cle"] == "a" and p["b"]["cle"] == "b")
        self.assertEqual((p["avis"]["reponse"], p["avis"]["auteur"]), ("oui", "Bob"))
        self.assertEqual(p["nb_avis"], 3)
        self.assertEqual([h["reponse"] for h in f["historique"]], ["nsp", "oui", "non"])      # du plus récent
        self.assertEqual(f["avis"]["statut"], "confirme")

    def test_avis_pour_tout_le_groupe(self):
        gid = self.groupe_abc()
        r = self.avis(self.bob, reponse="oui", groupe=gid, commentaire="Groupement déclaré")
        self.assertEqual((r.status_code, r.json()["enregistres"]), (200, 3))
        f = self.alice.get(f"/api/copilote/groupe/{gid}").json()
        self.assertEqual(f["avis"], {"statut": "confirme", "oui": 3, "non": 0, "paires": 3})
        self.assertEqual(len(f["historique"]), 1)
        self.assertTrue(f["historique"][0]["tout_le_groupe"])
        tous = self.alice.get("/api/copilote/avis").json()["avis"]
        self.assertEqual((len(tous), tous[0]["tout_le_groupe"], tous[0]["auteur"]), (1, True, "Bob"))

    def test_deux_avis_de_paire_ne_font_pas_un_avis_de_groupe(self):
        lignes = [{"cle_a": a, "cle_b": b, "reponse": "oui", "commentaire": "", "auteur": "Alice",
                   "cree_le": "2026-10-22T10:00:00+00:00", "groupe": "a|b|c", "id": i}
                  for i, (a, b) in enumerate([("a", "b"), ("a", "c")])]
        sortie = api_groupes._regrouper(lignes)
        self.assertEqual(len(sortie), 1)
        self.assertFalse(sortie[0]["tout_le_groupe"])
        lignes.append({**lignes[0], "cle_a": "b", "cle_b": "c", "id": 9})
        self.assertTrue(api_groupes._regrouper(lignes)[0]["tout_le_groupe"])

    def test_avis_refuses(self):
        gid = self.groupe_abc()
        autre = next(g["id"] for g in self.cliques() if g["id"] != gid)
        cas = [({"reponse": "peut-etre", "groupe": gid}, 422),
               ({"reponse": "oui"}, 422),                                          # ni paire ni groupe
               ({"reponse": "oui", "cle_a": "a"}, 422),
               ({"reponse": "oui", "cle_a": "a", "cle_b": "a"}, 422),
               ({"reponse": "oui", "cle_a": "a", "cle_b": "zz"}, 404),             # société sans groupe
               ({"reponse": "oui", "cle_a": "a", "cle_b": "e", "groupe": gid}, 422),   # e n'est pas dans ce groupe
               ({"reponse": "oui", "groupe": 9999}, 404),
               ({"reponse": "oui", "groupe": gid, "commentaire": "x" * (api_groupes.MAX_COMMENTAIRE + 1)}, 422)]
        for corps, statut in cas:
            self.assertEqual(self.avis(self.alice, **corps).status_code, statut, corps)
        self.assertEqual(self.avis(self.alice, reponse="oui", groupe=autre).status_code, 200)    # le même contrôle, accepté
        retours = bases.retours()
        try:
            self.assertEqual(retours.execute("SELECT count(*) FROM avis_groupes").fetchone()[0], 3)   # que les 3 paires de l'accepté
        finally:
            retours.close()

    def test_commentaire_conserve_tel_quel_sans_interpretation(self):
        texte = "<script>alert(1)</script> ' OR 1=1 --"
        gid = self.groupe_abc()
        self.assertEqual(self.avis(self.alice, reponse="nsp", cle_a="a", cle_b="c", commentaire=texte, groupe=gid).status_code, 200)
        f = self.alice.get(f"/api/copilote/groupe/{gid}").json()
        self.assertEqual(f["historique"][0]["commentaire"], texte)     # l'échappement se fait à l'affichage

    def test_les_avis_vivent_dans_copilote_retours_seulement(self):
        self.avis(self.alice, reponse="oui", groupe=self.groupe_abc())
        calc = bases.calcul()
        try:
            tables = {r[0] for r in calc.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            calc.close()
        self.assertNotIn("avis_groupes", tables)


class Pages(unittest.TestCase):
    def test_le_lien_groupes_est_masque_jusqu_a_la_reponse_du_serveur(self):
        for nom in ("ao_ouverts.html", "dashboard.html"):
            html = (ICI / "statique" / nom).read_text(encoding="utf-8")
            self.assertRegex(html, r'<a class="onglet" href="/copilote/groupes" id="onglet-groupes" hidden>', nom)
            self.assertIn("fetch('/api/copilote/moi'", html, nom)
            self.assertIn(".onglet[hidden] { display: none; }", html, nom)

    def test_les_noms_renvoient_vers_la_fiche_du_tableau_de_bord(self):
        html = (ICI / "statique" / "copilote_groupes.html").read_text(encoding="utf-8")
        self.assertIn("/app#/societes?fiche=${encodeURIComponent('s:' + cle)}", html)
        dash = (ICI / "statique" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("id: s => 's:' + s.cle", dash)          # le format d'identifiant que la page utilise

    def test_la_page_garde_la_charte(self):
        html = (ICI / "statique" / "copilote_groupes.html").read_text(encoding="utf-8")
        self.assertIn("localStorage.getItem('pv-theme')", html)
        sans_icone = re.sub(r'<link rel="icon" href="data:[^"]*">', "", html)      # l'icône est en ligne : son xmlns n'est pas un CDN
        self.assertNotRegex(sans_icone, r"<script[^>]+src=|<link[^>]+stylesheet|https?://")


if __name__ == "__main__":
    unittest.main()
