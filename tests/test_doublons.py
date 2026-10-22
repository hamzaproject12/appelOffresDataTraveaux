import tempfile
import unittest
from pathlib import Path

from copilote import bases
from copilote.doublons import charger_societes, ecrire_csv, trouver
from tests import outils


def soc(nom, refs):
    return {"nom": nom, "refs": set(refs)}


class Doublons(unittest.TestCase):
    def test_noms_proches_et_jamais_ensemble_sont_signales(self):
        societes = {"bentahaholdingservice": soc("BENTAHA HOLDING SERVICE", {"m1"}),
                    "bentahaholdingservices": soc("STE BENTAHA HOLDING SERVICES", {"m2", "m3"})}
        p = trouver(societes)
        self.assertEqual(len(p), 1)
        self.assertEqual((p[0]["cle_a"], p[0]["marches_b"]), ("bentahaholdingservice", 2))
        self.assertGreaterEqual(p[0]["score"], 90)

    def test_deux_homonymes_qui_se_croisent_ne_sont_pas_des_doublons(self):
        societes = {"bentahaholdingservice": soc("A", {"m1", "m2"}),
                    "bentahaholdingservices": soc("B", {"m2"})}
        self.assertEqual(trouver(societes), [])

    def test_noms_differents_ignores(self):
        societes = {"constructionelamrani": soc("CONSTRUCTION EL AMRANI", {"m3"}),
                    "batimentbenjelloun": soc("BATIMENT BENJELLOUN", {"m4"})}
        self.assertEqual(trouver(societes), [])

    def test_charger_societes_depuis_la_base(self):
        with tempfile.TemporaryDirectory() as d:
            chemin = Path(d) / "h.db"
            db = outils.creer(chemin)
            outils.marche(db, "m1", 100, [("alpha", 90), ("beta", 100)])
            outils.marche(db, "m2", 100, [("alpha", 95)])
            outils.marche(db, "m3", 100, [("gamma", 95)], principale=0)     # republication : ignorée
            db.commit()
            db.close()
            lecture = bases._lecture_seule(chemin)
            try:
                s = charger_societes(lecture)
            finally:
                lecture.close()
        self.assertEqual(set(s), {"alpha", "beta"})
        self.assertEqual(s["alpha"]["refs"], {"m1", "m2"})

    def test_le_rapport_ne_modifie_rien_et_s_ecrit_en_csv(self):
        societes = {"bentahaholdingservice": soc("A", {"m1"}),
                    "bentahaholdingservices": soc("B", {"m2"})}
        with tempfile.TemporaryDirectory() as d:
            sortie = Path(d) / "sous" / "doublons.csv"
            ecrire_csv(trouver(societes), sortie)
            lignes = sortie.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(lignes[0], "score;cle_a;nom_a;marches_a;cle_b;nom_b;marches_b")
        self.assertEqual(len(lignes), 2)
        self.assertEqual(set(societes), {"bentahaholdingservice", "bentahaholdingservices"})


if __name__ == "__main__":
    unittest.main()
