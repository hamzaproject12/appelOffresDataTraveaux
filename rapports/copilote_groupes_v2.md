# Copilote — groupes v2 : détection par excès au hasard, fiche société, défaites

Branche `copilote`. Tests : `python -m unittest discover -s tests -t .` → 211 OK. `python tester_ao_ouverts.py` → tous
les contrôles passent. Chiffres reproduits par `python -m copilote.mesures_groupes` (aucune écriture ; sortie complète :
`rapports/copilote_groupes_v2_mesures.json`). Les 66 groupes de la v1 sont gardés dans `rapports/copilote_groupes_v1_courant.json`.

## Diagnostic (historique complet, fusions appliquées)

| | demandé (environ) | mesuré, toutes offres | mesuré, offres écartées retirées |
|---|---:|---:|---:|
| sociétés actives (≥ 5 marchés) | 6 130 | 6 625 | 6 119 |
| règle v1 (≥ 5 communs, ≥ 50 % dans les deux sens) | 484 paires · 520 sociétés | 564 · 596 | 540 · 551 |
| ≥ 4 communs, excès ≥ 3, p < 0,001 | 6 321 · 2 100 | 10 570 · 2 765 | 9 251 · 2 446 |
| ≥ 3 communs, excès ≥ 3, p < 0,01 | 17 040 · 4 060 | 29 824 · 4 917 | 25 932 · 4 412 |

- Les ~6 130 sociétés actives se retrouvent quand on retire les offres écartées. Pour les paires, l'écart persiste même ainsi
  (10 570 ou 9 251 contre ~6 321). Je ne l'ai pas expliqué : l'acheteur pris par son nom brut et le comptage par lot ne
  le ferment pas. Ta formule de l'attendu diffère probablement de la mienne sur un point.
- La v2 compte toutes les offres déposées, écartées comprises (point 5 bis). Sur 151 645 présences société × marché,
  on a 132 833 admises ou attributaires, 17 543 écartées (dossier administratif ou technique) et 1 269 « déposées ».
  La matrice en compte bien 151 645.

## Méthode

1. **Attendu** pour chaque paire croisée au moins 3 fois (117 304 paires) : somme, sur les acheteurs (clé
   `cle_acheteur`), de (marchés de A chez l'acheteur × marchés de B ÷ marchés de l'acheteur). Excès = observé ÷ attendu ;
   valeur p de Poisson (`liens.py`).
2. **Niveaux** (`reglages.json`, section `groupes`) : les seuils demandés, gardés tels quels.
   - fort : ≥ 6 communs, excès ≥ 5, p < 0,0001 ;
   - moyen : ≥ 4 communs, excès ≥ 3, p < 0,001 ;
   - faible : ≥ 3 communs, excès ≥ 2, p < 0,01.

   **Ajout, seconde voie vers « moyen » :** la règle v1 (≥ 5 communs, ≥ 50 % dans les deux sens). Sans elle, seuls
   29 des 66 anciens groupes restaient couverts. 128 de leurs paires ont un excès proche de 1 : deux sociétés qui se
   suivent chez les mêmes acheteurs, si bien que l'acheteur explique la rencontre. Cette voie ajoute 304 liens moyens.
3. **Indicateurs** : « dans les deux sens », « B accompagne A » (B avec A sur ≥ 50 % de ses propres marchés).
4. **Groupes** : cliques maximales du graphe fort + moyen + avis oui − avis non.
   - Une clique de 3 sociétés ou plus doit réunir ≥ 3 membres sur ≥ 5 marchés. Sinon, ses liens restent des groupes
     de 2.
   - Les liens « faible » n'entrent jamais dans un groupe, sauf avec un avis oui.
5. **Familles** : Louvain et découpe comme avant. Au-delà de 12 sociétés, Louvain est relancé sur la famille à
   résolution croissante. Si de grandes cliques qui se recouvrent restent ensemble, elles sont rangées une à une sans
   dépasser 12. Une clique seule de plus de 12 sociétés reste entière.
6. **Score** : mêmes 6 critères.
   - Le poids de « qui gagne » passe à 0, et ses 15 points sont répartis : force 23,5, volume 17,6, hasard 35,3,
     contextes 17,6, hors profil 5,9.
   - Force = moyenne de la part la plus élevée des deux sens ; le minimum est affiché.
   - Hasard : tirages pour 3 sociétés ou plus, excès et Poisson pour une paire, sur la même échelle.
7. **Défaites** (`groupes_defaites`, un type « groupe » et un type « paire ») : pour chaque marché ou lot perdu, on
   range le gagnant (extérieur), son taux, et l'écart à la meilleure offre admise du groupe (classement de
   `construire_base`). On range aussi l'écart au prix de référence stocké, et si toutes les offres des membres étaient
   écartées.
