// Étape D : la décision GO / À ÉTUDIER / NO-GO, dans le navigateur.
// Copie À L'IDENTIQUE de copilote/decision.py et de copilote/eligibilite.py (mêmes noms, mêmes résultats) :
// tests/cas_decision.json, tests/lancer_decision.js, tests/test_decision.py. Lire l'en-tête de decision.py.
(function (racine) {
  'use strict';
  var GO = 'GO', ETUDIER = 'A_ETUDIER', NOGO = 'NO_GO';
  var RANG = { GO: 0, A_ETUDIER: 1, NO_GO: 2 };
  var EPS = 1e-12;
  var CODE = /(?<![A-Za-z0-9])([A-Za-z])\s*[.\-]\s*(\d{1,2})(?!\d)/g;
  var ALTERNATIVE = /(?<![A-Za-zÀ-ÿ])ou(?![A-Za-zÀ-ÿ])/i;

  // ------------------------------------------------------------------ éligibilité (eligibilite.py)
  function rangClasse(classe) {
    var t = String(classe == null ? '' : classe).trim().toUpperCase();
    if (t === 'S' || t === 'CLASSE S') return 0;
    var m = /(\d+)/.exec(t);
    if (!m) return null;
    var n = parseInt(m[1], 10);
    return n >= 1 && n <= 5 ? n : null;
  }
  function suffit(nous, exige) { return nous != null && exige != null && nous <= exige; }
  function codesDe(libelle) {
    var out = [], m, re = new RegExp(CODE.source, 'g');
    var t = libelle || '';
    while ((m = re.exec(t)) !== null) out.push(m[1].toUpperCase() + '.' + parseInt(m[2], 10));
    return out;
  }
  function exigencesDe(lignes) {
    var sortie = [];
    lignes.forEach(function (l) {
      var libelle = l[0], classe = l[1], codes = codesDe(libelle);
      var alternative = ALTERNATIVE.test(libelle || '') && codes.length > 1;
      var rang = rangClasse(classe), txt = classe == null ? null : String(classe).trim();
      if (!codes.length) { sortie.push({ code: null, classe: txt, rang: rang, alternative: false, brut: libelle }); return; }
      codes.forEach(function (c) { sortie.push({ code: c, classe: txt, rang: rang, alternative: alternative, brut: libelle }); });
    });
    return sortie;
  }
  function couvre(p, e) {
    if (p.type !== 'groupement') return false;
    var qs = p.qualifs || [];
    for (var i = 0; i < qs.length; i++) if (qs[i].code === e.code && suffit(rangClasse(qs[i].classe), e.rang)) return true;
    return false;
  }
  function verifier(exigences, nosQuals, partenaires) {
    partenaires = partenaires || [];
    var lignes = [], incertain = false, manque = 0;
    exigences.forEach(function (e) {
      var l = {}; Object.keys(e).forEach(function (k) { l[k] = e[k]; });
      l.nous = null; l.etat = null; l.couvert_par = null; l.apporteraient = [];
      if (e.code == null || e.rang == null) { l.etat = 'inconnu'; incertain = true; }
      else {
        var n = Object.prototype.hasOwnProperty.call(nosQuals, e.code) ? nosQuals[e.code] : null;
        var nr = n != null ? rangClasse(n) : null;
        l.nous = n;
        if (n == null) l.etat = 'absent';
        else if (nr == null || !suffit(nr, e.rang)) l.etat = 'classe';
        else l.etat = 'ok';
        if (l.etat === 'absent' || l.etat === 'classe') {
          for (var i = 0; i < partenaires.length; i++) if (couvre(partenaires[i], e)) { l.couvert_par = partenaires[i].nom; break; }
        }
      }
      if (e.alternative) {
        incertain = true;
        if ((l.etat === 'absent' || l.etat === 'classe') && !l.couvert_par) l.etat = 'alternative';
      }
      if ((l.etat === 'absent' || l.etat === 'classe') && !l.couvert_par) manque++;
      lignes.push(l);
    });
    return { statut: manque ? 'non_eligible' : (incertain ? 'a_etudier' : 'eligible'), lignes: lignes, manque: manque };
  }
  function apports(exigences, nosQuals, partenaires) {
    var seul = verifier(exigences, nosQuals), sortie = [];
    seul.lignes.forEach(function (l) {
      if (l.etat === 'absent' || l.etat === 'classe')
        sortie.push({ code: l.code, classe: l.classe, partenaires: partenaires.filter(function (p) { return couvre(p, l); }).map(function (p) { return p.nom; }) });
    });
    return sortie;
  }

  // ------------------------------------------------------------------ la courbe (decision.py)
  function lisser(v, k) {
    var n = v.length, out = [];
    for (var j = 0; j < n; j++) {
      var a = Math.max(0, j - k), b = Math.min(n - 1, j + k), s = 0.0;
      for (var i = a; i <= b; i++) s += v[i];
      out.push(s / (b - a + 1));
    }
    return out;
  }
  function indiceDe(xs, x) {
    var j = Math.floor((x - xs[0]) / (xs[1] - xs[0]) + 0.5);
    return Math.min(xs.length - 1, Math.max(0, j));
  }
  function autour(lisse, jm, j0, j1, seuil) {
    var a = jm, b = jm;
    while (a > j0 && lisse[a - 1] >= seuil) a--;
    while (b < j1 && lisse[b + 1] >= seuil) b++;
    return [a, b];
  }
  function point(xs, chance, c, E, j) {
    var x = xs[j];
    return { x: x, indice: j, chance: chance[j], marge: x - c, marge_dh: (x - c) * E,
      marge_pct: c > 0 ? (x - c) / c : null, gain: (x - c) * chance[j], gain_dh: (x - c) * chance[j] * E };
  }
  function cleDe(x) { return String(x); }
  // La « règle simple apprise sur 2025 » (copie de decision.regle_simple).
  function regleSimple(xs, chance, c, E, xmin, xmax, reg, prevu) {
    var rs = reg.regle_simple, k, i;
    if (!rs) return null;
    var tranche = rs.tout;
    if (prevu != null) {
      tranche = rs.tranches[rs.tranches.length - 1];
      for (i = 0; i < rs.tranches.length; i++) {
        if (rs.tranches[i].borne == null || prevu < rs.tranches[i].borne) { tranche = rs.tranches[i]; break; }
      }
    }
    var meilleur = null;
    for (k = 0; k < rs.taux.length; k++) {
      var xk = rs.taux[k];
      if (xk < xmin - EPS || xk > xmax + EPS) continue;
      var g = (xk - c) * tranche.w[k];
      if (meilleur === null || g > meilleur[0]) meilleur = [g, xk];
    }
    if (meilleur === null) return null;
    var pt = point(xs, chance, c, E, indiceDe(xs, meilleur[1]));
    pt.cle = 'fixe'; pt.libelle = tranche.libelle; pt.appris_sur = rs.appris_sur; pt.marches_appris = tranche.marches;
    return pt;
  }
  function evaluer(xs, chance, estimation, coutDh, margeMin, reg, prevu) {
    var p = reg.plage, n = xs.length, c = coutDh / estimation, j;
    var xmin = Math.max(p.plancher, c * (1 + margeMin)), xmax = p.plafond;
    var j0 = n, j1 = -1;
    for (j = 0; j < n; j++) if (xs[j] >= xmin - EPS) { j0 = j; break; }
    for (j = 0; j < n; j++) if (xs[j] <= xmax + EPS) j1 = j;
    var ev = { cout: c, cout_dh: coutDh, estimation: estimation, marge_min: margeMin, xmin: xmin, xmax: xmax,
      vide: j0 > j1, reco: null, zone: null, meilleur_gain: null, meilleure_chance: null, tableau: [] };
    var gains = [];
    for (j = 0; j < n; j++) gains.push((xs[j] - c) * chance[j]);
    if (!ev.vide) {
      var lisse = lisser(gains, reg.lissage), jb = j0, jm = j0;
      for (j = j0 + 1; j <= j1; j++) if (gains[j] > gains[jb]) jb = j;
      ev.meilleur_gain = point(xs, chance, c, estimation, jb);
      for (j = j0 + 1; j <= j1; j++) if (lisse[j] > lisse[jm]) jm = j;
      if (lisse[jm] > 0) {
        ev.reco = point(xs, chance, c, estimation, jm); ev.reco.gain_lisse = lisse[jm];
        var ab = autour(lisse, jm, j0, j1, reg.zone_part * lisse[jm]);
        ev.zone = { de: xs[ab[0]], a: xs[ab[1]] };
      }
    }
    var jp = n - 1;
    for (j = 0; j < n; j++) if (xs[j] >= p.plancher - EPS) { jp = j; break; }
    var ls = lisser(chance, reg.lissage), jc = jp;
    for (j = jp + 1; j < n; j++) if (ls[j] > ls[jc]) jc = j;
    var mc = point(xs, chance, c, estimation, jc);
    mc.sous_marge = xs[jc] < xmin - EPS;
    ev.meilleure_chance = mc;
    var lignes = reg.table_choix.map(function (x) { return { cle: cleDe(x), x: x }; });
    if (ev.reco) lignes.push({ cle: 'reco', x: ev.reco.x });
    lignes.forEach(function (l) {
      var pt = point(xs, chance, c, estimation, indiceDe(xs, l.x));
      pt.cle = l.cle;
      pt.hors_plage = pt.x < p.plancher - EPS || pt.x > xmax + EPS;
      pt.sous_marge = pt.x < c * (1 + margeMin) - EPS;
      pt.alerte = pt.x < p.alerte - EPS;
      ev.tableau.push(pt);
    });
    if (!ev.vide) {
      var fixe = regleSimple(xs, chance, c, estimation, xmin, xmax, reg, prevu == null ? null : prevu);
      if (fixe) {
        fixe.hors_plage = false; fixe.sous_marge = false; fixe.alerte = fixe.x < p.alerte - EPS;
        ev.tableau.push(fixe);
      }
    }
    var jp1 = indiceDe(xs, p.plancher), ju = indiceDe(xs, 1.0);
    ev.chance_plancher = { x: xs[jp1], chance: chance[jp1] };
    ev.chance_un = { x: xs[ju], chance: chance[ju] };
    var jx = jp1;
    for (j = jp1 + 1; j < n; j++) if (chance[j] > chance[jx]) jx = j;
    ev.chance_max = { x: xs[jx], chance: chance[jx] };
    return ev;
  }

  // ------------------------------------------------------------------ la décision
  function listeManques(elig) {
    if (!elig) return '';
    return elig.lignes.filter(function (l) { return (l.etat === 'absent' || l.etat === 'classe') && !l.couvert_par; })
      .map(function (l) { return l.code + ' classe ' + l.classe; }).join(', ');
  }
  function listeIncertitudes(elig) {
    if (!elig) return '';
    var out = [];
    elig.lignes.forEach(function (l) {
      if (l.etat === 'inconnu') out.push('qualification non reconnue (' + String(l.brut || 'sans libellé').trim() + ')');
      else if (l.alternative) out.push('alternative « ou » (' + l.code + ')');
    });
    return out.filter(function (v, i) { return out.indexOf(v) === i; }).join(', ');
  }
  function decider(ev, elig, signaux, reg) {
    var s = signaux || {}, raisons = [], niveau = GO, E = ev.estimation;
    function baisser(n) { if (RANG[n] > RANG[niveau]) niveau = n; }
    function dire(code, ton, v) { raisons.push({ code: code, ton: ton, v: v }); }
    var jours = s.jours;
    if (jours != null && jours < 0) { dire('delai_depasse', 'bloquant', { jours: -jours }); baisser(NOGO); }
    else if (jours != null && jours < reg.delai_min_jours) { dire('delai_court', 'attention', { jours: jours, minimum: reg.delai_min_jours }); baisser(ETUDIER); }
    if (elig && elig.statut === 'non_eligible') { dire('non_eligible', 'bloquant', { liste: listeManques(elig) }); baisser(NOGO); }
    else if (elig && elig.statut === 'a_etudier') { dire('eligibilite_incertaine', 'attention', { liste: listeIncertitudes(elig) }); baisser(ETUDIER); }
    var reco = ev.reco;
    if (ev.vide) {
      dire('plage_vide', 'bloquant', { cout_dh: ev.cout_dh, xmin: ev.xmin, xmin_dh: ev.xmin * E, xmax: ev.xmax }); baisser(NOGO);
    } else if (reco == null || reco.gain < reg.gain.nogo) {
      var mg = ev.meilleur_gain;
      dire('aucun_gain', 'bloquant', { xmin: ev.xmin, xmax: ev.xmax, gain: mg.gain, gain_dh: mg.gain_dh, x: mg.x, seuil: reg.gain.nogo }); baisser(NOGO);
    } else {
      if (reco.chance < reg.chance.nogo) { dire('chance_faible', 'bloquant', { chance: reco.chance, x: reco.x, seuil: reg.chance.nogo }); baisser(NOGO); }
      else if (reco.chance < reg.chance.go) { dire('chance_moyenne', 'attention', { chance: reco.chance, x: reco.x, seuil: reg.chance.go }); baisser(ETUDIER); }
      else dire('chance_ok', 'ok', { chance: reco.chance, x: reco.x });
      if (reco.gain < reg.gain.go) { dire('gain_moyen', 'attention', { gain: reco.gain, gain_dh: reco.gain_dh, x: reco.x, prix_dh: reco.x * E, seuil: reg.gain.go }); baisser(ETUDIER); }
      else dire('gain_ok', 'ok', { gain: reco.gain, gain_dh: reco.gain_dh, x: reco.x, prix_dh: reco.x * E });
      dire('marge', 'info', { marge_dh: reco.marge_dh, marge_pct: reco.marge_pct, marge_min: ev.marge_min });
      if (reco.x < reg.plage.alerte - EPS) dire('proche_seuil', 'attention', { x: reco.x, alerte: reg.plage.alerte });
    }
    var prevu = s.prevu;
    if (prevu != null) {
      if (prevu < reg.petit_marche) dire('marche_petit', 'info', { prevu: prevu, c_bas: ev.chance_plancher.chance, x_bas: ev.chance_plancher.x, c_un: ev.chance_un.chance, c_max: ev.chance_max.chance, x_max: ev.chance_max.x });
      else if (ev.chance_max.chance < reg.chance.go) dire('marche_gros_faible', 'info', { prevu: prevu, c_max: ev.chance_max.chance, x_max: ev.chance_max.x });
      else dire('marche_gros', 'info', { prevu: prevu, c_max: ev.chance_max.chance, x_max: ev.chance_max.x });
    }
    var faibles = [], nAch = s.acheteur_n, part = s.identifies_part;
    if (nAch != null && nAch < reg.confiance.acheteur_min) faibles.push(['acheteur_peu', { n: nAch }]);
    if (part != null && part < reg.confiance.identifies_min) faibles.push(['identifies_peu', { part: part }]);
    var confiance = nAch == null && part == null ? 'inconnue' : (faibles.length ? 'faible' : 'correcte');
    if (faibles.length) { faibles.forEach(function (f) { dire('confiance_' + f[0], 'attention', f[1]); }); baisser(ETUDIER); }
    (s.groupes_forts || []).forEach(function (g) { dire('groupe_fort', 'attention', { nom: g.nom, p: g.p, numero: g.numero }); baisser(ETUDIER); });
    var inf = s.infructueux;
    if (inf && inf.total > 0 && inf.taux > reg.infructueux_max) { dire('infructueux', 'attention', { taux: inf.taux, n: inf.n, total: inf.total, seuil: reg.infructueux_max }); baisser(ETUDIER); }
    if (s.plan_max != null && s.en_cours != null && s.en_cours >= s.plan_max) { dire('plan_plein', 'attention', { en_cours: s.en_cours, plan_max: s.plan_max }); baisser(ETUDIER); }
    return { decision: niveau, raisons: raisons, confiance: confiance };
  }

  // ------------------------------------------------------------------ les phrases
  function groupes(n) {
    var t = String(n), m = [];
    while (t.length > 3) { m.unshift(t.slice(-3)); t = t.slice(0, -3); }
    m.unshift(t);
    return m.join(' ');
  }
  function decimal(v, d, groupe) {
    var n = Math.floor(Math.abs(v) * Math.pow(10, d) + 0.5), signe = v < 0 && n ? '-' : '';
    var q = Math.pow(10, d), ent = d ? Math.floor(n / q) : n, frac = d ? n - ent * q : 0;
    var corps = (groupe ? groupes(ent) : String(ent)) + (d ? ',' + String(frac).padStart(d, '0') : '');
    return signe + corps;
  }
  var FORMATS = {
    dh: function (v) { return decimal(v, 0, true); }, pct: function (v) { return decimal(v * 100, 1) + ' %'; },
    pct0: function (v) { return decimal(v * 100, 0) + ' %'; }, t3: function (v) { return decimal(v, 3); },
    n1: function (v) { return decimal(v, 1); }, n: function (v) { return decimal(v, 0); }, s: function (v) { return String(v); }
  };
  function remplir(modele, v) {
    return modele.replace(/\{(\w+)(?::(\w+))?\}/g, function (_, nom, f) { return FORMATS[f || 's'](v[nom]); });
  }
  function phrase(raison, textes) { return remplir(textes.raisons[raison.code], raison.v); }

  function coutDuScenario(coutDh, estimation, partenaire) {
    if (!partenaire) return coutDh;
    var e = partenaire.effet_cout || 0.0;
    return coutDh + (partenaire.effet_cout_unite === 'pct' ? e * estimation : e);
  }

  var api = { rangClasse: rangClasse, suffit: suffit, codesDe: codesDe, exigencesDe: exigencesDe, verifier: verifier, apports: apports,
    lisser: lisser, indiceDe: indiceDe, evaluer: evaluer, decider: decider, remplir: remplir, phrase: phrase,
    coutDuScenario: coutDuScenario, listeManques: listeManques, listeIncertitudes: listeIncertitudes, GO: GO, ETUDIER: ETUDIER, NOGO: NOGO };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else racine.Decision = api;
})(this);
