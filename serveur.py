"""Site des marchés publics : accès par nom, données servies page par page, suivi des consultations.

Lancement local :
    python -m uvicorn serveur:app --reload --port 8000

Variables d'environnement (à définir sur Railway) :
    SECRET              clé de signature des cookies (obligatoire en production)
    ADMIN_UTILISATEUR   identifiant de la page d'administration (défaut : admin)
    ADMIN_MOTDEPASSE    mot de passe de la page d'administration (obligatoire)
    BASE                chemin de la base SQLite (défaut : /data/pv.db, sinon pv.db à côté du script)
    MENTION_SUIVI       "0" pour retirer la phrase d'information sur l'enregistrement des consultations
    AO_OUVERTS          chemin de la base des appels d'offres ouverts (défaut : /data/ao_ouverts.db,
                        sinon ao_ouverts.db à côté du script). C'est une base SÉPARÉE, sur le volume :
                        pv_travaux.db est remplacée à chaque déploiement, elle ne peut rien retenir.
    JETON_SERVICE       jeton « Authorization: Bearer » du service qui pousse les appels d'offres
                        ouverts sur POST /api/v1/appels_ouverts. Sans lui, la route refuse tout.
    LIMITE_SERVICE_HEURE  plafond horaire de ce service (défaut 600), indépendant des visiteurs

Le navigateur ne reçoit jamais le jeu de données complet : chaque requête renvoie au plus 100 lignes.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

# La clé d'acheteur des appels d'offres ouverts DOIT être celle qui a servi à construire
# pv_travaux.db, sinon la jointure avec l'historique est fausse sans prévenir. On importe donc
# les deux pièces telles quelles plutôt que d'en recopier une variante :
#   PREFIXE_ORG  retire le code interne du portail (« M4 / ENAM - OFFICE… »), comme le fait
#                marche_du_portail() avant d'écrire marches.acheteur ;
#   cle_plate    réduit ce qui reste à [a-z0-9], comme cle_marche() le fait déjà des deux côtés.
# construire_base.py ne lit aucun fichier à l'import : il n'y définit que des regex et des chemins.
from construire_base import PREFIXE_ORG, cle_plate
# Le copilote (chantier 7) : ses deux bases entrent dans le contrôle de séparation ci-dessous.
from copilote.config import COPILOTE_CALCUL, COPILOTE_RETOURS
from copilote.routes import creer_routeur

ICI = Path(__file__).resolve().parent


def _empreinte(chemin: Path) -> str:
    """Les 16 premiers caractères du SHA-256 d'un fichier, pour reconnaître une archive.

    Les dates ne servent à rien ici : Docker donne aux fichiers de l'image la date du clone et
    non celle du commit, si bien qu'une archive inchangée paraît neuve à chaque construction.
    """
    import hashlib
    h = hashlib.sha256()
    try:
        with open(chemin, "rb") as f:
            for bloc in iter(lambda: f.read(1 << 20), b""):
                h.update(bloc)
    except OSError:
        return ""
    return h.hexdigest()[:16]


def decompresser(cible: Path) -> None:
    """La base voyage compressée (pv.db.gz) : décompressée, elle est trop lourde pour Git.

    On la déplie au démarrage, et on recommence quand l'archive est plus récente — c'est ce qui
    arrive à chaque déploiement d'une base mise à jour.
    """
    archive = Path(str(cible) + ".gz")
    if not archive.exists():
        # Le site Travaux embarque pv_travaux.db.gz, le site Services pv.db.gz : on prend celle
        # qui est là, sans supposer son nom.
        voisines = sorted(ICI.glob("pv*.db.gz"))
        archive = voisines[0] if len(voisines) == 1 else ICI / "pv.db.gz"
    if not archive.exists():
        return
    marque = cible.with_name(cible.name + ".amorce")
    actuelle = _empreinte(archive)
    if cible.exists() and os.environ.get("AMORCE_SEULEMENT") == "1":
        # Le pod met la base à jour tout seul sur son volume : l'archive du dépôt ne sert qu'à
        # l'amorcer. Sans garde-fou, un simple déploiement de code ferait revenir la base en
        # arrière et effacerait les mises à jour accumulées. Mais si l'archive du dépôt a
        # CHANGÉ, c'est qu'une base reconstruite arrive — plus complète que ce que le volume
        # a pu accumuler seul : elle doit prendre la place. On distingue les deux cas par
        # l'empreinte du fichier, la date n'étant pas fiable dans une image Docker.
        try:
            posee = marque.read_text(encoding="utf-8").strip()
        except OSError:
            posee = ""
        if not actuelle or posee == actuelle:
            return
        print(f"[pv] {archive.name} a changé : la base du dépôt remplace celle du volume",
              flush=True)
    if cible.exists() and cible.stat().st_mtime >= archive.stat().st_mtime:
        return
    try:
        cible.parent.mkdir(parents=True, exist_ok=True)
        provisoire = cible.with_name(cible.name + ".tmp")
        with gzip.open(archive, "rb") as source, open(provisoire, "wb") as sortie:
            shutil.copyfileobj(source, sortie)
        for reste in (str(cible) + "-wal", str(cible) + "-shm"):
            Path(reste).unlink(missing_ok=True)
        os.replace(provisoire, cible)
        if actuelle:
            try:
                marque.write_text(actuelle, encoding="utf-8")
            except OSError:
                pass                                 # sans la marque, on redéplierait une fois
        print(f"[pv] base dépliée depuis {archive.name} ({archive.stat().st_size / 1e6:.0f} Mo "
              f"compressés) vers {cible}", flush=True)
    except OSError as e:
        print(f"[pv] impossible de déplier {archive} : {e}", flush=True)


def base_voisine() -> Path | None:
    """Une base posée à côté du script sous un autre nom : un déploiement par catégorie.

    Le site Travaux embarque pv_travaux.db.gz, le site Services pv.db.gz. Tant qu'il n'y en a
    qu'une, on la trouve sans avoir à configurer quoi que ce soit.
    """
    archives = sorted(ICI.glob("pv*.db.gz"))
    return ICI / archives[0].name[:-3] if len(archives) == 1 else None


def trouver_base() -> Path:
    """La base indiquée par BASE, sinon celle posée à côté du script, sinon le volume /data."""
    if os.environ.get("BASE"):
        cible = Path(os.environ["BASE"])
    elif (ICI / "pv.db").exists():
        cible = ICI / "pv.db"
    elif base_voisine() is not None:
        cible = base_voisine()
    elif os.name != "nt" and Path("/data").is_dir():
        cible = Path("/data/pv.db")
    else:
        cible = ICI / "pv.db"
    decompresser(cible)
    return cible


def completer_base(chemin: Path) -> None:
    """Ajoute à une base d'une version antérieure ce que ce serveur attend.

    La base et le code voyagent séparément : un déploiement peut livrer ce serveur avec la base
    d'avant. Plutôt que de tomber en panne sur une colonne absente, on la crée vide — le site
    fonctionne, les nouvelles colonnes sont seulement sans contenu jusqu'à la reconstruction.
    """
    try:
        db = sqlite3.connect(chemin)
        colonnes = {r[1] for r in db.execute("PRAGMA table_info(marches)")}
        for nom, type_sql in (("classes", "TEXT"), ("mieux_disant", "TEXT"),
                              ("attributaire_mieux_disant", "INTEGER")):
            if nom not in colonnes:
                db.execute(f"ALTER TABLE marches ADD COLUMN {nom} {type_sql}")
        if "rang" not in {r[1] for r in db.execute("PRAGMA table_info(concurrents)")}:
            db.execute("ALTER TABLE concurrents ADD COLUMN rang INTEGER")
        db.execute("CREATE TABLE IF NOT EXISTS qualifications (ref TEXT, secteur TEXT, domaine TEXT,"
                   " qualification TEXT, classe TEXT, brut TEXT)")
        db.commit()
        db.close()
    except sqlite3.Error as e:
        print(f"[pv] base non complétée ({e}) : certaines colonnes resteront absentes", flush=True)


BASE = trouver_base()
completer_base(BASE)
# Le journal vit dans son propre fichier : la base de données est remplacée à chaque mise à jour,
# le suivi des consultations, lui, doit survivre.
JOURNAL_BASE = Path(os.environ.get("JOURNAL") or
                    ("/data/journal.db" if os.name != "nt" and Path("/data").is_dir()
                     else ICI / "journal.db"))
SECRET = (os.environ.get("SECRET") or secrets.token_hex(32)).encode()
ADMIN_UTILISATEUR = os.environ.get("ADMIN_UTILISATEUR", "admin")
ADMIN_MOTDEPASSE = os.environ.get("ADMIN_MOTDEPASSE", "")
MENTION_SUIVI = os.environ.get("MENTION_SUIVI", "1") != "0"
DUREE_SESSION = 12 * 3600
PAR_PAGE_MAX = 100
LIMITE_MINUTE = int(os.environ.get("LIMITE_MINUTE", 90))     # requêtes par minute
LIMITE_JOUR = int(os.environ.get("LIMITE_JOUR", 1200))       # requêtes par jour
# Les limites comptent à la fois par nom saisi ET par adresse IP : changer de nom ne remet pas
# les compteurs à zéro.

# --------------------------------------------------- appels d'offres ouverts : une base à part
# pv_travaux.db est livrée compressée dans le dépôt et REMPLACÉE sur le volume dès que
# l'empreinte de l'archive change (voir decompresser()). Tout ce qu'on y écrirait serait perdu
# au déploiement suivant. Les appels d'offres reçus du service de détection vivent donc dans
# leur propre fichier, sur le même volume, qu'aucune reconstruction ne connaît :
# construire_base.py écrit dans BASE, maj_quotidienne.py remplace BASE, planificateur.py déplie
# amorce.tar.gz (consultations/ et extraits/ seulement) — aucun des trois ne nomme ao_ouverts.db.
AO_BASE = Path(os.environ.get("AO_OUVERTS") or
               ("/data/ao_ouverts.db" if os.name != "nt" and Path("/data").is_dir()
                else ICI / "ao_ouverts.db"))
JETON_SERVICE = os.environ.get("JETON_SERVICE", "")
LIMITE_SERVICE_HEURE = int(os.environ.get("LIMITE_SERVICE_HEURE", 600))

# Où atterrit un visiteur une fois son nom donné. Les appels d'offres ouverts passent devant
# l'historique : c'est ce qu'on vient regarder tous les matins.
ACCUEIL_CONNECTE = "/ao-ouverts"

# Les pages HTML sont servies avec un ETag mais sans consigne de cache : le navigateur en garde
# alors une copie et la réutilise sans demander, si bien qu'une barre d'onglets modifiée n'apparaît
# qu'après un rechargement forcé. « no-cache » veut dire « revalide à chaque fois » : un 304 sans
# corps quand rien n'a changé, la page neuve dès que le fichier change.
REVALIDER = {"Cache-Control": "no-cache"}


def page_statique(request: Request, nom: str):
    """Sert une page HTML en la faisant revalider ; 304 sans corps quand elle n'a pas changé."""
    chemin = ICI / "statique" / nom
    # L'ETag est calculé ici et non laissé à FileResponse, qui ne le pose qu'au moment d'envoyer :
    # on ne pourrait pas le comparer à celui du visiteur, et sans lui chaque bascule d'onglet
    # retélécharge la page entière.
    st = chemin.stat()
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    entetes = {**REVALIDER, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=entetes)
    return FileResponse(chemin, headers=entetes)