8. **Avis** : oui retient la paire quel que soit son niveau, non l'exclut. « Ajouter une société liée » écrit un avis oui
   avec un commentaire obligatoire, visible aussitôt dans la fiche société et pris en compte par les groupes au recalcul
   suivant.

## Faux positifs attendus par niveau

| niveau | paires testées (≥ communs du niveau) | seuil p | attendues par hasard | trouvées |
|---|---:|---:|---:|---:|
| fort | 28 228 | 0,0001 | 2,8 | 3 321 |
| moyen (règle seule) | 65 615 | 0,001 | 65,6 | 10 570 |
| faible (règle seule) | 117 304 | 0,01 | 1 173 | 32 811 |

Le niveau faible garde donc environ 3,6 % de paires dues au hasard ; c'est pourquoi il ne forme pas de groupes. Ce calcul
est une borne grossière : l'attendu est estimé et les tests ne sont pas indépendants.

## Résultat

| | v1 | v2 |
|---|---:|---:|
| liens retenus pour les groupes | 564 | 10 874 (fort 3 321 · moyen 7 553) |
| liens faibles (fiche société seulement) | — | 22 108 |
| sociétés actives avec un partenaire moyen ou fort | 185 dans un groupe | **2 982** |
| sociétés actives avec au moins un partenaire (faible compris) | — | 4 839 |
| groupes | 66 | 5 257, dont 2 786 de 2 sociétés |
| familles | 45 | 2 453 |
| score médian | 54 | 52 |
| recalcul | 3 s · ~256 Mo | 70 à 90 s · pic 483 Mo |

- **Niveau des groupes** : 286 fort, 4 971 moyen.
- **Victoires** : 3 218 groupes « peu de victoires », 1 525 « rotation », 514 « chef de file ».
- **Mémoire** : mesurée sur ce poste. Avec le serveur (~370 Mo), le pire cas est d'environ 850 Mo pour 1 Go sur Railway.
  Le recalcul tourne dans le processus du planificateur, après la mise à jour quotidienne, jamais dans le serveur web.

## Les 66 groupes de la v1

- **Couverture** : 66 sur 66 sont inclus dans un groupe v2 ; 40 ont exactement la même composition, 26 sont dans un
  groupe plus grand (+1 à +8 sociétés). Niveau v2 : 64 moyen, 2 fort.
- **Effet du seul retrait de « qui gagne »** sur leurs anciennes composantes : +2,4 points en moyenne (de −10 à +12).
  Par structure : « peu de victoires » +4,0 ; « chef de file » +2,2 ; « rotation » +0,6. Un groupe qui gagne peu ne
  perd plus de points.
- **Score moyen** : 52,6 en v1 ; 55,0 avec les poids v2 ; 58,4 en v2 complète, qui ajoute la nouvelle force et les
  groupes agrandis.

## Stabilité (calcul jusqu'au 31/12/2025, vérifié sur les marchés de 2026)

Paires dont les deux sociétés ont au moins 2 marchés en 2026 :

| niveau en 2025 | paires | retrouvées en 2026 (≥ 1 commun) | ≥ 2 communs | excès 2026 |
|---|---:|---:|---:|---:|
| fort | 1 045 | 81,8 % | 66,0 % | 4,03 |
| moyen | 2 688 | 67,8 % | 49,9 % | 2,92 |
| faible | 7 589 | 50,7 % | 31,4 % | 2,36 |
| base : sans niveau (≥ 3 communs) | 37 833 | 45,2 % | 25,0 % | 1,75 |

L'ordre des niveaux tient en 2026, et chaque niveau bat la base simple.

## Temps de réponse (local, calcul courant)

| | mesure |
|---|---:|
| fiche société (220 sociétés : 200 tirées au hasard + les 20 plus liées) | médiane 9 ms · p95 29 ms · max 37 ms |
| fiche des 5 plus grands groupes | max 130 ms |
| liste (page de 25) | 60 ms ; avec recherche « travaux » 125 ms |

## Changements

- `copilote/liens.py` (nouveau) : attendu, excès, Poisson, niveaux, seconde voie.
- `copilote/groupes.py` :
  - graphe fort + moyen, groupes de 2, défaites, poids lus dans `reglages.json`, familles plafonnées ;
  - les tables changent de version et sont refaites automatiquement (`familles_anciennes` est conservée) ;
  - Louvain sur un sous-graphe trié : avant, le nombre de familles changeait d'un lancement à l'autre.
- `copilote/api_groupes.py` : recherche dans toute la table `societes`, fiche société, liste paginée et filtrée par
  niveau avec compteurs, avis « ajout ».
