"""Contrôles de la réception des appels d'offres ouverts, sans toucher aux bases réelles.

    cd C:\\pv\\pv_travaux\\site
    python tester_ao_ouverts.py

Tout se passe dans un dossier temporaire : une copie réduite de pv_travaux.db pour l'historique,
une ao_ouverts.db neuve pour la réception. Les bases du dossier ne sont ni lues en écriture ni
remplacées, et le journal des visiteurs non plus.

Les contrôles demandés, et ce qu'il a fallu y ajouter :

  1. POST sans jeton                              -> 401
  2. POST avec le bon jeton                       -> 200
  3. deux POST du même ref_annonce, clients différents -> une ligne, deux clients
  4. POST sans date_limite                        -> 422, et rien en base
  5. champs hors contrat                          -> acceptés, conservés dans brut
  6. redémarrage avec une empreinte SHA-256 différente -> ao_ouverts.db intacte

  et autour : le cookie visiteur qui n'ouvre pas la porte du service, la clé d'acheteur
  identique à celle de construire_base.py, l'historique d'un marché alloti lu dans lots et
  non dans marches, les expirés conservés mais masqués, les deux journaux séparés et les
  deux plafonds indépendants.
"""
from __future__ import annotations

import gzip
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ICI = Path(__file__).resolve().parent
JETON = "jeton-d-essai-9f2c"

_echecs: list[str] = []


def verifier(nom: str, condition: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if condition else 'ÉCHEC'}  {nom}" + (f"  — {detail}" if detail else ""))
    if not condition:
        _echecs.append(nom)