def verifier_separation_ao() -> None:
    """Refuse de démarrer si ao_ouverts.db peut être emporté par le remplacement de la base.

    Le contrôle ne coûte rien et répond à la seule question qui compte : le fichier que nous
    allons remplir est-il, de près ou de loin, un de ceux que le démarrage écrase ? On compare
    les chemins résolus, pas les noms : BASE=/data/ao_ouverts.db par erreur dans les variables
    Railway suffirait à tout perdre, et personne ne s'en apercevrait avant le déploiement suivant.
    """
    a = AO_BASE.resolve()
    menaces = {
        "la base servie (BASE), remplacée dès que l'empreinte de l'archive change": BASE.resolve(),
        "le journal des visiteurs (JOURNAL)": JOURNAL_BASE.resolve(),
    }
    # decompresser() supprime aussi le -wal et le -shm de la base servie avant de la remplacer.
    for suffixe in ("-wal", "-shm", ".tmp", ".neuf", ".gz", ".amorce"):
        menaces[f"un fichier de travail de la base servie ({suffixe})"] = (
            Path(str(BASE) + suffixe).resolve())
    for quoi, chemin in menaces.items():
        if a == chemin:
            raise RuntimeError(
                f"AO_OUVERTS={AO_BASE} désigne {quoi}.\n"
                f"Les appels d'offres ouverts seraient effacés au prochain déploiement. "
                f"Donne-lui un fichier à lui sur le volume, par exemple /data/ao_ouverts.db.")

    # Les deux bases du copilote obéissent à la même règle, et ne se confondent ni entre elles
    # ni avec les appels d'offres ouverts : copilote_calcul.db se reconstruit, copilote_retours.db
    # (les avis humains) ne se reconstruit jamais.
    copilote = {
        "COPILOTE_CALCUL": COPILOTE_CALCUL.resolve(),
        "COPILOTE_RETOURS": COPILOTE_RETOURS.resolve(),
    }
    voisines = dict(menaces)
    voisines["les appels d'offres ouverts (AO_OUVERTS)"] = a
    for variable, chemin in copilote.items():
        for quoi, autre in voisines.items():
            if chemin == autre:
                raise RuntimeError(
                    f"{variable}={chemin} désigne {quoi}.\n"
                    f"Les données du copilote seraient perdues ou mélangées. "
                    f"Donne-lui un fichier à lui sur le volume, par exemple "
                    f"/data/{'copilote_calcul' if variable == 'COPILOTE_CALCUL' else 'copilote_retours'}.db.")
    if copilote["COPILOTE_CALCUL"] == copilote["COPILOTE_RETOURS"]:
        raise RuntimeError(
            "COPILOTE_CALCUL et COPILOTE_RETOURS désignent le même fichier.\n"
            "La base de calcul se supprime et se reconstruit à volonté, celle des retours jamais : "
            "elles ne peuvent pas partager un fichier.")


verifier_separation_ao()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(creer_routeur(SECRET, BASE, AO_BASE))      # copilote : /copilote/*, /api/copilote/*


# ------------------------------------------------------------------ base de données

def verifier_base() -> str:
    """Message d'erreur si la base est absente ou vide, chaîne vide si tout va bien."""
    if not BASE.exists():
        return (f"Base introuvable : {BASE}\n"
                f"Fabrique-la avec :  python construire_base.py <dossier resultats> pv.db")
    try:
        db = sqlite3.connect(f"file:{BASE}?mode=ro", uri=True)
        try:
            n = db.execute("SELECT COUNT(*) FROM marches").fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error as e:
        return (f"Base inutilisable : {BASE} ({e})\n"
                f"Refabrique-la avec :  python construire_base.py <dossier resultats> pv.db")
    return "" if n else f"Base vide : {BASE}"


PROBLEME = verifier_base()
print(f"[pv] journal : {JOURNAL_BASE}", flush=True)
print(f"[pv] base : {BASE}" + (f"\n[pv] ATTENTION — {PROBLEME}" if PROBLEME else ""), flush=True)
print(f"[pv] administration : " + (f"activée (identifiant « {ADMIN_UTILISATEUR} »)" if ADMIN_MOTDEPASSE
      else "DÉSACTIVÉE — définis ADMIN_MOTDEPASSE avant de lancer le serveur, sinon /admin refusera tout"),
      flush=True)
print(f"[pv] limites : {LIMITE_MINUTE} requêtes/minute et {LIMITE_JOUR}/jour, par nom et par adresse IP",
      flush=True)


def preparer_journal() -> None:
    db = sqlite3.connect(JOURNAL_BASE)
    try:
        db.executescript("""
          CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY, horodatage TEXT, visiteur TEXT, ip TEXT, action TEXT,
            details TEXT, resultats INTEGER, duree_ms INTEGER, agent TEXT);
          CREATE TABLE IF NOT EXISTS visiteurs (
            nom TEXT PRIMARY KEY, premiere_visite TEXT, derniere_visite TEXT, visites INTEGER, requetes INTEGER);
          CREATE INDEX IF NOT EXISTS i_j_visiteur ON journal(visiteur);
          CREATE INDEX IF NOT EXISTS i_j_date ON journal(horodatage);""")
        db.commit()
    finally:
        db.close()


preparer_journal()


def preparer_ao() -> None:
    """Crée ao_ouverts.db si elle n'existe pas. Elle n'est jamais reconstruite, seulement remplie."""
    AO_BASE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(AO_BASE)
    try:
        db.executescript("""
          CREATE TABLE IF NOT EXISTS appels_ouverts (
            ref_annonce TEXT PRIMARY KEY, org_acronyme TEXT, reference TEXT, objet TEXT,
            acheteur TEXT, cle_acheteur TEXT, publie_le TEXT, date_limite TEXT, estimation REAL,
            caution_provisoire REAL, lieu TEXT, url TEXT, source TEXT, recu_le TEXT, maj_le TEXT,
            brut TEXT);
          CREATE TABLE IF NOT EXISTS appels_clients (
            ref_annonce TEXT, client TEXT, ajoute_le TEXT, PRIMARY KEY (ref_annonce, client));
          CREATE TABLE IF NOT EXISTS appels_lots (
            ref_annonce TEXT, lot TEXT, intitule TEXT, estimation REAL, caution_provisoire REAL,
            PRIMARY KEY (ref_annonce, lot));
          CREATE TABLE IF NOT EXISTS appels_qualifications (
            ref_annonce TEXT, libelle TEXT, classe TEXT);
          CREATE TABLE IF NOT EXISTS journal_service (
            id INTEGER PRIMARY KEY, recu_le TEXT, jeton_nom TEXT, route TEXT, nb_recus INTEGER,
            nb_crees INTEGER, nb_maj INTEGER, ip TEXT);
          CREATE INDEX IF NOT EXISTS i_ao_limite ON appels_ouverts(date_limite);
          CREATE INDEX IF NOT EXISTS i_ao_acheteur ON appels_ouverts(cle_acheteur);
          CREATE INDEX IF NOT EXISTS i_ao_client ON appels_clients(client);
          CREATE INDEX IF NOT EXISTS i_ao_qual ON appels_qualifications(ref_annonce);""")
        db.commit()
    finally:
        db.close()


preparer_ao()
print(f"[pv] appels d'offres ouverts : {AO_BASE}", flush=True)
print("[pv] service de détection : " + (f"actif, {LIMITE_SERVICE_HEURE} requêtes/heure"
      if JETON_SERVICE else "DÉSACTIVÉ — définis JETON_SERVICE pour ouvrir POST /api/v1/appels_ouverts"),
      flush=True)


