// Lance statique/simulateur.js sur les cas lus sur l'entrée standard : {"cas": [{"modele": {...}, "modifs": {...}}]}.
// Écrit {"resultats": [sortie de simuler, ...], "ms": [durée de chaque cas]}. Sert à tests/test_simulateur.py.
const fs = require('fs');
const path = require('path');
const Simulateur = require(path.join(__dirname, '..', 'statique', 'simulateur.js'));
const entree = JSON.parse(fs.readFileSync(0, 'utf8'));
const ms = [];
const resultats = entree.cas.map(c => {
  const t0 = process.hrtime.bigint();
  const r = Simulateur.simuler(c.modifs ? Simulateur.appliquer(c.modele, c.modifs) : c.modele);
  ms.push(Number(process.hrtime.bigint() - t0) / 1e6);
  return r;
});
process.stdout.write(JSON.stringify({ resultats, ms }));