def petite_base(source: Path, cible: Path) -> None:
    """Une base d'historique minuscule, au schéma de la vraie, avec deux acheteurs connus.

    On ne recopie pas pv_travaux.db (60 Mo) : on reprend ses CREATE TABLE pour que la requête
    des agrégats soit exactement celle qui tournera en production, et on y met six marchés.
    """
    lu = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        schema = [r[0] for r in lu.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name IN ('marches', 'lots')")]
    finally:
        lu.close()
    db = sqlite3.connect(cible)
    try:
        for sql in schema:
            db.execute(sql)
        # Quatre marchés simples chez « COMMUNE D'ESSAI » : 3 attributions au mieux-disant sur 4,
        # écarts 4, 6, 8 et 10 (médiane 7), 2 à 5 concurrents (médiane 3,5).
        for i, (ecart, mieux, conc) in enumerate([(4.0, 1, 2), (6.0, 1, 3), (8.0, 0, 4), (10.0, 1, 5)]):
            db.execute("INSERT INTO marches (ref, acheteur, principale, nb_lots, ecart_attributaire,"
                       " attributaire_mieux_disant, nb_concurrents) VALUES (?,?,1,1,?,?,?)",
                       (f"e{i}", "COMMUNE D'ESSAI", ecart, mieux, conc))
        # Un marché alloti : ses chiffres sont dans lots, et marches n'en porte aucun. Si le
        # serveur lisait marches pour un alloti, il ne verrait rien ici — c'est le piège testé.
        db.execute("INSERT INTO marches (ref, acheteur, principale, nb_lots) VALUES (?,?,1,2)",
                   ("e9", "PROVINCE D'ESSAI"))
        for lot, (ecart, mieux, conc) in enumerate([(20.0, 0, 6), (30.0, 1, 8)], start=1):
            db.execute("INSERT INTO lots (ref, lot, ecart_attributaire, attributaire_mieux_disant,"
                       " nb_concurrents) VALUES (?,?,?,?,?)", ("e9", str(lot), ecart, mieux, conc))
        db.commit()
    finally:
        db.close()


def demarrer(dossier: Path):
    """Importe le serveur avec ses variables d'environnement pointées sur le dossier d'essai."""
    os.environ["BASE"] = str(dossier / "pv_travaux.db")
    os.environ["JOURNAL"] = str(dossier / "journal.db")
    os.environ["AO_OUVERTS"] = str(dossier / "ao_ouverts.db")
    os.environ["JETON_SERVICE"] = JETON
    os.environ["SECRET"] = "secret-d-essai"
    os.environ["ADMIN_MOTDEPASSE"] = "mot-de-passe-d-essai"
    sys.path.insert(0, str(ICI))
    import serveur
    return importlib.reload(serveur) if "serveur" in sys.modules else serveur


def appel(ref="AO-2026-001", **extra) -> dict:
    corps = {"ref_annonce": ref, "date_limite": "2099-12-31T10:00:00",
             "acheteur": "COMMUNE D'ESSAI", "objet": "Travaux d'essai", "reference": "01/2026",
             "org_acronyme": "ESSAI", "estimation": "1 200 000,00", "url": "https://exemple/ao"}
    corps.update(extra)
    return corps


def main() -> int:
    source = Path(os.environ.get("BASE_SOURCE") or ICI / "pv_travaux.db")
    if not source.exists():
        print(f"pv_travaux.db introuvable ({source}) : impossible de reprendre son schéma.")
        return 2

    dossier = Path(tempfile.mkdtemp(prefix="essai_ao_"))
    print(f"dossier d'essai : {dossier}\n")
    try:
        petite_base(source, dossier / "pv_travaux.db")
        serveur = demarrer(dossier)
        from fastapi.testclient import TestClient
        client = TestClient(serveur.app)
        ao = lambda: sqlite3.connect(dossier / "ao_ouverts.db")

        print("1) authentification")
        r = client.post("/api/v1/appels_ouverts", json={"appels_ouverts": [appel()]})
        verifier("POST sans jeton -> 401", r.status_code == 401, f"reçu {r.status_code}")
        r = client.post("/api/v1/appels_ouverts", json={"appels_ouverts": [appel()]},
                        headers={"Authorization": "Bearer mauvais-jeton"})
        verifier("POST avec un mauvais jeton -> 401", r.status_code == 401, f"reçu {r.status_code}")
        # Le cookie visiteur ne doit pas ouvrir la porte du service.
        client.post("/entrer", data={"nom": "Visiteur d'essai"}, follow_redirects=False)
        r = client.post("/api/v1/appels_ouverts", json={"appels_ouverts": [appel()]})
        verifier("POST avec le seul cookie visiteur -> 401", r.status_code == 401, f"reçu {r.status_code}")

        entete = {"Authorization": f"Bearer {JETON}"}
        r = client.post("/api/v1/appels_ouverts", headers=entete,
                        json={"appels_ouverts": [appel(clients=["Hicham"])]})
        verifier("POST avec le bon jeton -> 200", r.status_code == 200, f"reçu {r.status_code}")
        verifier("une création annoncée", r.json().get("crees") == 1, json.dumps(r.json()))

        print("\n2) idempotence et fusion des clients")
        r = client.post("/api/v1/appels_ouverts", headers=entete,
                        json={"appels_ouverts": [appel(clients=["Pako"])]})
        verifier("le même ref_annonce repoussé -> mise à jour, pas création",
                 r.json().get("crees") == 0 and r.json().get("maj") == 1, json.dumps(r.json()))
        db = ao()
        lignes = db.execute("SELECT COUNT(*) FROM appels_ouverts").fetchone()[0]
        clients = [c for (c,) in db.execute(
            "SELECT client FROM appels_clients WHERE ref_annonce = 'AO-2026-001' ORDER BY client")]
        recu, maj = db.execute("SELECT recu_le, maj_le FROM appels_ouverts").fetchone()
        db.close()
        verifier("une seule ligne en base", lignes == 1, f"{lignes} ligne(s)")
        verifier("les deux clients sont là", clients == ["Hicham", "Pako"], str(clients))
        verifier("recu_le conservé, maj_le mis à jour", recu <= maj, f"{recu} / {maj}")

        print("\n3) refus des entrées inutilisables")
        avant = sqlite3.connect(dossier / "ao_ouverts.db")
        n_avant = avant.execute("SELECT COUNT(*) FROM appels_ouverts").fetchone()[0]
        avant.close()
        sans_date = appel(ref="AO-2026-SANS-DATE")
        del sans_date["date_limite"]
        r = client.post("/api/v1/appels_ouverts", headers=entete,
                        json={"appels_ouverts": [sans_date]})
        verifier("POST sans date_limite -> 422", r.status_code == 422, f"reçu {r.status_code}")
        verifier("le motif est explicite",
                 r.json()["rejetes"] and "date_limite" in r.json()["rejetes"][0]["motif"],
                 json.dumps(r.json().get("rejetes")))
        sans_ref = appel()
        del sans_ref["ref_annonce"]
        r = client.post("/api/v1/appels_ouverts", headers=entete, json={"appels_ouverts": [sans_ref]})
        verifier("POST sans ref_annonce -> 422", r.status_code == 422, f"reçu {r.status_code}")
        db = ao()
        n_apres = db.execute("SELECT COUNT(*) FROM appels_ouverts").fetchone()[0]
        orphelin = db.execute("SELECT COUNT(*) FROM appels_ouverts"
                              " WHERE ref_annonce = 'AO-2026-SANS-DATE'").fetchone()[0]
        db.close()
        verifier("rien n'a été écrit", n_apres == n_avant and orphelin == 0,
                 f"{n_avant} -> {n_apres}")

        print("\n4) champs hors contrat, lots et qualifications")
        r = client.post("/api/v1/appels_ouverts", headers=entete, json={"appels_ouverts": [appel(
            ref="AO-2026-002", acheteur="PROVINCE D'ESSAI", clients=["Pako"],
            categorie_principale="Travaux", mode_passation="Appel d'offres ouvert",
            entite_achat="Direction régionale", org_portail="province-essai", themes=["VRD", "Eau"],
            lots=[{"lot": "2", "intitule": "Assainissement", "estimation": 800000},
                  {"lot": "1", "intitule": "Voirie", "estimation": 400000}],
            qualifications=[{"libelle": "B.1", "classe": "3"}])]})
        verifier("les champs inconnus ne font pas échouer l'appel", r.status_code == 200,
                 f"reçu {r.status_code} — {r.text[:200]}")
        db = ao()
        brut = json.loads(db.execute("SELECT brut FROM appels_ouverts WHERE ref_annonce = 'AO-2026-002'")
                          .fetchone()[0])
        lots = db.execute("SELECT lot, intitule FROM appels_lots WHERE ref_annonce = 'AO-2026-002'"
                          " ORDER BY CAST(lot AS INTEGER)").fetchall()
        quals = db.execute("SELECT libelle, classe FROM appels_qualifications").fetchall()
        cle = db.execute("SELECT cle_acheteur FROM appels_ouverts WHERE ref_annonce = 'AO-2026-002'"
                         ).fetchone()[0]
        db.close()
        manquants = [c for c in ("categorie_principale", "mode_passation", "entite_achat",
                                 "org_portail", "themes") if c not in brut]
        verifier("brut garde le JSON d'origine en entier", not manquants, f"absents : {manquants}")
        verifier("les lots sont enregistrés et ordonnés",
                 lots == [("1", "Voirie"), ("2", "Assainissement")], str(lots))
        verifier("la qualification est enregistrée", quals == [("B.1", "3")], str(quals))
        verifier("cle_acheteur est celle de construire_base.py",
                 cle == serveur.cle_acheteur("PROVINCE D'ESSAI") == "provincedessai", str(cle))

        print("\n5) lecture par le visiteur et jointure avec l'historique")
        r = client.get("/api/v1/appels_ouverts")
        verifier("GET avec le cookie visiteur -> 200", r.status_code == 200, f"reçu {r.status_code}")
        d = r.json()
        par_ref = {l["ref_annonce"]: l for l in d["lignes"]}
        verifier("les deux appels ouverts sont rendus", d["total"] == 2, str(d["total"]))
        verifier("les clients connus sont listés", sorted(d["clients"]) == ["Hicham", "Pako"],
                 str(d["clients"]))
        h = par_ref["AO-2026-001"]["historique"]
        verifier("historique d'un acheteur à marchés simples",
                 h == {"nb_marches": 4, "taux_attribution_au_mieux_disant": 75.0,
                       "ecart_median_attribution": 7.0, "nb_concurrents_median": 3.5}, str(h))
        h2 = par_ref["AO-2026-002"]["historique"]
        # 1 marché alloti : les chiffres viennent de ses deux lots (écarts 20 et 30 -> 25),
        # pas de la ligne marches, qui est vide. nb_marches reste un nombre de marchés.
        verifier("historique d'un acheteur alloti, lu dans lots",
                 h2 == {"nb_marches": 1, "taux_attribution_au_mieux_disant": 50.0,
                        "ecart_median_attribution": 25.0, "nb_concurrents_median": 7.0}, str(h2))

        r = client.get("/api/v1/appels_ouverts", params={"client": "Hicham"})
        verifier("filtre par client", r.json()["total"] == 1, str(r.json()["total"]))
        r = client.get("/api/v1/appels_ouverts", params={"q": "essai travaux"})
        verifier("recherche : tous les mots, dans n'importe quel ordre",
                 r.json()["total"] == 2, str(r.json()["total"]))
        r = client.get("/api/v1/appels_ouverts", params={"q": "province"})
        verifier("recherche sur le nom de l'acheteur", r.json()["total"] == 1,
                 str(r.json()["total"]))
        # La recherche porte sur l'objet, l'acheteur et la référence. Pas sur l'intitulé des
        # lots : « assainissement » n'est que là, et ne doit donc rien sortir.
        r = client.get("/api/v1/appels_ouverts", params={"q": "assainissement"})
        verifier("la recherche ne va pas chercher dans les lots", r.json()["total"] == 0,
                 str(r.json()["total"]))
        r = client.get("/api/v1/appels_ouverts", params={"acheteur": "province d essai"})
        verifier("filtre par acheteur, accents et casse ignorés",
                 r.json()["total"] == 1, str(r.json()["total"]))

        print("\n6) les expirés sont conservés, et masqués par défaut")
        client.post("/api/v1/appels_ouverts", headers=entete, json={"appels_ouverts": [
            appel(ref="AO-2020-VIEUX", date_limite="2020-01-15", clients=["Hicham"])]})
        verifier("un appel expiré n'apparaît pas par défaut",
                 client.get("/api/v1/appels_ouverts").json()["total"] == 2)
        d = client.get("/api/v1/appels_ouverts", params={"actifs": "0"}).json()
        verifier("il réapparaît avec actifs=0", d["total"] == 3 and d["expires"] == 1,
                 f"total {d['total']}, expirés {d['expires']}")

        print("\n7) le journal du service est à part")
        db = ao()
        n_service = db.execute("SELECT COUNT(*) FROM journal_service").fetchone()[0]
        db.close()
        j = sqlite3.connect(dossier / "journal.db")
        pollution = j.execute("SELECT COUNT(*) FROM journal WHERE action LIKE '%appels%'"
                              " AND visiteur = 'service'").fetchone()[0]
        j.close()
        verifier("les POST sont consignés dans journal_service", n_service >= 4, f"{n_service} lignes")
        verifier("et nulle part dans le journal des visiteurs", pollution == 0)

        print("\n8) le plafond du service est à lui, et n'entame pas celui des visiteurs")
        # 600 vrais POST prendraient trop longtemps : on remplit directement la file du service,
        # qui est la seule chose que le plafond consulte.
        serveur._service_recentes.clear()
        for _ in range(serveur.LIMITE_SERVICE_HEURE):
            verrou = serveur._compter_service("essai")
        verifier("la 600e requête de l'heure passe encore", verrou)
        verifier("la 601e est refusée", not serveur._compter_service("essai"))
        avant_visiteur = len(serveur._recentes.get("nom:Visiteur d'essai", ()))
        serveur._service_recentes.clear()
        client.post("/api/v1/appels_ouverts", headers=entete, json={"appels_ouverts": [appel()]})
        verifier("un POST du service ne touche pas le compteur du visiteur",
                 len(serveur._recentes.get("nom:Visiteur d'essai", ())) == avant_visiteur)

        print("\n9) la page est servie")
        r = client.get("/ao-ouverts")
        verifier("/ao-ouverts rend la page au visiteur connecté",
                 r.status_code == 200 and "Appels d’offres ouverts" in r.text, f"reçu {r.status_code}")
        sans_cookie = TestClient(serveur.app)
        verifier("/ao-ouverts renvoie à l'accès sans cookie",
                 sans_cookie.get("/ao-ouverts", follow_redirects=False).status_code == 302)

        print("\n10) LE PIÈGE : un déploiement ne doit pas emporter ao_ouverts.db")
        # On rejoue exactement ce que fait le démarrage : une archive dont l'empreinte SHA-256
        # a changé, AMORCE_SEULEMENT=1, et decompresser() qui remplace la base servie.
        cible = dossier / "pv_travaux.db"
        marque = dossier / "pv_travaux.db.amorce"
        marque.write_text("empreinte-de-la-veille", encoding="utf-8")
        with gzip.open(dossier / "pv_travaux.db.gz", "wb") as sortie, open(cible, "rb") as source_db:
            shutil.copyfileobj(source_db, sortie)
        db = ao()
        avant_refs = {r[0] for r in db.execute("SELECT ref_annonce FROM appels_ouverts")}
        avant_clients = db.execute("SELECT COUNT(*) FROM appels_clients").fetchone()[0]
        db.close()
        empreinte_avant = serveur._empreinte(dossier / "pv_travaux.db.gz")

        os.environ["AMORCE_SEULEMENT"] = "1"
        serveur.decompresser(cible)
        empreinte_apres = marque.read_text(encoding="utf-8").strip()
        verifier("l'empreinte a bien changé, donc la base a bien été remplacée",
                 empreinte_apres == empreinte_avant != "empreinte-de-la-veille",
                 f"{empreinte_apres}")
        verifier("ao_ouverts.db existe toujours", (dossier / "ao_ouverts.db").exists())
        db = ao()
        apres_refs = {r[0] for r in db.execute("SELECT ref_annonce FROM appels_ouverts")}
        apres_clients = db.execute("SELECT COUNT(*) FROM appels_clients").fetchone()[0]
        db.close()
        verifier("ses appels sont intacts", apres_refs == avant_refs,
                 f"{len(avant_refs)} -> {len(apres_refs)}")
        verifier("ses clients sont intacts", apres_clients == avant_clients,
                 f"{avant_clients} -> {apres_clients}")
        # Et le garde-fou de démarrage refuse la configuration qui perdrait tout.
        vrai_ao = serveur.AO_BASE
        try:
            serveur.AO_BASE = serveur.BASE
            try:
                serveur.verifier_separation_ao()
                refuse = False
            except RuntimeError:
                refuse = True
        finally:
            serveur.AO_BASE = vrai_ao
        verifier("AO_OUVERTS pointé sur la base servie est refusé au démarrage", refuse)

        print()
        if _echecs:
            print(f"{len(_echecs)} contrôle(s) en échec : " + ", ".join(_echecs))
            return 1
        print("Tous les contrôles passent.")
        return 0
    finally:
        shutil.rmtree(dossier, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
