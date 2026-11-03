// Étape C : la simulation de l'ouverture des plis, dans le navigateur.
// Copie À L'IDENTIQUE de copilote/simulateur.py (même générateur mulberry32, mêmes tirages dans le même ordre).
// Même graine = même résultat : tests/cas_simulation.json, tests/lancer_simulateur.js, tests/test_simulateur.py.
// Lire l'en-tête de simulateur.py pour la description d'un scénario et de la règle du gagnant.
(function (racine) {
  'use strict';
  var TAUX_MIN = 0.3, TAUX_MAX = 3.0;
  var GRILLE = { debut: 1600, denominateur: 2000, n: 801 };
  var SCENARIOS = 4000, LISSAGE = 10, PART_ZONE = 0.9;
  var CLASSES_P = { debut: 0.70, pas: 0.005, n: 120 };

  function Generateur(graine) { this.a = graine >>> 0; }
  Generateur.prototype.uniforme = function () {
    this.a = (this.a + 0x6D2B79F5) >>> 0;
    var t = this.a;
    t = Math.imul(t ^ (t >>> 15), t | 1) >>> 0;
    t = (t ^ ((t + (Math.imul(t ^ (t >>> 7), t | 61) >>> 0)) >>> 0)) >>> 0;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  Generateur.prototype.normale = function () {
    var u1 = 1.0 - this.uniforme();
    var u2 = this.uniforme();
    return Math.sqrt(-2.0 * Math.log(u1)) * Math.cos(2.0 * Math.PI * u2);
  };
  Generateur.prototype.poisson = function (moyenne) {
    if (moyenne <= 0) return 0;
    var limite = Math.exp(-moyenne), k = 0, p = this.uniforme();
    while (p > limite && k < 1000) { k += 1; p *= this.uniforme(); }
    return k;
  };

  function logistique(z) { if (z < -700.0) return 0.0; return 1.0 / (1.0 + Math.exp(-z)); }
  function logit(p) { p = Math.min(1 - 1e-9, Math.max(1e-9, p)); return Math.log(p / (1 - p)); }
  function quantileInterpole(q, u) {
    var x = u * (q.length - 1), a = Math.floor(x);
    if (a >= q.length - 1) return q[q.length - 1];
    return q[a] + (q[a + 1] - q[a]) * (x - a);
  }
  function quantile(tries, p) {
    if (!tries.length) return null;
    var x = p * (tries.length - 1), a = Math.floor(x), b = Math.min(a + 1, tries.length - 1);
    return tries[a] + (tries[b] - tries[a]) * (x - a);
  }
  function borner(t) { return t < TAUX_MIN ? TAUX_MIN : (t > TAUX_MAX ? TAUX_MAX : t); }
  function sigmaPropre(sigma, sm) { return Math.sqrt(Math.max(sigma * sigma - sm * sm, 0.0001)); }
  var parNombre = function (a, b) { return a - b; };

  // Deux temps : qui vient, puis les taux, décalés de d(N) = constante + pente × log(N) (voir simulateur.py).
  function scenario(g, mo, detail) {
    var nb = mo.nombre, su = nb.dispersion;
    var u = nb.decalage + su * g.normale() - su * su / 2;
    var ma = mo.marche, m = ma.sigma * g.normale();
    var soc = mo.societes, offres = [], qui = [], evincees = [], chefs = [];
    var venusG = [], nVenus = 0;
    mo.groupes.forEach(function (gr) {
      var c = soc[gr.chef];
      var present = g.uniforme() < logistique(c.z + u);
      var gagner = g.uniforme() < gr.w;
      var membres = gr.membres.map(function (j) {
        var s = soc[j], seuil = present && gagner ? s.q : logistique(s.z + u);
        return g.uniforme() < seuil;
      });
      venusG.push([present, gagner, membres]);
      nVenus += (present ? 1 : 0) + membres.filter(Boolean).length;
    });
    var venusS = mo.seules.map(function (i) { var v = g.uniforme() < logistique(soc[i].z + u); if (v) nVenus += 1; return v; });
    var k = g.poisson(nb.lambda * Math.exp(u));
    nVenus += k;
    var d = (ma.constante || 0.0) + (ma.pente || 0.0) * Math.log(Math.max(nVenus, 1));
    function poser(i, taux) {
      if (g.uniforme() < soc[i].eviction) { if (detail) evincees.push([i, borner(taux)]); return; }
      offres.push(borner(taux)); qui.push(i);
    }
    mo.groupes.forEach(function (gr, ig) {
      var v = venusG[ig], present = v[0], gagner = v[1], c = soc[gr.chef], tChef = null;
      if (present) {
        tChef = c.mu + m + d + c.sp * g.normale();
        var avant = offres.length;
        poser(gr.chef, tChef);
        chefs.push(offres.length > avant ? offres[offres.length - 1] : null);
      } else chefs.push(null);
      gr.membres.forEach(function (j, jm) {
        if (!v[2][jm]) return;
        var s = soc[j];
        if (present && gagner) poser(j, tChef + s.ev_mu + s.ev_sigma * g.normale());
        else { var pr = present ? s.avec : s.sans; poser(j, pr.mu + m + d + pr.sp * g.normale()); }
      });
    });
    mo.seules.forEach(function (i, is) {
      if (venusS[is]) { var s = soc[i]; poser(i, s.mu + m + d + s.sp * g.normale()); }
    });
    var inc = mo.inconnues;
    for (var r = 0; r < k; r++) {
      var q = quantileInterpole(inc.quantiles, g.uniforme());
      var t = inc.moyenne + m + d + (q - inc.moyenne) * inc.f;
      if (g.uniforme() < inc.eviction) { if (detail) evincees.push([-1 - r, borner(t)]); continue; }
      offres.push(borner(t)); qui.push(-1 - r);
    }
    return { offres: offres, qui: qui, chefs: chefs, evincees: evincees, u: u, m: m + d };
  }

  function referenceSans(offres) {
    if (!offres.length) return null;
    var s = 0.0;
    for (var i = 0; i < offres.length; i++) s += offres[i];
    return (s / offres.length + 1.0) / 2.0;
  }
  function gagnantSans(offres, p) {
    var meilleur = -1, sous = false;
    for (var i = 0; i < offres.length; i++) {
      var t = offres[i];
      if (t <= p) { if (!sous || t > offres[meilleur]) { meilleur = i; sous = true; } }
      else if (!sous && (meilleur < 0 || t < offres[meilleur])) meilleur = i;
    }
    return meilleur;
  }
  function grille(mo) {
    var gr = (mo && mo.grille) || GRILLE, xs = new Array(gr.n);
    for (var j = 0; j < gr.n; j++) xs[j] = (gr.debut + j) / gr.denominateur;
    return xs;
  }
  // Ajoute 1 à gains[j] pour chaque x de la grille où notre offre gagne (xs croissants).
  function compterVictoires(offres, xs, gains) {
    var k = offres.length, j;
    if (k === 0) { for (j = 0; j < xs.length; j++) gains[j] += 1; return; }
    var s = 0.0;
    for (j = 0; j < k; j++) s += offres[j];
    var r = offres.slice().sort(parNombre), idx = 0;
    for (j = 0; j < xs.length; j++) {
      var x = xs[j], p = ((s + x) / (k + 1) + 1.0) / 2.0;
      while (idx < k && r[idx] < x) idx++;
      var suivant = idx < k ? r[idx] : Infinity;
      if (x <= p ? suivant > p : r[0] > x) gains[j] += 1;
    }
  }

  function preparer(mo) {
    var sm = mo.marche.sigma;
    var soc = mo.societes.map(function (s) {
      var x = Object.assign({}, s);
      x.z = s.p > 0 ? logit(s.p) : -1e9;
      x.sp = sigmaPropre(s.sigma, sm);
      ['avec', 'sans'].forEach(function (cle) {
        if (s[cle]) x[cle] = { mu: s[cle].mu, sp: sigmaPropre(s[cle].sigma, sm) };
      });
      return x;
    });
    var inc = Object.assign({}, mo.inconnues), si = inc.sigma;
    inc.f = si > 0 ? Math.sqrt(Math.max(0.1, 1.0 - sm * sm / (si * si))) : 1.0;
    var dans = {};
    mo.groupes.forEach(function (gr) { dans[gr.chef] = 1; gr.membres.forEach(function (i) { dans[i] = 1; }); });
    var seules = [];
    for (var i = 0; i < soc.length; i++) if (!dans[i]) seules.push(i);
    return Object.assign({}, mo, { societes: soc, inconnues: inc, seules: seules });
  }

  function zoneRecommandee(xs, chance, scenarios) {
    scenarios = scenarios || SCENARIOS;
    var n = chance.length, lisse = [], j, k;
    if (!n) return null;
    for (j = 0; j < n; j++) {
      var a = Math.max(0, j - LISSAGE), b = Math.min(n - 1, j + LISSAGE), s = 0.0;
      for (k = a; k <= b; k++) s += chance[k];
      lisse.push(s / (b - a + 1));
    }
    var jm = 0;
    for (j = 1; j < n; j++) if (lisse[j] > lisse[jm]) jm = j;
    if (lisse[jm] <= 0) return null;
    var seuil = PART_ZONE * lisse[jm], de = jm, a2 = jm;
    while (de > 0 && lisse[de - 1] >= seuil) de--;
    while (a2 < n - 1 && lisse[a2 + 1] >= seuil) a2++;
    return { x: xs[jm], indice: jm, chance: chance[jm], chance_lissee: lisse[jm], de: xs[de], a: xs[a2], seuil: seuil };
  }

  // La chance simulée corrigée par la table de calibration (voir calibrer dans simulateur.py).
  function calibrer(chance, table) {
    if (!table) return chance.slice();
    var tx = table.x, ty = table.y;
    return chance.map(function (c) {
      if (c <= tx[0]) return ty[0];
      if (c >= tx[tx.length - 1]) return ty[ty.length - 1];
      var k = 1;
      while (tx[k] < c) k++;
      return ty[k - 1] + (ty[k] - ty[k - 1]) * (c - tx[k - 1]) / (tx[k] - tx[k - 1]);
    });
  }

  function histogramme(tries) {
    var h = new Array(CLASSES_P.n).fill(0);
    tries.forEach(function (v) {
      var k = Math.floor((v - CLASSES_P.debut) / CLASSES_P.pas);
      if (k >= 0 && k < CLASSES_P.n) h[k] += 1;
    });
    return h;
  }

  function simuler(modele, avecCentral) {
    var mo = preparer(modele), S = mo.scenarios || SCENARIOS, g = new Generateur(mo.graine), xs = grille(mo);
    var gains = new Array(xs.length).fill(0), refs = [], refsS = [], nombres = [], etats = [];
    var ng = mo.groupes.length, chefPresent = new Array(ng).fill(0), chefGagne = new Array(ng).fill(0), chefEcarts = [];
    for (var k = 0; k < ng; k++) chefEcarts.push([]);
    for (var s = 0; s < S; s++) {
      etats.push(g.a);
      var sc = scenario(g, mo, false);
      compterVictoires(sc.offres, xs, gains);
      var p0 = referenceSans(sc.offres);
      nombres.push(sc.offres.length);
      refsS.push(p0);
      if (p0 === null) continue;
      refs.push(p0);
      var gi = gagnantSans(sc.offres, p0);
      for (k = 0; k < ng; k++) {
        var t = sc.chefs[k];
        if (t === null) continue;
        chefPresent[k] += 1;
        chefEcarts[k].push(t - p0);
        if (sc.qui[gi] === mo.groupes[k].chef) chefGagne[k] += 1;
      }
    }
    var brute = gains.map(function (v) { return v / S; });
    var chance = calibrer(brute, mo.calibration);
    // Calibration croissante : point recommandé et zone lus sur la courbe simulée, chance affichée calibrée.
    var zone = zoneRecommandee(xs, brute, S);
    if (zone) {
      zone.chance = chance[zone.indice];
      var cz = calibrer([zone.chance_lissee, zone.seuil], mo.calibration);
      zone.chance_lissee = cz[0];
      zone.seuil = cz[1];
    }
    var tries = refs.slice().sort(parNombre), med = quantile(tries, 0.5);
    var sortie = {
      scenarios: S, graine: mo.graine, grille: xs, chance: chance, chance_brute: brute, zone: zone,
      prix_reference: { n: tries.length, mediane: med, q10: quantile(tries, 0.1), q90: quantile(tries, 0.9),
        q25: quantile(tries, 0.25), q75: quantile(tries, 0.75), histogramme: histogramme(tries) },
      offres_moyennes: nombres.reduce(function (a, b) { return a + b; }, 0) / S,
      chefs: mo.groupes.map(function (gr, k) {
        var e = chefEcarts[k].slice().sort(parNombre);
        return { chef: mo.societes[gr.chef].cle, present: chefPresent[k] / S, gagne: chefGagne[k] / S,
          ecart_mediane: quantile(e, 0.5), ecart_q10: quantile(e, 0.1), ecart_q90: quantile(e, 0.9) };
      })
    };
    if (avecCentral !== false && med !== null)
      sortie.central = scenarioCentral(mo, etats, refsS, nombres, med, zone ? zone.x : 1.0);
    return sortie;
  }

  function scenarioCentral(mo, etats, refs, nombres, med, x) {
    var nm = nombres.slice().sort(parNombre)[Math.floor(nombres.length / 2)], meilleur = 0, score = null;
    for (var s = 0; s < refs.length; s++) {
      if (refs[s] === null) continue;
      var sc = Math.abs(refs[s] - med) * 100.0 + Math.abs(nombres[s] - nm);
      if (score === null || sc < score) { meilleur = s; score = sc; }
    }
    var g = new Generateur(0);
    g.a = etats[meilleur];
    var r = scenario(g, mo, true);
    return detailler(mo, meilleur, r.offres, r.qui, r.evincees, x, r.u, r.m);
  }

  function detailler(mo, s, offres, qui, evincees, x, u, m) {
    var soc = mo.societes;
    function nom(i) { return i >= 0 ? { cle: soc[i].cle, nom: soc[i].nom || soc[i].cle } : { cle: null, nom: 'Société non identifiée ' + (-i) }; }
    var toutes = offres.concat([x]), somme = 0;
    for (var i = 0; i < toutes.length; i++) somme += toutes[i];
    var moyenne = somme / toutes.length, p = (moyenne + 1.0) / 2.0, gi = gagnantSans(toutes, p);
    var lignes = offres.map(function (t, k) { return Object.assign(nom(qui[k]), { taux: t, admise: true }); })
      .concat(evincees.map(function (e) { return Object.assign(nom(e[0]), { taux: e[1], admise: false }); }));
    return { scenario: s, x: x, offres: lignes, moyenne: moyenne, prix_reference: p,
      gagnant: gi === offres.length ? 'nous' : nom(qui[gi]).nom, nous_gagnons: gi === offres.length,
      effet_nombre: u, effet_marche: m };
  }

  // Les modifications de la page : {cle: {retiree, p, mu}} → un nouveau modèle (l'original n'est pas touché).
  function appliquer(modele, modifs) {
    modifs = modifs || {};
    var societes = modele.societes.map(function (s) {
      var md = modifs[s.cle];
      if (!md) return s;
      var x = Object.assign({}, s);
      if (md.retiree) { x.p = 0; x.q = 0; }
      else if (md.p != null) x.p = md.p;
      if (md.mu != null) {
        var d = md.mu - s.mu;
        x.mu = md.mu;
        if (s.avec) x.avec = { mu: s.avec.mu + d, sigma: s.avec.sigma };
        if (s.sans) x.sans = { mu: s.sans.mu + d, sigma: s.sans.sigma };
      }
      return x;
    });
    return Object.assign({}, modele, { societes: societes });
  }

  // Notre offre x gagne-t-elle face à ces offres admises (taux) ? La même règle que la courbe.
  function gagne(offres, x) { var g = [0]; compterVictoires(offres, [x], g); return g[0] === 1; }

  var api = { Generateur: Generateur, gagne: gagne, simuler: simuler, appliquer: appliquer, preparer: preparer,
    zoneRecommandee: zoneRecommandee, grille: grille, CLASSES_P: CLASSES_P, GRILLE: GRILLE };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else racine.Simulateur = api;
})(this);
