// Lance statique/groupes_calcul.js sur des jeux de données lus sur l'entrée standard (JSON) :
//   {"jeux": [{"donnees": {...}, "reglages": {...} | null}, ...], "repetitions": N}
// Écrit {"resultats": [...], "ms": [durée du calcul de chaque jeu, en ms]}. Sert à tests/test_autour_societe.py.
const fs = require('fs');
const path = require('path');
const GroupesCalcul = require(path.join(__dirname, '..', 'statique', 'groupes_calcul.js'));
const entree = JSON.parse(fs.readFileSync(0, 'utf8'));
const resultats = [], ms = [];
for (const jeu of entree.jeux) {
  const t0 = process.hrtime.bigint();
  let r;
  for (let i = 0; i < (entree.repetitions || 1); i++) r = GroupesCalcul.calculer(jeu.donnees, jeu.reglages || undefined);
  ms.push(Number(process.hrtime.bigint() - t0) / 1e6 / (entree.repetitions || 1));
  resultats.push(r);
}
process.stdout.write(JSON.stringify({ resultats, ms }));