- `copilote/routes.py` : `GET /api/copilote/societes`, `GET /api/copilote/societe/{cle}`, paramètres `niveaux` et
  `page`.
- `statique/copilote_groupes.html` :
  - phrase d'en-tête, glossaire, filtres, pagination, résultats « Sociétés » ;
  - fiche société avec partenaires, preuves, avis et ajout d'une société liée ;
  - bloc « Quand le groupe est présent », avec ses marchés perdus.
- `serveur.py` : le gestionnaire d'erreurs garde le `Location` des redirections. En production, `/copilote/groupes`
  affichait `{"erreur": …}` au lieu du formulaire de code. Toutes les redirections du site sont maintenant testées par
  l'application complète.
- `copilote/rejouer_ao.py` : réutilise le calcul « jusqua-date » d'une même coupure, sauf s'il date de la v1.

## Limites

- **352 familles de plus de 12 sociétés**, chacune une clique unique de 13 à 19 sociétés. Elles viennent de 4 noyaux
  denses (17, 38, 59 et 82 sociétés, 199 au total), par exemple un même acheteur de l'Éducation ou de l'Agriculture.
  On ne peut pas les couper sans casser des liens, et elles se recouvrent beaucoup dans la liste. Piste : un affichage
  « noyau dense ».
- **L'attendu ne tient compte que de l'acheteur**, pas de la spécialité ni de la classe. Des sociétés d'une même
  spécialité chez un grand acheteur paraissent liées : c'est une partie des noyaux ci-dessus.
- **Fiche société** : les 30 places vont d'abord aux paires avec avis, puis aux niveaux les plus forts ; l'affichage est
  ensuite trié par excès. Trié par excès seul, des liens faibles sur 3 marchés (attendu ≈ 0,1) chassaient les liens
  forts : vu sur STE AFDAM TRAVAUX, avec 2 forts sur 4 affichés avant correction.
- **Ancien calcul** : les calculs v1 de `copilote_calcul.db` (dont les 20 « jusqua » des AO rejoués) sont refaits au
  premier recalcul. Pour les rejeux : `python -m copilote.rejouer_ao groupes`, environ 30 min, une fois par date de
  coupure.
- **Avis** : il change la fiche société tout de suite, mais les groupes seulement au recalcul suivant.

## À faire sur Railway au prochain déploiement

Après le déploiement, lancer une fois dans le pod : `python -m copilote.recalculer groupes` (environ 1 min 30). Hier,
`derniere_maj_groupes.txt` a été posé : sans ce lancement, le planificateur attendrait 7 jours, et la page dirait
« aucun calcul disponible ».

## À tester par Hamza

1. `/copilote/groupes` sans le code : on arrive au formulaire de code, plus de JSON.
2. La phrase d'en-tête doit annoncer 6 625 actives, dont 4 839 avec un partenaire (2 982 moyen ou fort). Ouvrir
   « Comprendre les termes ».
3. Filtres : par défaut Fort + Moyen + Confirmé, avec un compteur par niveau. Changer de filtre, puis de page.
4. Chercher des sociétés que tu connais, y compris hors groupe : elles apparaissent sous « Sociétés ».
5. Ouvrir une fiche société et vérifier les partenaires de présence que tu connais : %, excès, qui gagne, marchés en
   preuve. Donner un avis Oui, Non ou Je ne sais pas.
6. « Ajouter une société liée » sur une paire réelle : le commentaire est obligatoire, et le lien apparaît aussitôt
   dans les deux fiches.
7. Ouvrir un groupe qui gagne peu : bloc « Quand le groupe est présent », marchés perdus, écarts, et un score qui n'est
   pas pénalisé.
8. Lire une famille de 13 sociétés ou plus (noyau dense) et dire si l'affichage « noyau » te serait utile.
9. Tout vérifier sur téléphone (390 px).

## FEU VERT

| critère | mesure | état |
|---|---|---|
| ≥ 1 500 sociétés actives avec un partenaire moyen ou fort | 2 982 | OK |
| les 66 anciens groupes retrouvés | 66 / 66 dans un groupe (40 identiques) | OK |
| toute société de la base trouvée par la recherche | 11 631 / 11 631 (par leur nom complet) | OK |
| fiche société < 300 ms | max 37 ms sur 220 sociétés | OK |
| aucun mot accusateur dans la page | 0 (page et textes de l'API) | OK |
| stabilité par niveau, au-dessus de la base | excès 2026 : 4,03 / 2,92 / 2,36 contre 1,75 | OK |
| tests et `tester_ao_ouverts.py` | 211 OK · tous les contrôles passent | OK |
