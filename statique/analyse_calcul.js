/* Le nombre de concurrents d'un AO, recalculé dans le navigateur.
 *
 * Même calcul que copilote/probables.py (poisson_binomiale, distribution, fourchette) :
 * tests/test_probables.py fait tourner les deux sur tests/cas_concurrents.json et exige les mêmes
 * chiffres. Si l'un change, l'autre change.
 *
 *   poisson_binomiale(ps, λ)  loi du nombre de sociétés venues si chacune vient avec sa probabilité,
 *                             indépendamment, plus une Poisson(λ) pour celles que rien n'annonce ;
 *   distribution(ps, λ, σ, δ) la même avec un effet commun u = δ + σ·x − σ²/2, x ~ N(0, 1), ajouté à la
 *                             note logistique de chaque société et à log λ : moyenne sur 9 valeurs de u
 *                             (quadrature de Gauss-Hermite). δ vient de l'historique de l'acheteur
 *                             (probables.corriger_par_historique) et reste fixe quand on modifie la liste ;
 *   esperance(ps, λ, σ, δ)    l'espérance de distribution(ps, λ, σ, δ), sans la convolution ;
 *   recaler(ps, λ, σ, δ, t)   étape C2 : δ' = δ + ajout(m), σ' = σ × facteur(m), m = esperance(ps, λ, σ, δ),
 *                             interpolés en log m dans la table t (probables.recaler) ; recalculé quand on
 *                             décoche une société ;
 *   fourchette(d, niveau)     [bas, haut] : premiers k où la loi cumulée atteint (1 − niveau)/2 et
 *                             1 − (1 − niveau)/2 ; probable = valeur la plus probable ; moyenne.
 */
(function (racine) {
  'use strict';

  var HERMITE_X = [-4.5127458634, -3.205429002856, -2.076847978678, -1.023255663789, 0.0,
    1.023255663789, 2.076847978678, 3.205429002856, 4.5127458634];
  var HERMITE_W = [2.234584401e-05, 0.00278914132123, 0.04991640676522, 0.24409750289494, 0.40634920634921,
    0.24409750289494, 0.04991640676522, 0.00278914132123, 2.234584401e-05];

  function poisson_binomiale(ps, lam, epsilon) {
    lam = lam || 0;
    epsilon = epsilon == null ? 1e-9 : epsilon;
    var d = [1.0], i, k, nd;
    for (i = 0; i < ps.length; i++) {
      var p = Math.min(1, Math.max(0, +ps[i]));
      nd = new Array(d.length + 1).fill(0);
      for (k = 0; k < d.length; k++) {
        nd[k] += d[k] * (1 - p);
        nd[k + 1] += d[k] * p;
      }
      d = nd;
    }
    if (lam > 0) {
      var poisson = [], terme = Math.exp(-lam), cumul = 0;
      k = 0;
      for (;;) {
        poisson.push(terme);
        cumul += terme;
        if (cumul >= 1 - epsilon || k > 500) break;
        k += 1;
        terme = terme * lam / k;
      }
      nd = new Array(d.length + poisson.length - 1).fill(0);
      for (i = 0; i < d.length; i++) {
        for (var j = 0; j < poisson.length; j++) nd[i + j] += d[i] * poisson[j];
      }
      d = nd;
    }
    while (d.length > 1 && d[d.length - 1] < epsilon) d.pop();
    return d;
  }

  function distribution(ps, lam, dispersion, decalage) {
    lam = lam || 0;
    decalage = +decalage || 0;
    if (!(dispersion > 0) && !decalage) return poisson_binomiale(ps, lam);
    var s = Math.max(0, +dispersion || 0), notes = [], total = [], i, k;
    for (i = 0; i < ps.length; i++) {
      var p = Math.min(1 - 1e-12, Math.max(1e-12, +ps[i]));
      notes.push(Math.log(p / (1 - p)));
    }
    for (var h = 0; h < HERMITE_X.length; h++) {
      var u = decalage + s * HERMITE_X[h] - s * s / 2;
      var qs = notes.map(function (z) { return 1 / (1 + Math.exp(-(z + u))); });
      var d = poisson_binomiale(qs, lam * Math.exp(u));
      while (total.length < d.length) total.push(0);
      for (k = 0; k < d.length; k++) total[k] += HERMITE_W[h] * d[k];
    }
    while (total.length > 1 && total[total.length - 1] < 1e-9) total.pop();
    return total;
  }

  function notesDe(ps) {
    var notes = [];
    for (var i = 0; i < ps.length; i++) {
      var p = Math.min(1 - 1e-12, Math.max(1e-12, +ps[i]));
      notes.push(Math.log(p / (1 - p)));
    }
    return notes;
  }

  function esperance(ps, lam, dispersion, decalage) {
    lam = lam || 0;
    decalage = +decalage || 0;
    var s = Math.max(0, +dispersion || 0), notes = notesDe(ps), total = 0.0;
    for (var h = 0; h < HERMITE_X.length; h++) {
      var u = decalage + s * HERMITE_X[h] - s * s / 2, somme = 0;
      for (var i = 0; i < notes.length; i++) somme += 1 / (1 + Math.exp(-(notes[i] + u)));
      total += HERMITE_W[h] * (somme + lam * Math.exp(u));
    }
    return total;
  }

  function recaler(ps, lam, dispersion, decalage, table) {
    decalage = +decalage || 0;
    dispersion = +dispersion || 0;
    var m = esperance(ps, lam, dispersion, decalage);
    if (!table) return { decalage: decalage, dispersion: dispersion, prevu: m, ajout: 0.0, facteur: 1.0 };
    var lx = Math.log(Math.max(m, 0.1)), xs = table.log_prevu, aj = table.ajout, fa = table.facteur, a, f;
    if (lx <= xs[0]) { a = aj[0]; f = fa[0]; }
    else if (lx >= xs[xs.length - 1]) { a = aj[aj.length - 1]; f = fa[fa.length - 1]; }
    else {
      var k = 1;
      while (xs[k] < lx) k++;
      var t = (lx - xs[k - 1]) / (xs[k] - xs[k - 1]);
      a = aj[k - 1] + (aj[k] - aj[k - 1]) * t;
      f = fa[k - 1] + (fa[k] - fa[k - 1]) * t;
    }
    return { decalage: decalage + a, dispersion: dispersion * f, prevu: m, ajout: a, facteur: f };
  }

  function fourchette(d, niveau) {
    niveau = niveau == null ? 0.8 : niveau;
    var basQ = (1 - niveau) / 2, hautQ = 1 - (1 - niveau) / 2;
    var bas = null, haut = null, cumul = 0, k;
    for (k = 0; k < d.length; k++) {
      cumul += d[k];
      if (bas === null && cumul >= basQ - 1e-12) bas = k;
      if (haut === null && cumul >= hautQ - 1e-12) { haut = k; break; }
    }
    if (haut === null) haut = d.length - 1;
    if (bas === null) bas = haut;
    var probable = 0, moyenne = 0, couverte = 0;
    for (k = 0; k < d.length; k++) {
      if (d[k] > d[probable]) probable = k;
      moyenne += k * d[k];
    }
    for (k = bas; k <= haut; k++) couverte += d[k];
    return { bas: bas, haut: haut, probable: probable, moyenne: moyenne, couverte: couverte, niveau: niveau };
  }

  var api = { poisson_binomiale: poisson_binomiale, distribution: distribution, fourchette: fourchette,
    esperance: esperance, recaler: recaler,
    HERMITE_X: HERMITE_X, HERMITE_W: HERMITE_W };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else racine.AnalyseCalcul = api;
})(typeof window !== 'undefined' ? window : this);
