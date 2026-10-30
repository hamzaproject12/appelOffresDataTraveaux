/* Le calcul « autour d'une société », fait dans le navigateur.
 *
 * Même calcul que copilote/autour_societe.py (calculer) : tests/test_autour_societe.py fait tourner
 * les deux sur les mêmes données et exige les mêmes chiffres. Si l'un change, l'autre change.
 *
 * Entrée : la réponse de /api/groupes/societe/{cle}/donnees (unités de la société X, avec les offres
 * de chacun en taux = montant / estimation, et le nombre total d'unités de chaque autre société).
 * Une unité est un marché ordinaire ou UN LOT. Présence = toute offre déposée, écartée comprise.
 *
 * Pour chaque autre société Y :
 *   A  = victoires de X avec Y présente / victoires de X
 *   B  = unités communes / unités de X
 *   C  = unités communes / unités de Y dans toute la base
 *   A ÷ B  = combien de fois Y est plus présente quand X gagne que d'habitude
 *   victoires_y = unités communes que Y a gagnées (X perd alors)
 *   position    = médiane de (taux de Y / taux de X − 1) sur les victoires de X
 * Lecture : « presente » (plus présente quand elle gagne), « concurrent » (concurrent direct) ou
 * « neutre ». Un fait statistique, jamais une accusation.
 */
(function (racine) {
  'use strict';

  var REGLAGES = { min_victoires: 3, rapport_min: 1.5, max_victoires_y: 2, min_victoires_y: 3 };

  function mediane(v) {
    if (!v.length) return null;
    var t = v.slice().sort(function (a, b) { return a - b; }), m = t.length >> 1;
    return t.length % 2 ? t[m] : (t[m - 1] + t[m]) / 2;
  }

  function calculer(d, reglages) {
    var r = Object.assign({}, REGLAGES, reglages || {});
    var x = d.societe.cle, unites = d.unites, nX = unites.length, victoiresX = 0, i, j;
    var communes = new Map(), avec = new Map(), gagneY = new Map(), ecarts = new Map();
    function plus(m, k) { m.set(k, (m.get(k) || 0) + 1); }

    for (i = 0; i < nX; i++) {
      var u = unites[i], gagne = u.gagnant === x, tauxX = null, vus = new Set();
      for (j = 0; j < u.offres.length; j++) if (u.offres[j][0] === x) { tauxX = u.offres[j][2]; break; }
      if (gagne) victoiresX++;
      for (j = 0; j < u.offres.length; j++) {
        var cle = u.offres[j][0], taux = u.offres[j][2];
        if (cle === x || vus.has(cle)) continue;
        vus.add(cle);
        plus(communes, cle);
        if (gagne) {
          plus(avec, cle);
          if (taux && tauxX) {
            if (!ecarts.has(cle)) ecarts.set(cle, []);
            ecarts.get(cle).push(taux / tauxX - 1);
          }
        } else if (u.gagnant === cle) plus(gagneY, cle);
      }
    }

    var lignes = [];
    communes.forEach(function (c, cle) {
      var av = avec.get(cle) || 0, gy = gagneY.get(cle) || 0, soc = d.societes[cle];
      var a = victoiresX ? av / victoiresX : null, b = nX ? c / nX : null;
      var rapport = a !== null && b ? a / b : null, ty = soc ? soc.total : 0;
      var lecture = 'neutre';
      if (av >= r.min_victoires && rapport !== null && rapport >= r.rapport_min && gy <= r.max_victoires_y) lecture = 'presente';
      else if (gy >= r.min_victoires_y && gy > av) lecture = 'concurrent';
      lignes.push({ cle: cle, communes: c, victoires_x_avec: av, victoires_y: gy, A: a, B: b,
                    C: ty ? c / ty : null, rapport: rapport, position: mediane(ecarts.get(cle) || []),
                    lecture: lecture, total_y: ty });
    });
    lignes.sort(function (p, q) {
      return ((q.A || 0) - (p.A || 0)) || (q.communes - p.communes) || (p.cle < q.cle ? -1 : p.cle > q.cle ? 1 : 0);
    });
    var groupe = lignes.filter(function (l) { return l.lecture === 'presente'; })
      .map(function (l) { return l.cle; }).sort();
    return { unites_x: nX, victoires_x: victoiresX, lignes: lignes, groupe: [x].concat(groupe) };
  }

  /* Les unités de X où Y est présente, pour la preuve : les plus récentes d'abord (l'ordre des données). */
  function preuves(d, cleY) {
    var x = d.societe.cle, sortie = [];
    d.unites.forEach(function (u) {
      var y = null, mien = null;
      u.offres.forEach(function (o) { if (o[0] === cleY) y = o; if (o[0] === x) mien = o; });
      if (!y) return;
      sortie.push({ unite: u, taux_x: mien ? mien[2] : null, taux_y: y[2], statut_y: y[1],
                    gagne: !u.gagnant ? null : u.gagnant === x ? 'x' : u.gagnant === cleY ? 'y' : 'autre' });
    });
    return sortie;
  }

  var api = { REGLAGES: REGLAGES, calculer: calculer, preuves: preuves, mediane: mediane };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else racine.GroupesCalcul = api;
})(typeof window !== 'undefined' ? window : this);
