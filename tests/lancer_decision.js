// Lance statique/decision.js sur les cas lus sur l'entrée standard (voir tests/test_decision.py) et écrit les résultats.
const fs = require('fs');
const path = require('path');
const Decision = require(path.join(__dirname, '..', 'statique', 'decision.js'));
const entree = JSON.parse(fs.readFileSync(0, 'utf8'));
const resultats = entree.cas.map(c => {
  const exig = Decision.exigencesDe(c.lignes_ao || []);
  const partenaires = c.partenaires || [];
  const groupement = partenaires.filter(p => p.id === c.scenario_partenaire);
  const elig = c.lignes_ao ? Decision.verifier(exig, c.quals || {}, groupement) : null;
  const partenaire = partenaires.find(p => p.id === c.scenario_partenaire) || null;
  const cout = Decision.coutDuScenario(c.cout_dh, c.estimation, partenaire);
  const ev = Decision.evaluer(c.xs, c.chance, c.estimation, cout, c.marge_min, c.reg, (c.signaux || {}).prevu);
  const dec = Decision.decider(ev, elig, c.signaux, c.reg);
  return { exigences: exig, elig, apports: Decision.apports(exig, c.quals || {}, partenaires), cout, ev, dec,
    phrases: dec.raisons.map(r => Decision.phrase(r, c.textes)) };
});
process.stdout.write(JSON.stringify({ resultats }));
