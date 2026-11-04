"""Notre entreprise (étape D) : qualifications, régions, familles, marge minimale, coûts, plan de charge, partenaires.

Tout vit dans copilote_retours.db (saisies humaines, jamais reconstruites). Chaque ligne garde son auteur et sa date ;
chaque changement est aussi écrit dans `entreprise_journal`, qui n'est jamais réécrit.

Unités : les pourcentages sont rangés en fractions (marge 0,05 = 5 %, coût 0,85 = 85 % de l'estimation). La marge est
comptée sur le COÛT : prix ≥ coût × (1 + marge minimale).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import eligibilite, nature, regions

SCHEMA = """
CREATE TABLE IF NOT EXISTS entreprise_profil (
  cle TEXT PRIMARY KEY, valeur TEXT, auteur TEXT NOT NULL, modifie_le TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entreprise_qualifs (
  code TEXT PRIMARY KEY, classe TEXT NOT NULL, auteur TEXT NOT NULL, modifie_le TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entreprise_regions (
  region TEXT PRIMARY KEY, auteur TEXT NOT NULL, modifie_le TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entreprise_familles (
  famille TEXT PRIMARY KEY, auteur TEXT NOT NULL, modifie_le TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entreprise_couts_famille (
  famille TEXT PRIMARY KEY, cout REAL NOT NULL, auteur TEXT NOT NULL, modifie_le TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entreprise_partenaires (
  id INTEGER PRIMARY KEY, nom TEXT NOT NULL, type TEXT NOT NULL,        -- groupement | sous_traitant | fournisseur
  specialite TEXT NOT NULL DEFAULT '', qualifs TEXT NOT NULL DEFAULT '[]',   -- JSON [{code, classe}]
  effet_cout REAL NOT NULL DEFAULT 0, effet_cout_unite TEXT NOT NULL DEFAULT 'pct',   -- pct : part de l'estimation ; dh
  commentaire TEXT NOT NULL DEFAULT '', auteur TEXT NOT NULL, modifie_le TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entreprise_journal (
  id INTEGER PRIMARY KEY, quand TEXT NOT NULL, auteur TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '');
"""
TYPES = ("groupement", "sous_traitant", "fournisseur")
LIBELLE_TYPE = {"groupement": "Groupement", "sous_traitant": "Sous-traitant", "fournisseur": "Fournisseur"}
UNITES = ("pct", "dh")


class ProfilInvalide(ValueError):
    pass


def preparer(ret) -> None:
    ret.executescript(SCHEMA)


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def referentiel() -> dict:
    """Ce qu'on peut choisir : régions, familles de travaux, classes."""
    familles = []
    for code in nature.regles()["ordre"]:
        if code == "batiment":
            familles += [{"code": nature.BATIMENT_NEUF, "libelle": nature.libelle(nature.BATIMENT_NEUF)},
                         {"code": nature.BATIMENT_REHAB, "libelle": nature.libelle(nature.BATIMENT_REHAB)}]
        else:
            familles.append({"code": code, "libelle": nature.libelle(code)})
    return {"regions": [r for r in regions.MOTS if r != "Nord (APDN)"], "familles": familles,
            "classes": list(eligibilite.CLASSES), "types": [{"code": t, "libelle": LIBELLE_TYPE[t]} for t in TYPES]}


# --------------------------------------------------------------------------- lecture

def lire(ret) -> dict:
    """Le profil complet. `configure` est vrai dès qu'un coût par défaut (global) est saisi."""
    preparer(ret)
    scal = {r["cle"]: r for r in ret.execute("SELECT * FROM entreprise_profil")}

    def val(cle, defaut=None):
        r = scal.get(cle)
        return defaut if r is None or r["valeur"] in (None, "") else float(r["valeur"])

    def meta(cle):
        r = scal.get(cle)
        return None if r is None else {"auteur": r["auteur"], "modifie_le": r["modifie_le"]}

    plan = val("plan_charge_max")
    p = {
        "marge_min": val("marge_min"), "cout_defaut": val("cout_defaut"),
        "plan_charge_max": None if plan is None else int(plan),
        "meta": {k: meta(k) for k in ("marge_min", "cout_defaut", "plan_charge_max")},
        "qualifs": [dict(r) for r in ret.execute("SELECT code, classe, auteur, modifie_le FROM entreprise_qualifs ORDER BY code")],
        "regions": [dict(r) for r in ret.execute("SELECT region, auteur, modifie_le FROM entreprise_regions ORDER BY region")],
        "familles": [dict(r) for r in ret.execute("SELECT famille, auteur, modifie_le FROM entreprise_familles ORDER BY famille")],
        "couts_famille": [dict(r) for r in ret.execute("SELECT famille, cout, auteur, modifie_le FROM entreprise_couts_famille ORDER BY famille")],
        "partenaires": [_partenaire(r) for r in ret.execute("SELECT * FROM entreprise_partenaires ORDER BY nom, id")],
    }
    p["configure"] = p["cout_defaut"] is not None
    return p


def _partenaire(r) -> dict:
    d = dict(r)
    d["qualifs"] = json.loads(d["qualifs"] or "[]")
    return d


def qualifs_par_code(profil: dict) -> dict[str, str]:
    return {q["code"]: q["classe"] for q in profil["qualifs"]}


def cout_par_defaut(profil: dict, famille: str | None) -> dict | None:
    """Le coût par défaut (part de l'estimation) pour une famille de travaux : celui de la famille s'il est saisi,
    sinon le coût global. {taux, origine} ou None si rien n'est saisi."""
    par_famille = {c["famille"]: c["cout"] for c in profil["couts_famille"]}
    if famille and famille in par_famille:
        return {"taux": par_famille[famille], "origine": "famille"}
    if profil["cout_defaut"] is not None:
        return {"taux": profil["cout_defaut"], "origine": "global"}
    return None


# --------------------------------------------------------------------------- écriture

def _journal(ret, auteur: str, action: str, details: str = "") -> None:
    ret.execute("INSERT INTO entreprise_journal (quand, auteur, action, details) VALUES (?,?,?,?)",
                (_maintenant(), auteur, action, details[:2000]))


def _fraction(v, nom: str, bas: float, haut: float) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ProfilInvalide(f"{nom} : nombre attendu") from None
    if not (bas <= x <= haut):
        raise ProfilInvalide(f"{nom} : entre {bas} et {haut}")
    return x


def _code(c: str) -> str:
    codes = eligibilite.codes_de(c)
    if len(codes) != 1:
        raise ProfilInvalide(f"qualification illisible : « {c} » (écrire par exemple B.1)")
    return codes[0]


def _classe(c) -> str:
    t = str(c).strip().upper()
    if t not in eligibilite.CLASSES:
        raise ProfilInvalide(f"classe inconnue : « {c} » (S, 1, 2, 3, 4 ou 5)")
    return t


def ecrire(ret, profil: dict, auteur: str) -> dict:
    """Remplace le profil par `profil` (hors partenaires, qui ont leurs propres appels). Les lignes inchangées gardent
    leur auteur et leur date d'origine ; seules celles qui changent prennent l'auteur et la date d'aujourd'hui."""
    preparer(ret)
    marge = _fraction(profil.get("marge_min"), "marge minimale", 0, 2)
    cout = _fraction(profil.get("cout_defaut"), "coût par défaut", 0.1, 3)
    plan = _fraction(profil.get("plan_charge_max"), "plan de charge", 0, 1000)
    quals = {}
    for q in profil.get("qualifs") or []:
        quals[_code(q["code"])] = _classe(q["classe"])
    ref = referentiel()
    regs = sorted({r for r in profil.get("regions") or [] if r in ref["regions"]})
    fams = sorted({f for f in profil.get("familles") or [] if f in {x["code"] for x in ref["familles"]}})
    couts = {}
    for c in profil.get("couts_famille") or []:
        if c["famille"] not in {x["code"] for x in ref["familles"]}:
            raise ProfilInvalide(f"famille inconnue : {c['famille']}")
        v = _fraction(c.get("cout"), "coût de famille", 0.1, 3)
        if v is not None:
            couts[c["famille"]] = v
    maintenant = _maintenant()
    with ret:
        for cle, v in (("marge_min", marge), ("cout_defaut", cout), ("plan_charge_max", plan)):
            ancien = ret.execute("SELECT valeur FROM entreprise_profil WHERE cle = ?", (cle,)).fetchone()
            nouveau = "" if v is None else repr(float(v))
            if ((ancien["valeur"] or "") if ancien else "") != nouveau:
                ret.execute("INSERT INTO entreprise_profil (cle, valeur, auteur, modifie_le) VALUES (?,?,?,?)"
                            " ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur, auteur=excluded.auteur,"
                            " modifie_le=excluded.modifie_le", (cle, nouveau, auteur, maintenant))
                _journal(ret, auteur, f"profil.{cle}", f"{ancien['valeur'] if ancien else '—'} → {nouveau or '—'}")
        _remplacer(ret, auteur, maintenant, "entreprise_qualifs", "code", "classe", quals)
        _remplacer(ret, auteur, maintenant, "entreprise_regions", "region", None, {r: None for r in regs})
        _remplacer(ret, auteur, maintenant, "entreprise_familles", "famille", None, {f: None for f in fams})
        _remplacer(ret, auteur, maintenant, "entreprise_couts_famille", "famille", "cout", couts)
    return lire(ret)


def _remplacer(ret, auteur: str, maintenant: str, table: str, cle: str, colonne: str | None, voulu: dict) -> None:
    """Met la table à l'état voulu : ne touche pas aux lignes inchangées, journalise ajouts, changements, retraits."""
    actuel = {r[0]: (r[1] if colonne else None) for r in
              ret.execute(f"SELECT {cle}{', ' + colonne if colonne else ''} FROM {table}")}
    for k in set(actuel) - set(voulu):
        ret.execute(f"DELETE FROM {table} WHERE {cle} = ?", (k,))
        _journal(ret, auteur, f"{table}.retrait", str(k))
    for k, v in voulu.items():
        if k in actuel and actuel[k] == v:
            continue
        if colonne:
            ret.execute(f"INSERT INTO {table} ({cle}, {colonne}, auteur, modifie_le) VALUES (?,?,?,?)"
                        f" ON CONFLICT({cle}) DO UPDATE SET {colonne}=excluded.{colonne}, auteur=excluded.auteur,"
                        " modifie_le=excluded.modifie_le", (k, v, auteur, maintenant))
        else:
            ret.execute(f"INSERT INTO {table} ({cle}, auteur, modifie_le) VALUES (?,?,?)", (k, auteur, maintenant))
        _journal(ret, auteur, f"{table}.{'ajout' if k not in actuel else 'changement'}", f"{k}" + (f" = {v}" if colonne else ""))


# --------------------------------------------------------------------------- partenaires

def _valider_partenaire(p: dict) -> dict:
    nom = re.sub(r"\s+", " ", str(p.get("nom") or "")).strip()
    if not nom:
        raise ProfilInvalide("nom du partenaire obligatoire")
    if len(nom) > 120:
        raise ProfilInvalide("nom trop long")
    if p.get("type") not in TYPES:
        raise ProfilInvalide("type : groupement, sous_traitant ou fournisseur")
    unite = p.get("effet_cout_unite") or "pct"
    if unite not in UNITES:
        raise ProfilInvalide("unité de l'effet sur le coût : pct ou dh")
    effet = _fraction(p.get("effet_cout") or 0, "effet sur le coût", -3 if unite == "pct" else -1e10,
                      3 if unite == "pct" else 1e10)
    quals = [{"code": _code(q["code"]), "classe": _classe(q["classe"])} for q in p.get("qualifs") or []]
    return {"nom": nom, "type": p["type"], "specialite": str(p.get("specialite") or "").strip()[:200],
            "qualifs": quals, "effet_cout": effet or 0.0, "effet_cout_unite": unite,
            "commentaire": str(p.get("commentaire") or "").strip()[:500]}


def enregistrer_partenaire(ret, p: dict, auteur: str, pid: int | None = None) -> dict:
    preparer(ret)
    v = _valider_partenaire(p)
    maintenant = _maintenant()
    with ret:
        if pid is None:
            cur = ret.execute(
                "INSERT INTO entreprise_partenaires (nom, type, specialite, qualifs, effet_cout, effet_cout_unite,"
                " commentaire, auteur, modifie_le) VALUES (?,?,?,?,?,?,?,?,?)",
                (v["nom"], v["type"], v["specialite"], json.dumps(v["qualifs"]), v["effet_cout"],
                 v["effet_cout_unite"], v["commentaire"], auteur, maintenant))
            pid = cur.lastrowid
            _journal(ret, auteur, "partenaire.ajout", f"{v['nom']} ({v['type']})")
        else:
            if ret.execute("SELECT 1 FROM entreprise_partenaires WHERE id = ?", (pid,)).fetchone() is None:
                raise LookupError("partenaire introuvable")
            ret.execute(
                "UPDATE entreprise_partenaires SET nom=?, type=?, specialite=?, qualifs=?, effet_cout=?,"
                " effet_cout_unite=?, commentaire=?, auteur=?, modifie_le=? WHERE id=?",
                (v["nom"], v["type"], v["specialite"], json.dumps(v["qualifs"]), v["effet_cout"],
                 v["effet_cout_unite"], v["commentaire"], auteur, maintenant, pid))
            _journal(ret, auteur, "partenaire.changement", f"{v['nom']} ({v['type']})")
    return _partenaire(ret.execute("SELECT * FROM entreprise_partenaires WHERE id = ?", (pid,)).fetchone())


def supprimer_partenaire(ret, pid: int, auteur: str) -> None:
    preparer(ret)
    with ret:
        r = ret.execute("SELECT nom FROM entreprise_partenaires WHERE id = ?", (pid,)).fetchone()
        if r is None:
            raise LookupError("partenaire introuvable")
        ret.execute("DELETE FROM entreprise_partenaires WHERE id = ?", (pid,))
        _journal(ret, auteur, "partenaire.retrait", r["nom"])


def journal(ret, limite: int = 50) -> list[dict]:
    preparer(ret)
    return [dict(r) for r in ret.execute("SELECT quand, auteur, action, details FROM entreprise_journal"
                                         " ORDER BY id DESC LIMIT ?", (limite,))]
