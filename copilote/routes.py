"""Routes du copilote, branchées sur serveur.py par UNE inclusion :

    app.include_router(creer_routeur(SECRET, BASE, AO_BASE))

Tout ce qui est sous /copilote et /api/copilote passe par acces.garde, sauf /copilote/entrer.
Les routes protégées sont déclarées sur `protege`, dont la dépendance s'applique d'office : une
route ajoutée plus tard ne peut pas oublier l'authentification.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import acces, bases, config

STYLE = """<style>
:root{--fond:#f3f4f6;--surface:#fff;--bord:#dde1e7;--texte:#111827;--doux:#555e6c;--accent:#1f4f8f;
 --sur-accent:#fff;--rouge:#b3261e;--vert:#0f6a3e;--focus:#2563eb;color-scheme:light;
 --police:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;
 --mono:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--fond:#0d1117;--surface:#161b22;
 --bord:#2b333e;--texte:#e8ebef;--doux:#9ca6b4;--accent:#7fb2f0;--sur-accent:#0b1320;--rouge:#f28b82;
 --vert:#5cc98f;--focus:#7cb0ff;color-scheme:dark}}
:root[data-theme="dark"]{--fond:#0d1117;--surface:#161b22;--bord:#2b333e;--texte:#e8ebef;--doux:#9ca6b4;
 --accent:#7fb2f0;--sur-accent:#0b1320;--rouge:#f28b82;--vert:#5cc98f;--focus:#7cb0ff;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--fond);color:var(--texte);
 font:15px/1.6 var(--police);padding:20px}
main{background:var(--surface);border:1px solid var(--bord);border-radius:14px;padding:28px;
 width:min(520px,100%)}
h1{font-size:1.25rem;margin:0 0 6px}p{margin:0 0 16px;color:var(--doux)}
label{display:block;font-size:.8125rem;color:var(--doux);margin-bottom:6px}
input{width:100%;font:inherit;padding:11px 12px;border:1px solid var(--bord);border-radius:8px;
 background:var(--fond);color:var(--texte);margin-bottom:16px}
input:focus-visible,button:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
button{width:100%;font:inherit;font-weight:600;padding:11px;border:none;border-radius:8px;
 background:var(--accent);color:var(--sur-accent);cursor:pointer}
.erreur{color:var(--rouge);font-size:.875rem;margin-bottom:14px}
table{border-collapse:collapse;width:100%;font-size:.875rem}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bord)}
th{color:var(--doux);font-weight:600}.mono{font-family:var(--mono)}
</style>
<script>try{const t=localStorage.getItem('pv-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}</script>"""

PAGE_ENTRER = ("""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<title>Copilote — accès</title>""" + STYLE + """</head><body><main>
<h1>Copilote de soumission</h1><p>Accès réservé : entrez votre code personnel.</p>{erreur}
<form method="post" action="/copilote/entrer">
<label for="code">Code d'accès</label>
<input id="code" name="code" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Entrer</button></form></main></body></html>""")

PAGE_ACCUEIL = ("""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<title>Copilote de soumission</title>""" + STYLE + """</head><body><main>
<h1>Copilote de soumission</h1>
<p>Socle en place — connecté en tant que <strong id="auteur">…</strong>.</p>
<table><thead><tr><th>Base</th><th>Accès</th><th>Taille</th><th>Contenu</th></tr></thead>
<tbody id="bases"><tr><td colspan="4">Chargement…</td></tr></tbody></table>
</main><script>
fetch('/api/copilote/etat').then(r => r.json()).then(e => {
  document.getElementById('auteur').textContent = e.auteur;
  const ligne = (nom, b) => { const tr = document.createElement('tr');
    [nom, b.mode, b.existe ? (b.taille_mo + ' Mo') : 'absente',
     b.contenu == null ? '—' : b.contenu].forEach(t => {
      const td = document.createElement('td'); td.textContent = t; tr.appendChild(td); }); return tr; };
  const corps = document.getElementById('bases'); corps.replaceChildren();
  Object.entries(e.bases).forEach(([nom, b]) => corps.appendChild(ligne(nom, b)));
}).catch(() => { document.getElementById('auteur').textContent = 'erreur de chargement'; });
</script></body></html>""")


def _etat_base(chemin: Path, mode: str, requete: str | None, ouvrir) -> dict:
    """Existence, taille et un compte indicatif, sans jamais lever : c'est un tableau de bord."""
    etat = {"mode": mode, "existe": Path(chemin).exists(), "taille_mo": None, "contenu": None}
    if not etat["existe"]:
        return etat
    etat["taille_mo"] = round(Path(chemin).stat().st_size / 1e6, 1)
    if requete:
        try:
            db = ouvrir()
            try:
                etat["contenu"] = f"{db.execute(requete).fetchone()[0]}"
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            etat["contenu"] = "illisible"
    return etat


def creer_routeur(secret: bytes, base: Path | str, ao_base: Path | str) -> APIRouter:
    """Le routeur du copilote. `secret` est celui de serveur.py, `base` et `ao_base` ses deux bases."""
    bases.definir_sources(base, ao_base)
    garde = acces.garde(secret)

    public = APIRouter()
    protege = APIRouter(dependencies=[Depends(garde)])

    @public.get("/copilote/entrer", response_class=HTMLResponse)
    def entrer_page(request: Request, e: str = ""):
        if not acces.session_visiteur(secret, request):
            return RedirectResponse("/", 302)
        if acces.session_copilote(secret, request):
            return RedirectResponse("/copilote", 302)
        erreur = '<p class="erreur">Code refusé.</p>' if e else ""
        return HTMLResponse(PAGE_ENTRER.replace("{erreur}", erreur),
                            headers={"Cache-Control": "no-store"})

    @public.post("/copilote/entrer")
    def entrer(request: Request, code: str = Form(...)):
        if not acces.session_visiteur(secret, request):
            return RedirectResponse("/", 302)
        if not acces.comptes():
            return PlainTextResponse("COPILOTE_ACCES n'est pas défini sur le serveur.", 503)
        ip = acces.adresse(request)
        if acces.bloque(ip):
            return PlainTextResponse("Trop d'essais, réessayez dans quelques minutes.", 429)
        nom = acces.identifier(code)
        if nom is None:
            acces.noter_echec(ip)
            return RedirectResponse("/copilote/entrer?e=1", 303)
        reponse = RedirectResponse("/copilote", 303)
        reponse.set_cookie(acces.COOKIE_COPILOTE, acces.cookie_copilote(secret, nom),
                           max_age=config.DUREE_SESSION, httponly=True, samesite="lax",
                           secure=request.url.scheme == "https")
        return reponse

    @protege.get("/copilote", response_class=HTMLResponse)
    def accueil():
        return HTMLResponse(PAGE_ACCUEIL, headers={"Cache-Control": "no-store"})

    @protege.get("/api/copilote/etat")
    def etat(session: dict = Depends(garde)):
        # Ouvrir les deux bases du copilote les crée si elles n'existent pas encore : c'est ici,
        # et non au démarrage du serveur, qu'on vérifie que le volume accepte l'écriture.
        for ouvrir in (bases.calcul, bases.retours):
            try:
                ouvrir().close()
            except (sqlite3.Error, OSError):
                pass
        return JSONResponse({
            "auteur": session["nom"],
            "bases": {
                "pv_travaux.db": _etat_base(bases.chemin_historique(), "lecture seule",
                                            "SELECT COUNT(*) || ' marchés' FROM marches WHERE principale = 1",
                                            bases.historique),
                "ao_ouverts.db": _etat_base(bases.chemin_ao(), "lecture seule",
                                            "SELECT COUNT(*) || ' appels' FROM appels_ouverts",
                                            bases.ao_ouverts),
                "copilote_calcul.db": _etat_base(config.COPILOTE_CALCUL, "lecture-écriture",
                                                 "SELECT COUNT(*) || ' tables'"
                                                 " FROM sqlite_master WHERE type = 'table'",
                                                 bases.calcul),
                "copilote_retours.db": _etat_base(config.COPILOTE_RETOURS, "lecture-écriture",
                                                  "SELECT COUNT(*) || ' tables'"
                                                  " FROM sqlite_master WHERE type = 'table'",
                                                  bases.retours),
            },
        }, headers={"Cache-Control": "no-store"})

    routeur = APIRouter()
    routeur.include_router(public)
    routeur.include_router(protege)
    return routeur
