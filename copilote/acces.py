"""Accès au copilote : le cookie visiteur ne suffit pas, il faut aussi un code dédié.

COPILOTE_ACCES = "nom:code,nom:code". Le code ouvre un cookie signé à part (« pvc »), signé avec
une clé dérivée de SECRET : un cookie visiteur ne peut jamais passer pour un cookie copilote.
Le nom associé au code est celui qui signe chaque avis enregistré.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from . import config

COOKIE_VISITEUR = "pv"
COOKIE_COPILOTE = "pvc"

# Essais de code ratés par adresse : 8 par 10 minutes, ensuite on refuse même le bon code.
ESSAIS_MAX, ESSAIS_FENETRE = 8, 600
_echecs: dict[str, deque] = defaultdict(deque)


def comptes() -> dict[str, str]:
    """{nom: code} d'après COPILOTE_ACCES, lu à chaque appel. Une entrée mal formée est ignorée."""
    out: dict[str, str] = {}
    for entree in os.environ.get("COPILOTE_ACCES", "").split(","):
        nom, deux_points, code = entree.partition(":")
        nom, code = nom.strip(), code.strip()
        if deux_points and nom and code and nom not in out:
            out[nom] = code
    return out


def identifier(code: str) -> str | None:
    """Le nom dont c'est le code, ou None. Tous les comptes sont comparés, à temps constant."""
    trouve = None
    for nom, attendu in comptes().items():
        if hmac.compare_digest(code.encode(), attendu.encode()) and trouve is None:
            trouve = nom
    return trouve


# --------------------------------------------------------------- cookies signés

def _cle(secret: bytes, contexte: bytes) -> bytes:
    return hmac.new(secret, contexte, hashlib.sha256).digest() if contexte else secret


def signer(secret: bytes, donnees: dict, contexte: bytes = b"") -> str:
    """Même format que serveur.signer (charge.sceau) ; `contexte` dérive une clé distincte."""
    charge = base64.urlsafe_b64encode(json.dumps(donnees).encode()).decode().rstrip("=")
    sceau = hmac.new(_cle(secret, contexte), charge.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{charge}.{sceau}"


def verifier(secret: bytes, jeton: str | None, contexte: bytes = b"") -> dict | None:
    if not jeton or "." not in jeton:
        return None
    charge, _, sceau = jeton.rpartition(".")
    attendu = hmac.new(_cle(secret, contexte), charge.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sceau, attendu):
        return None
    try:
        d = json.loads(base64.urlsafe_b64decode(charge + "=" * (-len(charge) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict) or time.time() - d.get("t", 0) > config.DUREE_SESSION:
        return None
    return d


CONTEXTE_COPILOTE = b"copilote"


def cookie_copilote(secret: bytes, nom: str) -> str:
    return signer(secret, {"c": nom, "t": time.time()}, CONTEXTE_COPILOTE)


def session_visiteur(secret: bytes, request: Request) -> dict | None:
    d = verifier(secret, request.cookies.get(COOKIE_VISITEUR))
    return d if d and d.get("n") else None


def session_copilote(secret: bytes, request: Request) -> dict | None:
    """{"nom": …} si le visiteur est connu ET porte un cookie copilote encore valable.

    Un compte retiré de COPILOTE_ACCES perd son accès tout de suite, sans attendre l'expiration.
    """
    visiteur = session_visiteur(secret, request)
    d = verifier(secret, request.cookies.get(COOKIE_COPILOTE), CONTEXTE_COPILOTE)
    if not visiteur or not d or d.get("c") not in comptes():
        return None
    return {"nom": d["c"], "visiteur": visiteur["n"]}


# --------------------------------------------------------------- dépendance FastAPI

def adresse(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def bloque(ip: str) -> bool:
    f = _echecs[ip]
    while f and time.time() - f[0] > ESSAIS_FENETRE:
        f.popleft()
    return len(f) >= ESSAIS_MAX


def noter_echec(ip: str) -> None:
    _echecs[ip].append(time.time())


def garde(secret: bytes):
    """Dépendance de toutes les routes protégées : 401 pour l'API, redirection pour les pages."""
    def exiger(request: Request) -> dict:
        s = session_copilote(secret, request)
        if s:
            return s
        if request.url.path.startswith("/api/"):
            raise HTTPException(401, "Accès au copilote requis")
        cible = "/copilote/entrer" if session_visiteur(secret, request) else "/"
        raise HTTPException(302, "Accès au copilote requis", headers={"Location": cible})
    return exiger
