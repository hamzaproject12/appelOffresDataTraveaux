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
 *   victoires_y = unités communes que Y a gagnées (X présente, X perd alors)
 *   part_x_sur_y = victoires de Y où X était présente / toutes les victoires de Y (societes[Y].victoires)
 *   position    = médiane de (taux de Y / taux de X − 1) sur les victoires de X
 * Lecture de chaque Y, dans cet ordre (seuils dans REGLAGES, réglables dans la page) :
 *   noyau            présente sur ≥ min_victoires victoires de X, A ÷ B ≥ rapport_min, ET part_x_sur_y ≥ part_noyau ;
 *   accompagnatrice  présente sur ≥ min_victoires victoires de X, et (A ÷ B ≥ rapport_min ou C ≥ c_min), sans être noyau ;
 *   concurrent       victoires_y ≥ min_victoires_y ET A ÷ B < rapport_concurrent ;
 *   neutre           le reste.
 * Groupe : X + noyaux et accompagnatrices, classés par victoires de X avec Y présente puis A ÷ B, au plus
 * taille_groupe sociétés (X comprise) ; les suivantes sont « autres ». Un fait statistique, jamais une accusation.
 */
(function (racine) {
  'use strict';

  var REGLAGES = { min_victoires: 3, rapport_min: 1.3, part_noyau: 0.5, c_min: 0.6, min_victoires_y: 3,
                   rapport_concurrent: 1.0, taille_groupe: 9 };

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
      var av = avec.get(cle) || 0, gy = gagneY.get(cle) || 0, soc = d.societes[cle] || {};
      var a = victoiresX ? av / victoiresX : null, b = nX ? c / nX : null;
      var rapport = a !== null && b ? a / b : null, ty = soc.total || 0, vt = soc.victoires || 0;
      var cc = ty ? c / ty : null, part = vt ? Math.min(1, gy / vt) : null;
      var presente = av >= r.min_victoires, fort = rapport !== null && rapport >= r.rapport_min;
      var lecture = 'neutre';
      if (presente && fort && part !== null && part >= r.part_noyau) lecture = 'noyau';
      else if (presente && (fort || (cc !== null && cc >= r.c_min))) lecture = 'accompagnatrice';
      else if (gy >= r.min_victoires_y && rapport !== null && rapport < r.rapport_concurrent) lecture = 'concurrent';
      lignes.push({ cle: cle, communes: c, victoires_x_avec: av, victoires_y: gy, victoires_y_total: vt,
                    part_x_sur_y: part, A: a, B: b, C: cc, rapport: rapport,
                    position: mediane(ecarts.get(cle) || []), lecture: lecture, total_y: ty });
    });
    lignes.sort(function (p, q) {
      return ((q.A || 0) - (p.A || 0)) || (q.communes - p.communes) || (p.cle < q.cle ? -1 : p.cle > q.cle ? 1 : 0);
    });
    var proches = lignes.filter(function (l) { return l.lecture === 'noyau' || l.lecture === 'accompagnatrice'; })
      .sort(function (p, q) {
        return (q.victoires_x_avec - p.victoires_x_avec) || (q.rapport - p.rapport) || (p.cle < q.cle ? -1 : p.cle > q.cle ? 1 : 0);
      });
    var place = Math.max(0, Math.trunc(r.taille_groupe) - 1), dedans = proches.slice(0, place), autres = proches.slice(place);
    function cles(l) { return l.map(function (z) { return z.cle; }); }
    return { unites_x: nX, victoires_x: victoiresX, lignes: lignes,
             groupe: [x].concat(cles(dedans)),
             noyau: [x].concat(cles(dedans.filter(function (l) { return l.lecture === 'noyau'; }))),
             accompagnatrices: cles(dedans.filter(function (l) { return l.lecture === 'accompagnatrice'; })),
             autres: cles(autres) };
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