def sans_accents(texte: str | None) -> str:
    """Minuscules et accents retirés : « BENI MELLAL » et « Béni Mellal » s'écrivent pareil
    une fois passés ici. Les PV mélangent les deux, la recherche ne doit pas en dépendre."""
    t = unicodedata.normalize("NFKD", (texte or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def base(journal: bool = False) -> sqlite3.Connection:
    db = sqlite3.connect(JOURNAL_BASE if journal else BASE, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 4000")
    # Les noms en base portent les accents du PV et la casse de l'OCR : la recherche les ignore
    # des deux côtés grâce à cette fonction, appelée depuis le SQL.
    db.create_function("sans_accents", 1, sans_accents, deterministic=True)
    return db


def lire(sql: str, params: tuple = (), journal: bool = False) -> list[dict]:
    db = base(journal)
    try:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    finally:
        db.close()


def ecrire(sql: str, params: tuple = (), journal: bool = True) -> None:
    db = base(journal)
    try:
        db.execute(sql, params)
        db.commit()
    except sqlite3.Error:
        pass
    finally:
        db.close()


def cle_acheteur(nom: str | None) -> str:
    """La clé sous laquelle un acheteur se retrouve des deux côtés : historique et AO ouverts.

    Exactement les deux traitements que construire_base.py applique déjà au nom d'un acheteur
    avant de l'écrire — retirer le préfixe interne du portail, puis réduire à [a-z0-9]. Les
    importer plutôt que les recopier est volontaire : une variante qui dérive d'un caractère
    ferait une jointure silencieusement vide.
    """
    return cle_plate(PREFIXE_ORG.sub("", (nom or "")).strip())


# --------------------------------------------------- deux connexions, deux rôles
# L'historique est en lecture seule : il est remplacé sous nos pieds par la reconstruction
# nocturne, rien ne doit jamais y écrire depuis le serveur. Les appels d'offres ouverts, eux,
# sont la seule chose que le serveur écrit en propre, dans leur fichier à part.

def base_historique() -> sqlite3.Connection:
    """pv_travaux.db, en lecture seule."""
    db = sqlite3.connect(f"file:{BASE}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 4000")
    db.create_function("cle_acheteur", 1, cle_acheteur, deterministic=True)
    return db


def base_ao() -> sqlite3.Connection:
    """ao_ouverts.db, en lecture-écriture."""
    db = sqlite3.connect(AO_BASE, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 4000")
    db.create_function("sans_accents", 1, sans_accents, deterministic=True)
    return db


# ------------------------------------------------------------------ session signée

def signer(donnees: dict) -> str:
    charge = base64.urlsafe_b64encode(json.dumps(donnees).encode()).decode().rstrip("=")
    sceau = hmac.new(SECRET, charge.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{charge}.{sceau}"


def verifier(jeton: str | None) -> dict | None:
    if not jeton or "." not in jeton:
        return None
    charge, _, sceau = jeton.rpartition(".")
    attendu = hmac.new(SECRET, charge.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sceau, attendu):
        return None
    try:
        d = json.loads(base64.urlsafe_b64decode(charge + "=" * (-len(charge) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    return None if time.time() - d.get("t", 0) > DUREE_SESSION else d


_recentes: dict[str, deque] = defaultdict(deque)
_jour: dict[str, list] = defaultdict(lambda: [datetime.now(timezone.utc).date().isoformat(), 0])


def _compter(cle: str) -> bool:
    maintenant = time.time()
    f = _recentes[cle]
    while f and maintenant - f[0] > 60:
        f.popleft()
    f.append(maintenant)
    aujourdhui = datetime.now(timezone.utc).date().isoformat()
    compteur = _jour[cle]
    if compteur[0] != aujourdhui:
        compteur[0], compteur[1] = aujourdhui, 0
    compteur[1] += 1
    return len(f) <= LIMITE_MINUTE and compteur[1] <= LIMITE_JOUR


def adresse(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def dans_les_clous(visiteur: str, ip: str) -> bool:
    ok_nom = _compter("nom:" + visiteur)
    ok_ip = _compter("ip:" + ip)
    return ok_nom and ok_ip


def visiteur(request: Request) -> dict:
    session = verifier(request.cookies.get("pv"))
    if not session:
        raise HTTPException(401, "Session expirée")
    if not dans_les_clous(session["n"], adresse(request)):
        raise HTTPException(429, "Trop de requêtes, réessayez plus tard.")
    return session


# --------------------------------------------------- plafond propre au service de détection
# Les visiteurs sont comptés à la minute et à la journée, par nom et par IP. Le service, lui,
# pousse par rafales quand il vient de détecter une fournée : son compteur est à l'heure, dans
# sa propre file, et une rafale de sa part ne doit jamais fermer la porte à un visiteur.
_service_recentes: dict[str, deque] = defaultdict(deque)


def _compter_service(cle: str) -> bool:
    maintenant = time.time()
    f = _service_recentes[cle]
    while f and maintenant - f[0] > 3600:
        f.popleft()
    f.append(maintenant)
    return len(f) <= LIMITE_SERVICE_HEURE


def service(request: Request) -> str:
    """Authentifie le service de détection. Le cookie visiteur n'ouvre pas cette porte."""
    if not JETON_SERVICE:
        raise HTTPException(503, "JETON_SERVICE n'est pas défini sur le serveur.")
    schema, _, presente = request.headers.get("authorization", "").partition(" ")
    if schema.lower() != "bearer" or not hmac.compare_digest(presente.strip(), JETON_SERVICE):
        raise HTTPException(401, "Jeton de service absent ou invalide.")
    if not _compter_service(adresse(request)):
        raise HTTPException(429, f"Plus de {LIMITE_SERVICE_HEURE} requêtes dans l'heure.")
    return "service"


def noter_service(request: Request, route: str, recus: int, crees: int, maj: int) -> None:
    """Journalise l'appel du service dans SA table, jamais dans le journal des visiteurs."""
    db = base_ao()
    try:
        db.execute("INSERT INTO journal_service (recu_le, jeton_nom, route, nb_recus, nb_crees,"
                   " nb_maj, ip) VALUES (?,?,?,?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(timespec="seconds"), "JETON_SERVICE",
                    route, recus, crees, maj, adresse(request)))
        db.commit()
    except sqlite3.Error as e:
        print(f"[pv] journal_service non écrit : {e}", flush=True)
    finally:
        db.close()


def noter(request: Request, session: dict | None, action: str, details: str = "",
          resultats: int = 0, debut: float = 0.0) -> None:
    """Journalise une consultation (côté serveur uniquement)."""
    nom = (session or {}).get("n", "?")
    ip = adresse(request)
    ecrire("INSERT INTO journal (horodatage, visiteur, ip, action, details, resultats, duree_ms, agent)"
           " VALUES (?,?,?,?,?,?,?,?)",
           (datetime.now(timezone.utc).isoformat(timespec="seconds"), nom, ip, action, details[:500],
            resultats, int((time.time() - debut) * 1000) if debut else 0,
            request.headers.get("user-agent", "")[:200]))
    ecrire("UPDATE visiteurs SET derniere_visite = ?, requetes = requetes + 1 WHERE nom = ?",
           (datetime.now(timezone.utc).isoformat(timespec="seconds"), nom))


# ------------------------------------------------------------------ recherche plein texte

def expression_fts(q: str) -> str | None:
    mots = re.findall(r"[0-9\w]{2,}", q or "", re.UNICODE)[:6]
    return " AND ".join(f'"{m}"*' for m in mots) if mots else None


def mots_cherches(q: str, limite: int = 60) -> list[str]:
    """Les mots saisis, sans accents ni casse, les six premiers au plus."""
    return [sans_accents(m) for m in re.findall(r"[0-9\w]+", (q or "")[:limite], re.UNICODE)][:6]


def filtre_mots(q: str, colonne: str, limite: int = 60) -> tuple[str, list[str]] | None:
    """Tous les mots saisis, dans n'importe quel ordre et à n'importe quelle position.

    « beni mellal » ne cherche pas la suite exacte « beni mellal » : il cherche les noms qui
    portent « beni » ET « mellal », où qu'ils soient. Sortent donc aussi bien « COMMUNE BENI
    MELLAL » que « ... RÉGION BENI MELLAL-KHENIFRA » ou « CHEF DE LA SURETÉ DE BENIMELLAL »,
    que l'OCR a collé en un seul mot. Un seul mot saisi se comporte comme avant.
    """
    mots = mots_cherches(q, limite)
    if not mots:
        return None
    return (" AND ".join([f"sans_accents({colonne}) LIKE ?"] * len(mots)),
            [f"%{m}%" for m in mots])


def page_demandee(request: Request) -> tuple[int, int]:
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    taille = min(PAR_PAGE_MAX, max(10, int(request.query_params.get("taille", 50) or 50)))
    return page, taille


# ------------------------------------------------------------------ filtre par année
# Aucune plage codée en dur : on propose les années effectivement présentes dans la base, pourvu
# qu'elles pèsent au moins PART_MIN_ANNEE des marchés (ou qu'il s'agisse de l'année en cours).
# Tout le reste — dates douteuses (lues « 2086 » par l'OCR), marchés sans date, années résiduelles —
# forme l'entrée « incertaine ». Les tables *_annee rangent ces marchés-là sous annee = ''.
PART_MIN_ANNEE = 0.01
_annees: list[dict] | None = None


def annees() -> list[dict]:
    """[{annee, marches}] des années proposées, la plus récente d'abord, puis « incertaine »."""
    global _annees
    if _annees is None:
        lignes = lire("""SELECT CASE WHEN COALESCE(date_douteuse, 0) = 1 OR COALESCE(tri_date, '') = '' THEN ''
                         ELSE substr(tri_date, 1, 4) END annee, COUNT(*) marches
                         FROM marches WHERE principale = 1 GROUP BY annee""")
        total, courante = sum(l["marches"] for l in lignes), str(datetime.now().year)
        retenues = sorted((l for l in lignes if re.fullmatch(r"\d{4}", l["annee"]) and l["annee"] <= courante
                           and (l["marches"] >= total * PART_MIN_ANNEE or l["annee"] == courante)),
                          key=lambda l: l["annee"], reverse=True)
        reste = total - sum(l["marches"] for l in retenues)
        _annees = retenues + ([{"annee": "incertaine", "marches": reste}] if reste else [])
    return _annees


def annees_retenues() -> list[str]:
    return [a["annee"] for a in annees() if a["annee"] != "incertaine"]


def filtre_annee(annee: str | None, t: str = "m") -> tuple[str, list] | None:
    """Clause sur la table marches (alias t). BETWEEN plutôt que substr() : l'index i_m_date sert."""
    retenues = annees_retenues()
    if annee in retenues:
        return (f"{t}.tri_date BETWEEN ? AND ? AND COALESCE({t}.date_douteuse, 0) = 0",
                [annee + "0101", annee + "1231"])
    if annee == "incertaine":
        if not retenues:
            return "1=1", []
        plages = " OR ".join([f"COALESCE({t}.tri_date, '') BETWEEN ? AND ?"] * len(retenues))
        return (f"NOT (COALESCE({t}.date_douteuse, 0) = 0 AND ({plages}))",
                [x for a in retenues for x in (a + "0101", a + "1231")])
    return None


def filtre_annee_agrege(annee: str | None) -> tuple[str, list] | None:
    """Même découpage, sur les tables societes_annee / acheteurs_annee (alias y)."""
    retenues = annees_retenues()
    if annee in retenues:
        return "y.annee = ?", [annee]
    if annee == "incertaine":
        return (f"y.annee NOT IN ({','.join('?' * len(retenues))})", list(retenues)) if retenues else ("1=1", [])
    return None


# ------------------------------------------------- l'historique vu par acheteur
# Ce que les 18 085 marchés déjà attribués disent d'un acheteur, prêt à être posé en face d'un
# appel d'offres encore ouvert. C'est le seul endroit où les deux mondes se touchent.
#
# L'unité de mesure n'est pas toujours le marché : un marché alloti n'a ni écart ni mieux-disant
# au niveau du marché — tout est par lot, dans la table lots. On compte donc le marché quand il
# est simple, et chacun de ses lots quand il est alloti. nb_marches, lui, reste un nombre de
# marchés : c'est ce que le lecteur attend de « 42 marchés chez cet acheteur ».
_agregats: tuple[float, int, dict[str, dict]] | None = None


def agregats_acheteurs() -> dict[str, dict]:
    """{clé d'acheteur : {nb_marches, taux…, ecart_median…, nb_concurrents_median}}.

    Une seule passe sur la base, gardée en mémoire : 1 946 acheteurs ne pèsent rien, alors que
    recalculer la médiane à chaque affichage de page coûterait une lecture complète. La
    reconstruction nocturne remplace le fichier ; on relit quand sa date ou sa taille change.
    """
    global _agregats
    try:
        etat = (BASE.stat().st_mtime, BASE.stat().st_size)
    except OSError:
        return {}
    if _agregats is not None and _agregats[:2] == etat:
        return _agregats[2]

    marches: dict[str, int] = defaultdict(int)
    ecarts: dict[str, list] = defaultdict(list)
    mieux: dict[str, list] = defaultdict(list)
    concurrents: dict[str, list] = defaultdict(list)
    db = base_historique()
    try:
        for r in db.execute("""SELECT cle_acheteur(m.acheteur) k, COALESCE(m.nb_lots, 1) > 1 alloti,
                               m.ecart_attributaire e, m.attributaire_mieux_disant a,
                               m.nb_concurrents c FROM marches m WHERE m.principale = 1"""):
            if not r["k"]:
                continue
            marches[r["k"]] += 1
            if r["alloti"]:
                continue                     # ses chiffres sont dans lots, pas ici
            if r["e"] is not None:
                ecarts[r["k"]].append(r["e"])
            if r["a"] is not None:
                mieux[r["k"]].append(r["a"])
            if r["c"] is not None:
                concurrents[r["k"]].append(r["c"])
        for r in db.execute("""SELECT cle_acheteur(m.acheteur) k, l.ecart_attributaire e,
                               l.attributaire_mieux_disant a, l.nb_concurrents c
                               FROM lots l JOIN marches m ON m.ref = l.ref
                               WHERE m.principale = 1 AND COALESCE(m.nb_lots, 1) > 1"""):
            if not r["k"]:
                continue
            if r["e"] is not None:
                ecarts[r["k"]].append(r["e"])
            if r["a"] is not None:
                mieux[r["k"]].append(r["a"])
            if r["c"] is not None:
                concurrents[r["k"]].append(r["c"])
    except sqlite3.Error as e:
        print(f"[pv] agrégats par acheteur indisponibles ({e})", flush=True)
        return {}
    finally:
        db.close()

    out = {}
    for k, n in marches.items():
        out[k] = {
            "nb_marches": n,
            # Part des attributions qui sont allées au mieux-disant calculé, en pourcentage.
            # Lue sur les seules unités où la règle est calculable — un marché sans estimation
            # ni offres chiffrées ne dit ni oui ni non.
            "taux_attribution_au_mieux_disant":
                round(100 * sum(mieux[k]) / len(mieux[k]), 1) if mieux[k] else None,
            # Écart médian de l'attribution à l'estimation, en pourcentage : positif, l'acheteur
            # paie au-dessus de ce qu'il avait annoncé ; négatif, en dessous.
            "ecart_median_attribution": round(median(ecarts[k]), 1) if ecarts[k] else None,
            # Combien de concurrents se présentent habituellement chez lui. Les procédures sans
            # offre comptent : elles font partie de ce qu'on vient mesurer.
            "nb_concurrents_median": round(median(concurrents[k]), 1) if concurrents[k] else None,
        }
    _agregats = (etat[0], etat[1], out)
    return out


# ------------------------------------------------------------------ pages

PAGE_ACCUEIL = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Marchés publics — accès</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%231f4e78'/%3E%3Cpath d='M8 21V13M14 21V9M20 21V16M26 21V11' stroke='white' stroke-width='3' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
 :root {{ --fond:#f6f7f9; --carte:#fff; --bord:#e2e5ea; --texte:#16191d; --doux:#6b7280; --accent:#1f4e78; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --fond:#0f1216; --carte:#171b21; --bord:#262c35;
   --texte:#e6e8eb; --doux:#9aa3af; --accent:#6aa9e0; }} }}
 body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--fond); color:var(--texte);
   font:15px/1.6 "Segoe UI",system-ui,sans-serif; padding:20px; }}
 form {{ background:var(--carte); border:1px solid var(--bord); border-radius:12px; padding:28px; width:min(420px,100%); }}
 h1 {{ font-size:19px; margin:0 0 6px; }} p.sous {{ color:var(--doux); margin:0 0 22px; font-size:14px; }}
 label {{ display:block; font-size:13px; color:var(--doux); margin-bottom:6px; }}
 input {{ width:100%; font:inherit; padding:11px 12px; border:1px solid var(--bord); border-radius:8px;
   background:var(--fond); color:var(--texte); margin-bottom:16px; }}
 button {{ width:100%; font:inherit; font-weight:600; padding:11px; border:none; border-radius:8px;
   background:var(--accent); color:#fff; cursor:pointer; }}
 .erreur {{ color:#b42318; font-size:13.5px; margin-bottom:14px; }}
 .mention {{ color:var(--doux); font-size:12px; margin-top:18px; }}
</style></head><body>
<form method="post" action="/entrer">
  <h1>Marchés publics — extraits de PV</h1>
  <p class="sous">Résultats d'appels d'offres : attributaires, montants et concurrents.</p>
  {erreur}
  <label for="nom">Votre nom</label>
  <input id="nom" name="nom" required autofocus autocomplete="off" maxlength="60" placeholder="Nom et prénom">
  <button type="submit">Entrer</button>
  {mention}
</form></body></html>"""


@app.get("/", response_class=HTMLResponse)
def accueil(request: Request, erreur: str = ""):
    if PROBLEME:
        return HTMLResponse(f"<pre style='font:14px/1.6 monospace;padding:30px;white-space:pre-wrap'>"
                            f"Le site ne peut pas démarrer.\n\n{PROBLEME}</pre>", 503)
    if verifier(request.cookies.get("pv")):
        # Les appels d'offres encore ouverts sont ce qu'on vient voir en premier : c'est la page
        # d'accueil. L'historique des 18 085 marchés reste à un onglet de là.
        return RedirectResponse(ACCUEIL_CONNECTE, 302)
    mention = ('<p class="mention">Les consultations effectuées sur ce service sont enregistrées.</p>'
               if MENTION_SUIVI else "")
    return PAGE_ACCUEIL.format(erreur=f'<p class="erreur">{erreur}</p>' if erreur else "",
                               mention=mention)


@app.post("/entrer")
def entrer(request: Request, nom: str = Form(...)):
    nom = re.sub(r"\s+", " ", nom).strip()[:60]
    if len(nom) < 2:
        return RedirectResponse("/?erreur=Merci+d%27indiquer+votre+nom.", 302)
    session = {"n": nom, "t": time.time(), "s": secrets.token_hex(8)}
    maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ecrire("INSERT INTO visiteurs (nom, premiere_visite, derniere_visite, visites, requetes) VALUES (?,?,?,1,0)"
           " ON CONFLICT(nom) DO UPDATE SET derniere_visite = excluded.derniere_visite, visites = visites + 1",
           (nom, maintenant, maintenant))
    noter(request, session, "connexion")
    reponse = RedirectResponse(ACCUEIL_CONNECTE, 302)
    reponse.set_cookie("pv", signer(session), max_age=DUREE_SESSION, httponly=True, samesite="lax",
                       secure=request.url.scheme == "https")
    return reponse


@app.get("/deconnexion")
def deconnexion(request: Request):
    session = verifier(request.cookies.get("pv"))
    if session:
        noter(request, session, "déconnexion")
    reponse = RedirectResponse("/", 302)
    reponse.delete_cookie("pv")
    return reponse


@app.get("/app", response_class=HTMLResponse)
def application(request: Request):
    if not verifier(request.cookies.get("pv")):
        return RedirectResponse("/", 302)
    return page_statique(request, "dashboard.html")


# ------------------------------------------------------------------ API (toujours paginée)

@app.get("/api/stats")
def stats(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    annee = request.query_params.get("annee")
    fm = filtre_annee(annee, "m")
    if not fm:
        # Toutes années confondues : exactement les requêtes d'avant.
        g = lire("""SELECT COUNT(*) marches, SUM(attributaire IS NOT NULL) avec_attributaire,
                    SUM(infructueux) infructueux,
                    COALESCE(SUM(CASE WHEN montant_douteux = 0 THEN montant END),0) montant,
                    SUM(montant_douteux) montants_douteux,
                    SUM(estimation IS NOT NULL) avec_estimation,
                    SUM(prix_reference IS NOT NULL) avec_prix_reference,
                    (SELECT COUNT(*) FROM concurrents c JOIN marches x ON x.ref = c.ref
                     WHERE x.principale = 1) concurrents,
                    (SELECT COUNT(*) FROM societes) societes,
                    (SELECT COUNT(*) FROM acheteurs) acheteurs,
                    (SELECT COUNT(*) FROM marches WHERE principale = 0) republications
                    FROM marches WHERE principale = 1""")[0]
        annee = ""
    else:
        fx, fy = filtre_annee(annee, "x"), filtre_annee_agrege(annee)
        g = lire(f"""SELECT COUNT(*) marches, SUM(attributaire IS NOT NULL) avec_attributaire,
                    SUM(infructueux) infructueux,
                    COALESCE(SUM(CASE WHEN montant_douteux = 0 THEN montant END),0) montant,
                    SUM(montant_douteux) montants_douteux,
                    SUM(estimation IS NOT NULL) avec_estimation,
                    SUM(prix_reference IS NOT NULL) avec_prix_reference,
                    (SELECT COUNT(*) FROM concurrents c JOIN marches x ON x.ref = c.ref
                     WHERE x.principale = 1 AND {fx[0]}) concurrents,
                    (SELECT COUNT(DISTINCT y.cle) FROM societes_annee y WHERE {fy[0]}) societes,
                    (SELECT COUNT(DISTINCT y.nom) FROM acheteurs_annee y WHERE {fy[0]}) acheteurs,
                    (SELECT COUNT(*) FROM marches x WHERE x.principale = 0 AND {fx[0]}) republications
                    FROM marches m WHERE m.principale = 1 AND {fm[0]}""",
                 tuple(fx[1] + fy[1] + fy[1] + fx[1] + fm[1]))[0]
    noter(request, session, "accueil", f"année {annee}" if annee else "", 1, debut)
    return {"visiteur": session["n"], "annee": annee, **g}


@app.get("/api/annees")
def api_annees(request: Request, session: dict = Depends(visiteur)):
    """Années proposées dans le menu, avec leur nombre de marchés ; la somme fait le total de la base."""
    return annees()


@app.get("/api/marches")
def api_marches(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    p = request.query_params
    page, taille = page_demandee(request)
    where, params = ["m.principale = 1"], []
    # L'index plein texte exige déjà TOUS les mots saisis, dans n'importe quel ordre et où qu'ils
    # soient dans le PV (acheteur, objet, référence, concurrents) : « beni mellal » sort autant
    # « COMMUNE BENI MELLAL » que « ... RÉGION BENI MELLAL-KHENIFRA ».
    fts, i_recherche = expression_fts(p.get("q", "")), -1
    if fts:
        i_recherche = len(where)                 # repéré pour le repli plus bas
        where.append("m.ref IN (SELECT ref FROM recherche WHERE recherche MATCH ?)")
        params.append(fts)                       # et premier paramètre de la requête
    annee = filtre_annee(p.get("annee"))
    if annee:
        where.append(annee[0])
        params += annee[1]
    if p.get("acheteur"):
        where.append("m.acheteur = ?")
        params.append(p["acheteur"])
    if p.get("statut"):
        where.append("m.statut = ?")
        params.append(p["statut"])
    if p.get("attributaire") == "1":
        where.append("m.attributaire IS NOT NULL")
    if p.get("montant_min"):
        where.append("m.montant >= ?")
        params.append(float(p["montant_min"]))
    if p.get("estimation") == "1":
        where.append("m.estimation IS NOT NULL")
    # Qualification exigée : secteur, qualification précise ou classe. Un marché peut en exiger
    # plusieurs, d'où la sous-requête plutôt qu'une colonne.
    for cle_filtre, colonne in (("secteur", "secteur"), ("qualification", "qualification"),
                                ("classe", "classe")):
        if p.get(cle_filtre):
            where.append(f"m.ref IN (SELECT ref FROM qualifications WHERE {colonne} = ?)")
            params.append(p[cle_filtre])
    if p.get("anomalie") == "1":
        # L'attributaire réel n'est pas celui que la règle du mieux-disant désigne : soit une
        # élimination non publiée, soit une attribution qui mérite un regard.
        where.append("m.attributaire_mieux_disant = 0")
    if p.get("ecart_max"):                      # « le gagnant était au moins X % sous l'estimation »
        where.append("m.ecart_attributaire IS NOT NULL AND m.ecart_attributaire <= ?")
        params.append(float(p["ecart_max"]))
    if p.get("ecart_min"):
        where.append("m.ecart_attributaire IS NOT NULL AND m.ecart_attributaire >= ?")
        params.append(float(p["ecart_min"]))
    tris = {"date": "m.tri_date", "montant": "m.montant", "concurrents": "m.nb_concurrents",
            "acheteur": "m.acheteur", "reference": "m.reference", "attributaire": "m.attributaire",
            "estimation": "m.estimation", "reference_prix": "m.prix_reference",
            "ecart": "m.ecart_attributaire"}
    colonne = tris.get(p.get("tri", "date"), "m.tri_date")
    sens = "ASC" if p.get("sens") == "asc" else "DESC"
    filtre = " AND ".join(where)
    total = lire(f"SELECT COUNT(*) n FROM marches m WHERE {filtre}", tuple(params))[0]["n"]
    if fts and total == 0:
        # Un index de mots ne retrouve pas un mot collé à un autre par l'OCR : « mellal » ne sort
        # pas « BENIMELLAL ». Quand la recherche ne donne rien, on rebalaye en sous-chaîne —
        # plus lent, mais mieux qu'une page vide, et seulement dans ce cas-là.
        mots = filtre_mots(p.get("q", ""), "texte")
        if mots:
            where[i_recherche] = f"m.ref IN (SELECT ref FROM recherche WHERE {mots[0]})"
            params = mots[1] + params[1:]        # l'expression FTS était le premier paramètre
            filtre = " AND ".join(where)
            total = lire(f"SELECT COUNT(*) n FROM marches m WHERE {filtre}", tuple(params))[0]["n"]
    lignes = lire(f"""SELECT m.ref, m.reference, m.acheteur, m.maitre_ouvrage, m.objet, m.attributaire,
                      m.montant, m.montant_douteux, m.nb_concurrents, m.date_ouverture, m.publie_le,
                      m.date_douteuse,
                      m.statut, m.infructueux, m.estimation, m.prix_reference, m.ecart_attributaire, m.source,
                      m.classes, m.mieux_disant, m.attributaire_mieux_disant, m.nb_lots,
                      (SELECT COUNT(DISTINCT COALESCE(l.cle_attributaire, l.attributaire)) FROM lots l
                       WHERE l.ref = m.ref AND l.attributaire IS NOT NULL) AS nb_attributaires
                      FROM marches m WHERE {filtre}
                      ORDER BY {colonne} IS NULL, {colonne} {sens} LIMIT ? OFFSET ?""",
                   tuple(params) + (taille, (page - 1) * taille))
    noter(request, session, "recherche marchés",
          json.dumps({k: v for k, v in p.items() if v}, ensure_ascii=False), total, debut)
    return {"total": total, "page": page, "taille": taille, "lignes": lignes}


@app.get("/api/marche/{ref}")
def api_marche(ref: str, request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    m = lire("SELECT * FROM marches WHERE ref = ?", (ref,))
    if not m:
        raise HTTPException(404, "Marché introuvable")
    m = m[0]
    # Classés dans l'ordre de la commission : le mieux-disant d'abord, pas le moins cher.
    m["concurrents"] = lire("SELECT nom, cle, montant_acte, montant_verifie, statut, lots, ecart, rang"
                            " FROM concurrents WHERE ref = ?"
                            " ORDER BY rang IS NULL, rang, montant_verifie IS NULL, montant_verifie", (ref,))
    m["qualifications"] = lire("SELECT secteur, domaine, qualification, classe, brut"
                               " FROM qualifications WHERE ref = ?", (ref,))
    m["lots"] = lire("SELECT * FROM lots WHERE ref = ? ORDER BY CAST(lot AS INTEGER)", (ref,))
    # Les autres publications du même PV : rien n'est masqué, le lecteur peut les ouvrir.
    m["autres_versions"] = lire(
        "SELECT ref, publie_le, statut, attributaire, montant, nb_concurrents, principale"
        " FROM marches WHERE groupe = ? AND ref <> ? ORDER BY principale DESC, publie_le DESC",
        (m.get("groupe") or ref, ref)) if (m.get("versions") or 1) > 1 else []
    noter(request, session, "fiche marché", f"{ref} — {(m.get('acheteur') or '')[:80]}", 1, debut)
    return m


@app.get("/api/societes")
def api_societes(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    p = request.query_params
    page, taille = page_demandee(request)
    where, params = ["participations > 0"], []
    if p.get("q"):
        mots = filtre_mots(p["q"], "nom", 60)
        if mots:
            where.append(mots[0])
            params += mots[1]
    # « Quelles sociétés sont qualifiées B.1 classe 3 ? » — la qualification d'une société se déduit
    # des marchés auxquels elle a soumissionné : aucun registre public ne la donne autrement.
    for cle_filtre, colonne in (("secteur", "secteur"), ("qualification", "qualification"),
                                ("classe", "classe")):
        if p.get(cle_filtre):
            # Seuls les concurrents ADMIS comptent : c'est à l'examen du dossier administratif et
            # technique que la qualification est vérifiée. Un concurrent écarté à cette étape-là
            # ne prouve pas qu'il la détient — il prouve plutôt l'inverse.
            where.append(f"cle IN (SELECT c.cle FROM concurrents c"
                         f" JOIN qualifications q ON q.ref = c.ref"
                         f" WHERE c.statut IN ('admis', 'attributaire') AND q.{colonne} = ?)")
            params.append(p[cle_filtre])
    tris = {"participations": "participations", "gagnes": "gagnes", "montant": "montant",
            "nom": "nom", "acheteurs": "acheteurs"}
    colonne = tris.get(p.get("tri", "participations"), "participations")
    sens = "ASC" if p.get("sens") == "asc" else "DESC"
    filtre = " AND ".join(where)
    fy = filtre_annee_agrege(p.get("annee"))
    if not fy:
        total = lire(f"SELECT COUNT(*) n FROM societes WHERE {filtre}", tuple(params))[0]["n"]
        lignes = lire(f"SELECT * FROM societes WHERE {filtre} ORDER BY {colonne} {sens} LIMIT ? OFFSET ?",
                      tuple(params) + (taille, (page - 1) * taille))
    else:
        # Chiffres de l'exercice lus dans societes_annee. Les maîtres d'ouvrage distincts ne s'additionnent
        # pas d'une année à l'autre : pour « incertaine », qui mêle plusieurs années sur une poignée de
        # marchés, on les recompte.
        if p["annee"] == "incertaine":
            fm, pm = filtre_annee(p["annee"])
            acheteurs = f"""(SELECT COUNT(DISTINCT m.acheteur) FROM concurrents c JOIN marches m ON m.ref = c.ref
                            WHERE c.cle = s.cle AND m.principale = 1 AND m.acheteur <> '' AND {fm})"""
        else:
            acheteurs, pm = "SUM(y.acheteurs)", []
        agrege = f"""(SELECT s.cle, s.nom, SUM(y.participations) participations, SUM(y.gagnes) gagnes,
                     ROUND(SUM(y.montant), 2) montant, {acheteurs} acheteurs
                     FROM societes_annee y JOIN societes s ON s.cle = y.cle WHERE {fy[0]} GROUP BY s.cle)"""
        total = lire(f"SELECT COUNT(*) n FROM {agrege} WHERE {filtre}", tuple(pm + fy[1] + params))[0]["n"]
        lignes = lire(f"SELECT * FROM {agrege} WHERE {filtre} ORDER BY {colonne} {sens}, nom LIMIT ? OFFSET ?",
                      tuple(pm + fy[1] + params) + (taille, (page - 1) * taille))
    noter(request, session, "recherche sociétés",
          p.get("q", "") + (f" [année {p['annee']}]" if fy else ""), total, debut)
    return {"total": total, "page": page, "taille": taille, "lignes": lignes}


@app.get("/api/societe/{cle}")
def api_societe(cle: str, request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    s = lire("SELECT * FROM societes WHERE cle = ?", (cle,))
    if not s:
        raise HTTPException(404, "Société introuvable")
    s = s[0]
    # La fiche suit le filtre d'année de la liste : mêmes chiffres des deux côtés.
    annee = request.query_params.get("annee")
    fm, pm = filtre_annee(annee) or ("1=1", [])
    fy = filtre_annee_agrege(annee)
    if fy:
        s.update(lire(f"""SELECT COALESCE(SUM(y.participations), 0) participations,
                          COALESCE(SUM(y.gagnes), 0) gagnes, COALESCE(ROUND(SUM(y.montant), 2), 0) montant
                          FROM societes_annee y WHERE y.cle = ? AND {fy[0]}""", (cle, *fy[1]))[0])
        s["acheteurs"] = lire(f"""SELECT COUNT(DISTINCT m.acheteur) n FROM concurrents c JOIN marches m ON m.ref = c.ref
                                  WHERE c.cle = ? AND m.principale = 1 AND m.acheteur <> '' AND {fm}""",
                              (cle, *pm))[0]["n"]
    s["annee"] = annee if fy else ""
    # La liste des participations est paginée : jamais tronquée en silence.
    page, taille = page_demandee(request)
    total = lire(f"SELECT COUNT(*) n FROM concurrents c JOIN marches m ON m.ref = c.ref"
                 f" WHERE c.cle = ? AND m.principale = 1 AND {fm}", (cle, *pm))[0]["n"]
    lignes = lire(f"""SELECT m.ref, m.reference, m.acheteur, m.objet, m.attributaire, m.montant,
                      m.date_ouverture, m.estimation, m.prix_reference, c.montant_acte, c.montant_verifie,
                      c.statut statut_concurrent, c.ecart, m.nb_lots,
                      (SELECT COUNT(DISTINCT COALESCE(l.cle_attributaire, l.attributaire)) FROM lots l
                       WHERE l.ref = m.ref AND l.attributaire IS NOT NULL) AS nb_attributaires
                      FROM concurrents c JOIN marches m ON m.ref = c.ref
                      WHERE c.cle = ? AND m.principale = 1 AND {fm}
                      ORDER BY m.tri_date DESC, m.ref, c.rowid LIMIT ? OFFSET ?""",
                  (cle, *pm, taille, (page - 1) * taille))
    s["liste"] = {"total": total, "page": page, "taille": taille, "lignes": lignes, "annee": s["annee"]}
    # Profil de qualification : ce que la société a déjà eu le droit de viser, lu dans ses marchés.
    s["qualifications"] = lire("""SELECT q.secteur, q.domaine, q.qualification, q.classe, COUNT(*) n
                                  FROM concurrents c JOIN qualifications q ON q.ref = c.ref
                                  WHERE c.cle = ? AND c.statut IN ('admis', 'attributaire')
                                  GROUP BY q.secteur, q.domaine, q.qualification, q.classe
                                  ORDER BY n DESC, q.classe DESC LIMIT 40""", (cle,))
    # Positionnement habituel de la société par rapport à l'estimation du maître d'ouvrage
    s["ecart_moyen"] = lire(f"SELECT ROUND(AVG(c.ecart), 2) e FROM concurrents c"
                            f" JOIN marches m ON m.ref = c.ref"
                            f" WHERE c.cle = ? AND c.ecart IS NOT NULL AND m.principale = 1 AND {fm}",
                            (cle, *pm))[0]["e"]
    noter(request, session, "fiche société", s.get("nom", cle) + (f" [année {annee}]" if fy else "")
          + (f" (page {page})" if page > 1 else ""), total, debut)
    return s


@app.get("/api/acheteurs")
def api_acheteurs(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    p = request.query_params
    page, taille = page_demandee(request)
    where, params = ["1=1"], []
    if p.get("q"):
        mots = filtre_mots(p["q"], "nom", 80)
        if mots:
            where.append(mots[0])
            params += mots[1]
    tris = {"marches": "marches", "montant": "montant", "attribues": "attribues", "nom": "nom",
            "infructueux": "infructueux"}
    colonne = tris.get(p.get("tri", "marches"), "marches")
    sens = "ASC" if p.get("sens") == "asc" else "DESC"
    filtre = " AND ".join(where)
    table, tparams = "acheteurs", []
    fy = filtre_annee_agrege(p.get("annee"))
    if fy:                                 # chiffres de l'exercice, lus dans acheteurs_annee
        table = f"""(SELECT y.nom, SUM(y.marches) marches, SUM(y.attribues) attribues,
                     SUM(y.infructueux) infructueux, ROUND(SUM(y.montant), 2) montant,
                     SUM(y.concurrents) concurrents
                     FROM acheteurs_annee y WHERE {fy[0]} GROUP BY y.nom)"""
        tparams = fy[1]
    total = lire(f"SELECT COUNT(*) n FROM {table} WHERE {filtre}", tuple(tparams + params))[0]["n"]
    lignes = lire(f"SELECT * FROM {table} WHERE {filtre} ORDER BY {colonne} {sens} LIMIT ? OFFSET ?",
                  tuple(tparams + params) + (taille, (page - 1) * taille))
    noter(request, session, "recherche acheteurs",
          p.get("q", "") + (f" [année {p['annee']}]" if fy else ""), total, debut)
    return {"total": total, "page": page, "taille": taille, "lignes": lignes}


@app.get("/api/acheteur")
def api_acheteur(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    nom = request.query_params.get("nom", "")
    a = lire("SELECT * FROM acheteurs WHERE nom = ?", (nom,))
    if not a:
        raise HTTPException(404, "Maître d'ouvrage introuvable")
    a = a[0]
    annee = request.query_params.get("annee")
    fm, pm = filtre_annee(annee) or ("1=1", [])
    fy = filtre_annee_agrege(annee)
    if fy:
        a.update(lire(f"""SELECT COALESCE(SUM(y.marches), 0) marches, COALESCE(SUM(y.attribues), 0) attribues,
                          COALESCE(SUM(y.infructueux), 0) infructueux, COALESCE(ROUND(SUM(y.montant), 2), 0) montant,
                          COALESCE(SUM(y.concurrents), 0) concurrents
                          FROM acheteurs_annee y WHERE y.nom = ? AND {fy[0]}""", (nom, *fy[1]))[0])
    a["annee"] = annee if fy else ""
    page, taille = page_demandee(request)
    total = lire(f"SELECT COUNT(*) n FROM marches m WHERE m.acheteur = ? AND m.principale = 1 AND {fm}",
                 (nom, *pm))[0]["n"]
    lignes = lire(f"""SELECT m.ref, m.reference, m.objet, m.attributaire, m.montant, m.nb_concurrents,
                      m.date_ouverture, m.statut, m.estimation, m.prix_reference, m.ecart_attributaire,
                      m.montant_douteux, m.nb_lots,
                      (SELECT COUNT(DISTINCT COALESCE(l.cle_attributaire, l.attributaire)) FROM lots l
                       WHERE l.ref = m.ref AND l.attributaire IS NOT NULL) AS nb_attributaires
                      FROM marches m WHERE m.acheteur = ? AND m.principale = 1 AND {fm}
                      ORDER BY m.tri_date DESC, m.ref LIMIT ? OFFSET ?""",
                  (nom, *pm, taille, (page - 1) * taille))
    a["liste"] = {"total": total, "page": page, "taille": taille, "lignes": lignes, "annee": a["annee"]}
    a["gagnants"] = lire(f"""SELECT s.nom, COUNT(*) n FROM marches m JOIN societes s ON s.cle = m.cle_attributaire
                            WHERE m.acheteur = ? AND m.principale = 1 AND {fm}
                            GROUP BY s.nom ORDER BY n DESC LIMIT 8""", (nom, *pm))
    noter(request, session, "fiche acheteur", nom[:80] + (f" [année {annee}]" if fy else "")
          + (f" (page {page})" if page > 1 else ""), total, debut)
    return a


@app.get("/api/qualifications")
def api_qualifications(request: Request, session: dict = Depends(visiteur)):
    """De quoi remplir les trois menus : secteurs, qualifications et classes réellement présents."""
    debut = time.time()
    out = {
        "secteurs": lire("SELECT secteur valeur, COUNT(DISTINCT ref) marches FROM qualifications"
                         " WHERE secteur IS NOT NULL AND secteur <> '' GROUP BY secteur"
                         " ORDER BY marches DESC"),
        "qualifications": lire("SELECT qualification valeur, secteur, COUNT(DISTINCT ref) marches"
                               " FROM qualifications WHERE qualification IS NOT NULL AND qualification <> ''"
                               " GROUP BY qualification ORDER BY marches DESC LIMIT 400"),
        "classes": lire("SELECT classe valeur, COUNT(DISTINCT ref) marches FROM qualifications"
                        " WHERE classe IS NOT NULL AND classe <> '' GROUP BY classe ORDER BY classe"),
    }
    noter(request, session, "qualifications", "", len(out["qualifications"]), debut)
    return out


@app.get("/api/qualite")
def api_qualite(request: Request, session: dict = Depends(visiteur)):
    debut = time.time()
    fm, pm = filtre_annee(request.query_params.get("annee")) or ("1=1", [])
    statuts = lire(f"SELECT statut, COUNT(*) n FROM marches m WHERE m.principale = 1 AND {fm}"
                   " GROUP BY statut ORDER BY n DESC", tuple(pm))
    noter(request, session, "qualité", "", len(statuts), debut)
    return {"statuts": statuts}


# ------------------------------------------------------- appels d'offres ouverts
# Deux portes sur la même base, et elles ne s'ouvrent pas avec la même clé :
#   POST  le service de détection, jeton Bearer, plafond horaire à lui ;
#   GET   le visiteur du site, cookie habituel, pour alimenter la page /ao-ouverts.

def _texte(v, limite: int = 500) -> str | None:
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    t = re.sub(r"\s+", " ", str(v)).strip()[:limite]
    return t or None


def _nombre(v) -> float | None:
    """« 1 234 567,89 DH » aussi bien que 1234567.89 : le service n'est pas tenu d'être propre."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[^0-9,.\-]", "", str(v))
    if t.count(",") and t.count("."):            # « 1.234.567,89 » : le point sépare les milliers
        t = t.replace(".", "")
    t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


DATES = (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%dT%H:%M", 16),
         ("%Y-%m-%d %H:%M", 16), ("%d/%m/%Y %H:%M", 16), ("%Y-%m-%d", 10), ("%d/%m/%Y", 10))


def _date(v) -> str | None:
    """Une date de clôture en ISO, heure comprise.

    Une date sans heure devient 23:59:59 : une consultation qui ferme « le 5 novembre » est
    encore ouverte le 5 au matin, et la ranger à minuit la ferait disparaître un jour trop tôt.
    Les heures sont celles du portail (heure du Maroc) ; on ne les convertit pas, le décalage
    d'une heure avec UTC est absorbé par cette fin de journée.
    """
    t = _texte(v, 40)
    if not t:
        return None
    t = t.replace("Z", "").split("+")[0].strip()
    for forme, taille in DATES:
        try:
            d = datetime.strptime(t[:taille], forme)
        except ValueError:
            continue
        return d.isoformat(timespec="seconds") if taille > 10 else d.strftime("%Y-%m-%dT23:59:59")
    return None


def maintenant_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")[:19]


CHAMPS_AO = ("org_acronyme", "reference", "objet", "acheteur", "publie_le", "estimation",
             "caution_provisoire", "lieu", "url", "source")


@app.post("/api/v1/appels_ouverts")
async def api_pousser_appels(request: Request, _: str = Depends(service)):
    """Reçoit du service de détection les appels d'offres ouverts repérés pour des clients.

    Idempotent : le même ref_annonce peut arriver autant de fois qu'on veut. Ce qui change à
    chaque fois, c'est la liste des clients, et elle est FUSIONNÉE — un appel détecté pour
    Hicham puis repoussé pour Pako finit avec les deux, jamais avec le seul dernier arrivé.

    Les champs hors contrat (categorie_principale, mode_passation, entite_achat, org_portail,
    themes…) ne sont pas une erreur : ils ne sont pas rejetés, et la colonne brut garde le JSON
    tel qu'il est arrivé, de quoi rejouer la réception le jour où une colonne leur sera faite.
    """
    try:
        corps = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "Corps JSON illisible.")
    if not isinstance(corps, dict) or not isinstance(corps.get("appels_ouverts"), list):
        raise HTTPException(422, 'Le corps attend {"appels_ouverts": [ … ]}.')
    appels = corps["appels_ouverts"]
    # Les clients peuvent être annoncés une fois pour toute la fournée — {"clients": ["Hicham"],
    # "appels_ouverts": [ … ]} — ou appel par appel. Les deux se cumulent, et ne remplacent
    # jamais ce qui est déjà en base : un appel reçu pour Hicham puis repoussé pour Pako finit
    # avec les deux.
    clients_fournee = [n for n in (_texte(c, 80) for c in (corps.get("clients") or [])) if n]

    # On trie d'abord, on écrit ensuite : une entrée sans ref_annonce ou sans date_limite ne
    # doit laisser aucune trace en base, pas même une ligne à moitié remplie.
    rejetes, retenus = [], []
    for brut in appels:
        if not isinstance(brut, dict):
            rejetes.append({"ref_annonce": None, "motif": "entrée qui n'est pas un objet JSON"})
            continue
        ref = _texte(brut.get("ref_annonce"), 120)
        limite = _date(brut.get("date_limite"))
        if not ref:
            rejetes.append({"ref_annonce": None, "motif": "ref_annonce absent"})
        elif not limite:
            rejetes.append({"ref_annonce": ref, "motif": "date_limite absente ou illisible"})
        else:
            retenus.append((ref, limite, brut))

    # COALESCE(excluded, existant) : une repousse plus maigre que la première ne doit pas effacer
    # ce qu'on savait déjà. Seuls date_limite, maj_le et brut écrasent toujours.
    garde = ", ".join(f"{c} = COALESCE(excluded.{c}, appels_ouverts.{c})" for c in CHAMPS_AO)
    upsert = f"""INSERT INTO appels_ouverts (ref_annonce, {', '.join(CHAMPS_AO)}, cle_acheteur,
                 date_limite, recu_le, maj_le, brut)
                 VALUES (?,{','.join('?' * len(CHAMPS_AO))},?,?,?,?,?)
                 ON CONFLICT(ref_annonce) DO UPDATE SET {garde},
                   cle_acheteur = COALESCE(excluded.cle_acheteur, appels_ouverts.cle_acheteur),
                   date_limite = excluded.date_limite, maj_le = excluded.maj_le,
                   brut = excluded.brut"""

    maintenant, crees, maj = maintenant_iso(), 0, 0
    db = base_ao()
    try:
        for ref, limite, brut in retenus:
            neuf = db.execute("SELECT 1 FROM appels_ouverts WHERE ref_annonce = ?",
                              (ref,)).fetchone() is None
            v = {c: (_nombre(brut.get(c)) if c in ("estimation", "caution_provisoire")
                     else _date(brut.get(c)) if c == "publie_le"
                     else _texte(brut.get(c), 2000 if c == "objet" else 400))
                 for c in CHAMPS_AO}
            db.execute(upsert,
                       (ref, *(v[c] for c in CHAMPS_AO), cle_acheteur(v["acheteur"]) or None,
                        limite, maintenant, maintenant, json.dumps(brut, ensure_ascii=False)))
            crees, maj = crees + neuf, maj + (not neuf)

            propres = [n for n in (_texte(c, 80) for c in (brut.get("clients") or [])) if n]
            for nom in dict.fromkeys(clients_fournee + propres):
                db.execute("INSERT OR IGNORE INTO appels_clients (ref_annonce, client,"
                           " ajoute_le) VALUES (?,?,?)", (ref, nom, maintenant))
            # Les lots et les qualifications décrivent l'annonce elle-même : quand le service les
            # renvoie, c'est sa version qui fait foi. Quand il ne les envoie pas, on garde les
            # nôtres plutôt que de vider la fiche.
            if isinstance(brut.get("lots"), list):
                db.execute("DELETE FROM appels_lots WHERE ref_annonce = ?", (ref,))
                for l in brut["lots"]:
                    numero = _texte(l.get("lot"), 40) if isinstance(l, dict) else None
                    if numero:
                        db.execute("INSERT OR REPLACE INTO appels_lots VALUES (?,?,?,?,?)",
                                   (ref, numero, _texte(l.get("intitule"), 500),
                                    _nombre(l.get("estimation")),
                                    _nombre(l.get("caution_provisoire"))))
            if isinstance(brut.get("qualifications"), list):
                db.execute("DELETE FROM appels_qualifications WHERE ref_annonce = ?", (ref,))
                for q in brut["qualifications"]:
                    couple = ((_texte(q.get("libelle"), 200), _texte(q.get("classe"), 20))
                              if isinstance(q, dict) else (_texte(q, 200), None))
                    if couple[0] or couple[1]:
                        db.execute("INSERT INTO appels_qualifications VALUES (?,?,?)", (ref, *couple))
        db.commit()
    except sqlite3.Error as e:
        db.rollback()
        raise HTTPException(500, f"Écriture impossible : {e}")
    finally:
        db.close()

    noter_service(request, "POST /api/v1/appels_ouverts", len(appels), crees, maj)
    reponse = {"recus": len(appels), "crees": crees, "maj": maj, "rejetes": rejetes}
    # Tout rejeté : la réponse doit être un refus, pas un succès à zéro. Un lot mixte passe en
    # 200 avec ses rejets détaillés — perdre les entrées valides d'une fournée serait pire.
    return JSONResponse(reponse, 422 if rejetes and not retenus else 200)


@app.get("/api/v1/appels_ouverts")
def api_appels_ouverts(request: Request, session: dict = Depends(visiteur)):
    """Les appels d'offres ouverts, éclairés par ce que l'historique sait de leur acheteur."""
    debut = time.time()
    p = request.query_params
    page, taille = page_demandee(request)
    where, params = ["1 = 1"], []
    if p.get("client"):
        where.append("EXISTS (SELECT 1 FROM appels_clients c WHERE c.ref_annonce = a.ref_annonce"
                     " AND c.client = ?)")
        params.append(p["client"][:80])
    # Par défaut on ne montre que ce qui est encore ouvert. Les expirés restent en base — ils ne
    # sont jamais effacés — mais il faut les demander.
    actifs = p.get("actifs", "1") != "0"
    if actifs:
        where.append("a.date_limite >= ?")
        params.append(maintenant_iso())
    if p.get("acheteur"):
        where.append("a.cle_acheteur = ?")
        params.append(cle_acheteur(p["acheteur"]))
    if p.get("q"):
        mots = filtre_mots(p["q"], "(COALESCE(a.objet,'') || ' ' || COALESCE(a.acheteur,'')"
                                   " || ' ' || COALESCE(a.reference,''))")
        if mots:
            where.append(mots[0])
            params += mots[1]
    # Le filtre par mois porte sur la date limite : « ce que je dois rendre en novembre ».
    # Il est mis à part pour que la liste des mois proposés reste celle de tous les mois du reste
    # de la sélection — sinon choisir un mois ferait disparaître les autres du menu.
    filtre_base, params_base = " AND ".join(where), list(params)
    mois_demande = re.fullmatch(r"\d{4}-\d{2}", p.get("mois", "") or "")
    if mois_demande:
        where.append("substr(a.date_limite, 1, 7) = ?")
        params.append(mois_demande.group(0))
    tris = {"date_limite": "a.date_limite ASC", "publie_le": "a.publie_le DESC",
            "estimation": "a.estimation DESC", "acheteur": "a.acheteur ASC"}
    ordre = tris.get(p.get("tri", "date_limite"), tris["date_limite"])
    filtre = " AND ".join(where)

    db = base_ao()
    try:
        total = db.execute(f"SELECT COUNT(*) n FROM appels_ouverts a WHERE {filtre}",
                           tuple(params)).fetchone()["n"]
        lignes = [dict(r) for r in db.execute(
            f"""SELECT a.ref_annonce, a.org_acronyme, a.reference, a.objet, a.acheteur,
                a.cle_acheteur, a.publie_le, a.date_limite, a.estimation, a.caution_provisoire,
                a.lieu, a.url, a.source, a.recu_le, a.maj_le
                FROM appels_ouverts a WHERE {filtre}
                ORDER BY a.date_limite IS NULL, {ordre} LIMIT ? OFFSET ?""",
            tuple(params) + (taille, (page - 1) * taille))]
        par_ref = {l["ref_annonce"]: l for l in lignes}
        for l in lignes:
            l["clients"], l["lots"], l["qualifications"] = [], [], []
        if lignes:
            refs = list(par_ref)
            trous = ",".join("?" * len(refs))
            for r in db.execute(f"SELECT ref_annonce, client FROM appels_clients"
                                f" WHERE ref_annonce IN ({trous}) ORDER BY client", refs):
                par_ref[r["ref_annonce"]]["clients"].append(r["client"])
            for r in db.execute(f"SELECT ref_annonce, lot, intitule, estimation, caution_provisoire"
                                f" FROM appels_lots WHERE ref_annonce IN ({trous})"
                                f" ORDER BY CAST(lot AS INTEGER), lot", refs):
                par_ref[r["ref_annonce"]]["lots"].append(
                    {k: r[k] for k in ("lot", "intitule", "estimation", "caution_provisoire")})
            for r in db.execute(f"SELECT ref_annonce, libelle, classe FROM appels_qualifications"
                                f" WHERE ref_annonce IN ({trous}) ORDER BY libelle", refs):
                par_ref[r["ref_annonce"]]["qualifications"].append(
                    {"libelle": r["libelle"], "classe": r["classe"]})
        clients_connus = [r["client"] for r in db.execute(
            "SELECT client, COUNT(*) n FROM appels_clients GROUP BY client ORDER BY n DESC, client")]
        expires = db.execute("SELECT COUNT(*) n FROM appels_ouverts WHERE date_limite < ?",
                             (maintenant_iso(),)).fetchone()["n"]
        mois = [{"mois": r["m"], "n": r["n"]} for r in db.execute(
            f"""SELECT substr(a.date_limite, 1, 7) m, COUNT(*) n FROM appels_ouverts a
                WHERE {filtre_base} AND a.date_limite IS NOT NULL
                GROUP BY m ORDER BY m""", tuple(params_base))]
        # La prochaine échéance suit la sélection affichée, mois compris : sinon, en regardant
        # novembre, le chiffre clé annoncerait une date d'octobre qui n'est dans aucune ligne.
        prochaine = db.execute(
            f"SELECT MIN(a.date_limite) d FROM appels_ouverts a"
            f" WHERE {filtre} AND a.date_limite >= ?",
            tuple(params) + (maintenant_iso(),)).fetchone()["d"]
    finally:
        db.close()

    # Le pont entre les deux bases : ce que les marchés déjà attribués disent de cet acheteur.
    # Rien quand il est inconnu de l'historique — la page affichera un tiret, pas un zéro.
    histo = agregats_acheteurs()
    for l in lignes:
        l["historique"] = histo.get(l["cle_acheteur"] or "")

    noter(request, session, "appels d'offres ouverts",
          json.dumps({k: v for k, v in p.items() if v}, ensure_ascii=False), total, debut)
    return {"total": total, "page": page, "taille": taille, "lignes": lignes,
            "clients": clients_connus, "expires": expires, "actifs": actifs,
            "mois": mois, "prochaine": prochaine}


@app.get("/ao-ouverts", response_class=HTMLResponse)
def page_ao_ouverts(request: Request):
    if not verifier(request.cookies.get("pv")):
        return RedirectResponse("/", 302)
    return page_statique(request, "ao_ouverts.html")


# ------------------------------------------------------------------ administration

PAGE_ADMIN_CONNEXION = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Administration</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f7f9;color:#16191d;
 font:15px/1.6 "Segoe UI",system-ui,sans-serif;padding:20px}}
 form{{background:#fff;border:1px solid #e2e5ea;border-radius:12px;padding:28px;width:min(400px,100%)}}
 h1{{font-size:18px;margin:0 0 20px}} label{{display:block;font-size:13px;color:#6b7280;margin-bottom:6px}}
 input{{width:100%;font:inherit;padding:11px 12px;border:1px solid #e2e5ea;border-radius:8px;
 background:#f6f7f9;margin-bottom:16px}}
 button{{width:100%;font:inherit;font-weight:600;padding:11px;border:none;border-radius:8px;
 background:#1f4e78;color:#fff;cursor:pointer}} .erreur{{color:#b42318;font-size:13.5px;margin-bottom:14px}}
</style></head><body><form method="post" action="/admin/entrer">
 <h1>Administration</h1>{erreur}
 <label for="m">Mot de passe</label>
 <input id="m" name="motdepasse" type="password" required autofocus>
 <button type="submit">Entrer</button></form></body></html>"""


def admin_connecte(request: Request) -> bool:
    """Cookie d'administration, ou identifiants envoyés par le navigateur (compatibilité)."""
    if not ADMIN_MOTDEPASSE:
        return False
    jeton = verifier(request.cookies.get("adm"))
    if jeton and jeton.get("a") == 1:
        return True
    entete = request.headers.get("authorization", "")
    if entete.startswith("Basic "):
        try:
            utilisateur, _, motdepasse = base64.b64decode(entete[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(utilisateur, ADMIN_UTILISATEUR) \
            and hmac.compare_digest(motdepasse, ADMIN_MOTDEPASSE)
    return False


@app.post("/admin/entrer")
def admin_entrer(request: Request, motdepasse: str = Form(...)):
    if not ADMIN_MOTDEPASSE:
        return HTMLResponse("ADMIN_MOTDEPASSE n'est pas défini sur le serveur.", 503)
    if not hmac.compare_digest(motdepasse, ADMIN_MOTDEPASSE):
        noter(request, {"n": "?"}, "admin refusé")
        return RedirectResponse("/admin?e=1", 302)
    reponse = RedirectResponse("/admin", 302)
    reponse.set_cookie("adm", signer({"a": 1, "t": time.time()}), max_age=DUREE_SESSION, httponly=True,
                       samesite="lax", secure=request.url.scheme == "https")
    return reponse


@app.get("/admin/sortir")
def admin_sortir():
    reponse = RedirectResponse("/admin", 302)
    reponse.delete_cookie("adm")
    return reponse


@app.get("/admin", response_class=HTMLResponse)
def page_admin(request: Request, e: str = ""):
    if not ADMIN_MOTDEPASSE:
        return HTMLResponse("<pre style='font:14px monospace;padding:30px;white-space:pre-wrap'>"
                            "Administration désactivée.\n\nDéfinis ADMIN_MOTDEPASSE puis relance le serveur :\n"
                            "  $env:ADMIN_MOTDEPASSE = \"ton-mot-de-passe\"\n"
                            "  python -m uvicorn serveur:app --port 8000</pre>", 503)
    if not admin_connecte(request):
        return HTMLResponse(PAGE_ADMIN_CONNEXION.format(
            erreur='<p class="erreur">Mot de passe incorrect.</p>' if e else ""), 401)
    visiteurs = lire("SELECT * FROM visiteurs ORDER BY derniere_visite DESC LIMIT 100", journal=True)
    jours = lire("SELECT substr(horodatage,1,10) jour, COUNT(*) n, COUNT(DISTINCT visiteur) v"
                 " FROM journal GROUP BY jour ORDER BY jour DESC LIMIT 14", journal=True)
    actions = lire("SELECT action, COUNT(*) n FROM journal GROUP BY action ORDER BY n DESC", journal=True)
    recherches = lire("SELECT details, COUNT(*) n FROM journal WHERE action = 'recherche marchés'"
                      " AND details NOT IN ('', '{}') GROUP BY details ORDER BY n DESC LIMIT 25", journal=True)
    dernieres = lire("SELECT horodatage, visiteur, ip, action, details, resultats FROM journal"
                     " ORDER BY id DESC LIMIT 200", journal=True)

    def table(titre, colonnes, lignes):
        if not lignes:
            return f"<h2>{titre}</h2><p class=doux>Rien pour l'instant.</p>"
        entetes = "".join(f"<th>{c}</th>" for c in colonnes)
        corps = "".join("<tr>" + "".join(f"<td>{str(v)[:160]}</td>" for v in l.values()) + "</tr>" for l in lignes)
        return f"<h2>{titre}</h2><table><thead><tr>{entetes}</tr></thead><tbody>{corps}</tbody></table>"

    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>Administration</title>
<style>body{{font:14px/1.5 "Segoe UI",system-ui,sans-serif;margin:0;padding:24px;background:#f6f7f9;color:#16191d}}
h1{{font-size:18px}} h2{{font-size:15px;margin:26px 0 8px}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e2e5ea;border-radius:8px;overflow:hidden}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #eef0f3;font-size:13px}}
th{{background:#f0f2f5;font-size:11.5px;text-transform:uppercase;color:#6b7280}}
.doux{{color:#6b7280}} a{{color:#1f4e78}}</style></head><body>
<h1>Administration — consultations</h1>
<p><a href="/admin/journal.csv">Télécharger le journal complet (CSV)</a> · <a href="/app">voir le site</a>
 · <a href="/admin/sortir">fermer la session d'administration</a></p>
{table("Visiteurs", ["Nom", "Première visite", "Dernière visite", "Visites", "Requêtes"], visiteurs)}
{table("Activité par jour", ["Jour", "Requêtes", "Visiteurs"], jours)}
{table("Actions", ["Action", "Nombre"], actions)}
{table("Recherches les plus fréquentes", ["Filtres", "Nombre"], recherches)}
{table("200 dernières actions", ["Horodatage", "Visiteur", "IP", "Action", "Détails", "Résultats"], dernieres)}
</body></html>"""


@app.get("/admin/journal.csv")
def journal_csv(request: Request):
    if not admin_connecte(request):
        return RedirectResponse("/admin", 302)
    lignes = lire("SELECT horodatage, visiteur, ip, action, details, resultats, duree_ms, agent"
                  " FROM journal ORDER BY id DESC LIMIT 50000", journal=True)
    entetes = "horodatage;visiteur;ip;action;details;resultats;duree_ms;agent"
    corps = "\n".join(";".join(str(v).replace(";", ",").replace("\n", " ") for v in l.values()) for l in lignes)
    return PlainTextResponse("﻿" + entetes + "\n" + corps, media_type="text/csv",
                             headers={"content-disposition": 'attachment; filename="journal.csv"'})


@app.get("/sante")
def sante():
    if PROBLEME:
        return JSONResponse({"ok": False, "erreur": PROBLEME}, 503)
    try:
        n = lire("SELECT COUNT(*) n FROM marches")[0]["n"]
        return {"ok": True, "marches": n}
    except sqlite3.Error as e:
        return JSONResponse({"ok": False, "erreur": str(e)}, 500)


@app.exception_handler(HTTPException)
def erreurs(request: Request, exc: HTTPException):
    if exc.status_code == 401 and request.url.path.startswith("/api/") and exc.detail == "Session expirée":
        # « session » est le signal que le navigateur attend pour renvoyer vers la page d'accès.
        # Le service de détection, lui, n'a pas de session : son 401 garde son propre message.
        return JSONResponse({"erreur": "session"}, 401)
    if exc.status_code == 401 and exc.headers:
        return Response("Identifiant ou mot de passe incorrect.", 401, headers=exc.headers)
    return JSONResponse({"erreur": exc.detail}, exc.status_code)
