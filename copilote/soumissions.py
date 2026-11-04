"""Nos soumissions (étape D) : « J'ai soumissionné » et le bilan.

Une ligne par AO (ou par lot) dans copilote_retours.db, jamais supprimée : prix déposé, coût, scénario (seul,
groupement, sous-traitance), et un instantané de la décision affichée au moment du dépôt. Le bilan se fait quand le PV
arrive (copilote/suivi_page.py).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from . import entreprise

SCHEMA = """
CREATE TABLE IF NOT EXISTS soumissions (
  id INTEGER PRIMARY KEY,
  ref_annonce TEXT NOT NULL, lot TEXT NOT NULL DEFAULT '',
  prix_depose REAL NOT NULL, cout REAL NOT NULL, estimation REAL NOT NULL,
  scenario TEXT NOT NULL, partenaire TEXT NOT NULL DEFAULT '',
  date_limite TEXT, objet TEXT, acheteur TEXT, rejeu INTEGER NOT NULL DEFAULT 0,
  decision TEXT NOT NULL DEFAULT '', instantane TEXT NOT NULL DEFAULT '{}',   -- JSON : prix recommandé, chance, gain, raisons
  commentaire TEXT NOT NULL DEFAULT '',
  auteur TEXT NOT NULL, soumis_le TEXT NOT NULL, modifie_le TEXT NOT NULL,
  UNIQUE (ref_annonce, lot));
"""
SCENARIOS = ("seul", "groupement", "sous_traitance")


class SoumissionInvalide(ValueError):
    pass


def preparer(ret) -> None:
    entreprise.preparer(ret)
    ret.executescript(SCHEMA)


def _nombre(v, nom: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise SoumissionInvalide(f"{nom} : nombre attendu") from None
    if not (x > 0 and x < 1e13):
        raise SoumissionInvalide(f"{nom} : doit être positif")
    return x


def enregistrer(ret, d: dict, auteur: str) -> dict:
    """Crée ou met à jour la soumission de cet AO (le dépôt peut être corrigé : l'historique est dans le journal)."""
    preparer(ret)
    ref, lot = str(d.get("ref_annonce") or "").strip(), str(d.get("lot") or "").strip()
    if not ref:
        raise SoumissionInvalide("AO obligatoire")
    prix, cout, est = _nombre(d.get("prix_depose"), "prix déposé"), _nombre(d.get("cout"), "coût"), _nombre(d.get("estimation"), "estimation")
    if d.get("scenario") not in SCENARIOS:
        raise SoumissionInvalide("scénario : seul, groupement ou sous_traitance")
    maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
    instantane = json.dumps(d.get("instantane") or {}, ensure_ascii=False, separators=(",", ":"))[:20000]
    avant = ret.execute("SELECT id FROM soumissions WHERE ref_annonce = ? AND lot = ?", (ref, lot)).fetchone()
    with ret:
        ret.execute(
            "INSERT INTO soumissions (ref_annonce, lot, prix_depose, cout, estimation, scenario, partenaire, date_limite,"
            " objet, acheteur, rejeu, decision, instantane, commentaire, auteur, soumis_le, modifie_le)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(ref_annonce, lot) DO UPDATE SET prix_depose=excluded.prix_depose, cout=excluded.cout,"
            " estimation=excluded.estimation, scenario=excluded.scenario, partenaire=excluded.partenaire,"
            " decision=excluded.decision, instantane=excluded.instantane, commentaire=excluded.commentaire,"
            " auteur=excluded.auteur, modifie_le=excluded.modifie_le",
            (ref, lot, prix, cout, est, d["scenario"], str(d.get("partenaire") or "")[:120], d.get("date_limite"),
             (d.get("objet") or "")[:500], (d.get("acheteur") or "")[:200], 1 if ref.startswith("REJEU-") else 0,
             str(d.get("decision") or "")[:20], instantane, str(d.get("commentaire") or "")[:500], auteur,
             maintenant, maintenant))
        entreprise._journal(ret, auteur, "soumission.changement" if avant else "soumission.ajout",
                            f"{ref}{':' + lot if lot else ''} prix {prix:.0f} coût {cout:.0f} ({d['scenario']})")
    return lire(ret, ref, lot)


def _ligne(r) -> dict:
    d = dict(r)
    d["instantane"] = json.loads(d["instantane"] or "{}")
    d["rejeu"] = bool(d["rejeu"])
    return d


def lire(ret, ref: str, lot: str | None) -> dict | None:
    preparer(ret)
    r = ret.execute("SELECT * FROM soumissions WHERE ref_annonce = ? AND lot = ?", (ref, lot or "")).fetchone()
    return None if r is None else _ligne(r)


def lister(ret) -> list[dict]:
    preparer(ret)
    return [_ligne(r) for r in ret.execute("SELECT * FROM soumissions ORDER BY soumis_le DESC, id DESC")]


def jours_restants(date_limite: str | None, aujourdhui: date | None = None) -> int | None:
    try:
        d = date.fromisoformat((date_limite or "")[:10])
    except ValueError:
        return None
    return (d - (aujourdhui or date.today())).days


def en_cours(ret, aujourdhui: date | None = None) -> int:
    """Nos soumissions (hors AO de test) dont la date d'ouverture des plis n'est pas encore passée."""
    jour = (aujourdhui or date.today()).isoformat()
    preparer(ret)
    return ret.execute("SELECT COUNT(*) FROM soumissions WHERE rejeu = 0 AND substr(date_limite, 1, 10) >= ?", (jour,)).fetchone()[0]
