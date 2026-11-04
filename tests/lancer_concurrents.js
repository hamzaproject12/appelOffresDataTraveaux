// Lance statique/analyse_calcul.js sur les cas lus sur l'entrée standard (JSON) :
//   {"cas": [{"ps": [...], "lambda": x, "dispersion": s, "decalage": δ (facultatif), "recalage": table (facultatif),
//             "niveau": n}, ...]}
// Écrit {"resultats": [{"distribution": [...], "fourchette": {...}}, ...]}. Sert à tests/test_probables.py.
const fs = require('fs');
const path = require('path');
const AnalyseCalcul = require(path.join(__dirname, '..', 'statique', 'analyse_calcul.js'));
const entree = JSON.parse(fs.readFileSync(0, 'utf8'));
const resultats = entree.cas.map(c => {
  const rc = AnalyseCalcul.recaler(c.ps, c.lambda, c.dispersion, c.decalage || 0, c.recalage || null);
  const d = AnalyseCalcul.distribution(c.ps, c.lambda, rc.dispersion, rc.decalage);
  return { recale: rc, distribution: d, fourchette: AnalyseCalcul.fourchette(d, c.niveau) };
});
process.stdout.write(JSON.stringify({ resultats }));
