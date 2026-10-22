"""Routes du copilote, branchées sur serveur.py par UNE inclusion :

    app.include_router(creer_routeur(SECRET, BASE, AO_BASE))

Tout ce qui est sous /copilote et /api/copilote passe par acces.garde, sauf /copilote/entrer.
Les routes protégées sont déclarées sur `protege`, dont la dépendance s'applique d'office : une
route ajoutée plus tard ne peut pas oublier l'authentification.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi import Path as ParamChemin
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import acces, acheteurs, api_groupes, api_similaires, bases, config, similarite

STATIQUE = Path(__file__).resolve().parent.parent / "statique"
SANS_CACHE = {"Cache-Control": "no-store"}


class AvisEntree(BaseModel):
    """Un avis sur une paire (cle_a + cle_b) ou sur tout un groupe (groupe). L'auteur n'en fait pas
    partie : il vient toujours de la session."""
    reponse: Literal["oui", "non", "nsp"]
    commentaire: str = Field("", max_length=api_groupes.MAX_COMMENTAIRE)
    groupe: int | None = None
    cle_a: str | None = Field(None, max_length=200)
    cle_b: str | None = Field(None, max_length=200)


class CorrespondanceEntree(BaseModel):
    """Validation, refus ou saisie à la main d'une correspondance entre l'acheteur d'un AO et l'historique.
    L'auteur n'en fait pas partie : il vient de la session."""
    cle_ao: str = Field(..., min_length=1, max_length=200)
    cle_historique: str = Field(..., min_length=1, max_length=200)
    statut: Literal["proposee", "validee", "refusee"]
    commentaire: str = Field("", max_length=500)
    nom_ao: str | None = Field(None, max_length=300)


def page_statique(request: Request, nom: str) -> Response:
    """Même règle que serveur.page_statique : revalidation à chaque visite, 304 si rien n'a changé."""
    chemin = STATIQUE / nom
    st = chemin.stat()
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    entetes = {"Cache-Control": "no-cache", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=entetes)
    return FileResponse(chemin, headers=entetes)

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

    # ------------------------------------------------------------------ étape 3 : groupes
    @protege.get("/copilote/groupes")
    def page_groupes(request: Request):
        return page_statique(request, "copilote_groupes.html")

    @protege.get("/api/copilote/moi")
    def moi(session: dict = Depends(garde)):
        """Réponse légère pour la barre du haut : 200 si le visiteur a l'accès copilote, 401 sinon."""
        return JSONResponse({"nom": session["nom"]}, headers=SANS_CACHE)

    @protege.get("/api/copilote/groupes")
    def liste_groupes(q: str = Query("", max_length=100), tri: Literal[api_groupes.TRIS] = "score"):
        with closing(bases.calcul()) as calc, closing(bases.retours()) as ret:
            return JSONResponse(api_groupes.lister(calc, ret, q, tri), headers=SANS_CACHE)

    @protege.get("/api/copilote/famille/{empreinte}")
    def une_famille(empreinte: str = ParamChemin(pattern=r"^[0-9a-f]{8,64}$")):
        with closing(bases.calcul()) as calc, closing(bases.retours()) as ret:
            f = api_groupes.famille(calc, ret, empreinte)
        if f is None:
            raise HTTPException(404, "Famille introuvable")
        return JSONResponse(f, headers=SANS_CACHE)

    @protege.get("/api/copilote/groupe/{gid}")
    def un_groupe(gid: int):
        with closing(bases.calcul()) as calc, closing(bases.retours()) as ret:
            f = api_groupes.fiche(calc, ret, gid)
        if f is None:
            raise HTTPException(404, "Groupe introuvable")
        return JSONResponse(f, headers=SANS_CACHE)

    @protege.get("/api/copilote/avis")
    def liste_avis(limite: int = Query(50, ge=1, le=200)):
        with closing(bases.calcul()) as calc, closing(bases.retours()) as ret:
            return JSONResponse({"avis": api_groupes.derniers_avis(calc, ret, limite)}, headers=SANS_CACHE)

    @protege.post("/api/copilote/avis")
    def donner_avis(avis: AvisEntree, session: dict = Depends(garde)):
        try:
            with closing(bases.calcul()) as calc, closing(bases.retours()) as ret:
                r = api_groupes.enregistrer_avis(calc, ret, session["nom"], avis.reponse, avis.commentaire,
                                                 avis.groupe, avis.cle_a, avis.cle_b)
        except api_groupes.ErreurAvis as e:
            raise HTTPException(e.statut, str(e))
        return JSONResponse(r, headers=SANS_CACHE)

    # ------------------------------------------------------------------ étape 4 : marchés similaires
    @protege.get("/api/copilote/similaires/{ref_annonce}")
    def similaires(ref_annonce: str, lot: str | None = Query(None, max_length=40),
                   poids: str | None = Query(None, max_length=300)):
        """Les 50 marchés les plus proches d'un AO ouvert. `poids` = « acheteur:50,montant:5 » (essais)."""
        try:
            with closing(bases.calcul()) as calc, closing(bases.retours()) as ret, closing(bases.ao_ouverts()) as ao:
                r = api_similaires.analyser(ref_annonce, lot, poids, ouvrir_historique=bases.historique,
                                            chemin_historique=bases.chemin_historique(), ao=ao, calc=calc, ret=ret)
        except FileNotFoundError as e:
            raise HTTPException(503, str(e))
        except api_similaires.ErreurAnalyse as e:
            return JSONResponse({"detail": str(e), **e.extra}, status_code=e.statut, headers=SANS_CACHE)
        return JSONResponse(r, headers=SANS_CACHE)

    @protege.get("/api/copilote/acheteurs")
    def chercher_acheteurs(q: str = Query("", max_length=100), limite: int = Query(20, ge=1, le=50)):
        """Cherche un acheteur de l'historique par son nom (pour saisir une correspondance à la main)."""
        mots = acheteurs.noyau(q).split()
        with closing(bases.historique()) as hist:
            connus = api_similaires._connus(hist, similarite._cle_fichier(bases.chemin_historique()))
        trouves = [c for c in connus if mots and all(m in acheteurs.plat(c["nom"]) for m in mots)]
        trouves.sort(key=lambda c: -c["marches"])
        return JSONResponse({"acheteurs": [{"cle": c["cle"], "nom": c["nom"], "marches": c["marches"],
                                            "type": c["type"]} for c in trouves[:limite]]}, headers=SANS_CACHE)

    @protege.get("/api/copilote/correspondances")
    def liste_correspondances(statut: Literal["proposee", "validee", "refusee"] | None = None,
                              cle_ao: str | None = Query(None, max_length=200)):
        with closing(bases.retours()) as ret:
            return JSONResponse({"correspondances": acheteurs.lister(ret, statut, cle_ao)}, headers=SANS_CACHE)

    @protege.post("/api/copilote/correspondance")
    def decider_correspondance(c: CorrespondanceEntree, session: dict = Depends(garde)):
        with closing(bases.historique()) as hist:
            connus = {x["cle"]: x for x in api_similaires._connus(hist, similarite._cle_fichier(
                bases.chemin_historique()))}
        if c.cle_historique not in connus:
            raise HTTPException(404, "Cet acheteur n'existe pas dans l'historique")
        with closing(bases.retours()) as ret:
            acheteurs.decider(ret, c.cle_ao, c.cle_historique, c.statut, session["nom"], c.commentaire,
                              c.nom_ao, connus[c.cle_historique]["nom"])
            ligne = acheteurs.lister(ret, cle_ao=c.cle_ao)
        return JSONResponse({"correspondances": ligne}, headers=SANS_CACHE)

    def prechauffer() -> None:
        """Au démarrage : recharge (ou calcule) l'index de similarité en arrière-plan. Ne bloque ni ne
        fait échouer le serveur si l'historique n'est pas encore là."""
        def travail():
            try:
                with closing(bases.calcul()) as calc:
                    similarite.moteur(bases.historique, bases.chemin_historique(), calc)
            except Exception as e:                              # noqa: BLE001 : jamais fatal au démarrage
                print(f"[copilote] index de similarité non préchargé : {e}", flush=True)
        import threading
        threading.Thread(target=travail, name="copilote-prechauffage", daemon=True).start()

    routeur = APIRouter()
    routeur.add_event_handler("startup", prechauffer)
    routeur.include_router(public)
    routeur.include_router(protege)
    return routeur
