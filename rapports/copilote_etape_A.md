# Copilote — étape A : sociétés probables, groupes de l'AO, première page d'analyse

Branche `copilote`, fusionnée dans `main`. Tests : `python -m unittest discover -s tests -t .` → **282 OK** (253 + 29 nouveaux).
`python tester_ao_ouverts.py` → tous les contrôles passent. `serveur.py` n'est pas modifié.
Mesures : `python -m copilote.mesures_probables tout` (≈ 5 min) → `rapports/copilote_etape_A_mesures.json`.

## Ce qui est en place

| Fichier | Rôle |
|---|---|
| `copilote/probables.py` | historique en mémoire (coupé par `rejeu.limite_pour`), candidates, probabilité de venir, éviction, loi du nombre de concurrents |
| `copilote/probables.json` | coefficients, λ, σ et table de calibration, ajustés sur 2025 (`mesures_probables ajuster`) |
| `copilote/groupes_ao.py` | alliées (calcul « autour d'une société » sur la coupure), groupes par union-find, chef de file, stratégie passée |
| `copilote/analyse.py` | le dossier `GET /api/copilote/analyse/{ref}?lot=` (protégé par COPILOTE_ACCES) |
| `copilote/verite_rejeu.py` | la vérité d'un REJEU pour la page (`GET /api/copilote/rejeu/{ref}/verite`) ; jamais importé par le moteur (test) |
| `copilote/mesures_probables.py` | ajustement (2025) et mesures (2026 + REJEU) |
| `statique/copilote_analyse.html`, `analyse_calcul.js` | page `/copilote/analyse/{ref}` (étapes ① à ⑤, bloc « Ce qui s'est réellement passé ») |
| `statique/ao_ouverts.html` | bouton « Analyser » sur chaque AO, seulement si `/api/copilote/moi` répond 200 |
| `autour_societe.py` (+ `donnees_unites`), `rejeu.py` (+ `limite_pour`), `api_similaires.py` (`interne=True`) | petits ajouts |
| `tests/test_probables.py`, `tests/cas_concurrents.json`, `tests/lancer_concurrents.js` | 29 tests ; JS et Python comparés sur 10 cas communs |

## Comment c'est calculé

- **Candidates** : sociétés des 300 marchés les plus similaires, et celles qui ont déposé chez l'acheteur (3 ans) ou dans la région (12 mois), sans qualification incompatible. En moyenne 1 900 par AO ; elles couvrent 86 % des vrais soumissionnaires (60 % avec les seuls 50 similaires et l'acheteur).
- **Probabilité de venir** : une formule logistique sur 12 variables (similaires pondérés par la note et la récence, acheteur, qualification, activité, ancienneté, région, famille, écart au montant habituel). Elle est ajustée sur 400 marchés de 2025, puis corrigée par une table de calibration strictement croissante, apprise sur 400 autres marchés : l'ordre des sociétés ne change pas.
- **Nombre de concurrents** : une Poisson-binomiale **avec un effet commun au marché** (σ = 0,7), plus une Poisson(λ) pour les sociétés que rien n'annonce. Sans effet commun, la fourchette à 80 % ne contenait le vrai nombre que 44 % du temps : un marché attire plus ou moins tout le monde à la fois.
- **Liste affichée** : les 30 premières à partir de 3 %, plus les sociétés « par association ». Les alliées sont cherchées pour les sociétés à 15 % ou plus (seuil réglable). Toutes les règles de groupe sont celles de ta demande (a à d, 9 sociétés au plus).

## Mesures

| | backtest 2026 (500 marchés) | 20 AO REJEU (27 marchés ou lots) |
|---|---:|---:|
| vrais soumissionnaires dans le top 10 | 27,0 % | 33,5 % |
| vrais soumissionnaires dans le top 15 | **33,4 %** (base simple 29,3 %) | **41,4 %** (base 38,9 %) |
| vrai gagnant dans la liste (≈ 24 sociétés) | **39,0 %** | **63,0 %** |
| vrai gagnant dans le top 15 | 34,1 % (base 30,0 %) | 48,1 % |
| vrai nombre dans la fourchette 80 % | **77,6 %** (10 % en dessous, 13 % au-dessus) | **85,2 %** |
| largeur moyenne de la fourchette | 13,5 | 13,1 |
| vraies sociétés trouvées seulement par association | 22 sur 3 868 (0,6 %) | 3 sur 218 |
| vrais membres de groupe retrouvés dans un groupe prévu | 56,8 % (474) | 91,7 % (60) |

- **Base simple** : classer par nombre de présences dans les 50 similaires. La méthode la bat partout ; elle est donc gardée.
- **Par taille de marché (2026)** :
  - top 15 : 42 % pour 1 à 3 offres, 31 % pour 4 à 7, 27 % pour 8 à 15, 17 % au-delà ;
  - fourchette : 80 %, 95 %, 74 % et **39 %** pour les marchés de 16 offres et plus (sous-estimés).
- **Calibration (2026)** : une société annoncée à 26 % vient 20 % du temps ; à 35 %, 28,5 % du temps. Avant correction, une société annoncée à 82 % ne venait que 39 % du temps.

## FEU VERT

| critère | mesuré (2026 / REJEU) | |
|---|---|---|
| ≥ 60 % des vrais soumissionnaires dans le top 15 | 33 % / 41 % | **KO** |
| vrai gagnant dans la liste ≥ 70 % | 39 % / 63 % | **KO** |
| nombre de concurrents dans la fourchette 80 % entre 70 et 90 % | 77,6 % / 85,2 % | **OK** |
| page sans erreur à 390 et 1 440 px | aucune erreur console, aucun débordement horizontal (sélecteur de lots corrigé) | **OK** |

### Pourquoi les deux KO, et ce que je propose

- **Le plafond vient des données, pas du réglage.** Le rang médian d'un vrai soumissionnaire est 51 sur environ 1 900 candidates. J'ai essayé sans gain :
  - 3 variables de plus (activité sur 2 mois, région récente, région × famille) ;
  - un modèle non linéaire (gradient boosting), qui fait 32,8 % contre 32,7 %.

  Corrections déjà appliquées : élargissement des candidates, effet commun (critère 3 passé de KO à OK), calibration.
- **Options (à toi de décider)** :
  1. **Revoir les seuils** du feu vert sur ce qui est mesurable avec les PV : par exemple top 15 ≥ 33 % et gagnant dans la liste ≥ 40 %, à battre aux étapes suivantes.
  2. **Ajouter une source** : la liste des sociétés qui ont retiré le dossier de consultation, si le portail la donne pour l'AO (je ne l'ai pas vérifié). C'est le seul signal qui dirait qui s'intéresse à *cet* AO.
  3. **Laisser l'utilisateur ajouter** « nos concurrents habituels » sur un AO. La page le permet déjà en partie : on peut changer une probabilité.
  4. **Allonger la liste** : le gagnant y est 51 % du temps dans un top 50 et 61 % dans un top 100, mais la liste perd son sens.

## Limites

- **Mémoire et temps** : la mémoire de l'historique prend 213 Mo et 13 s à construire (préchargée au démarrage). Une première analyse de REJEU prend 20 à 40 s, car l'index « jusqua- » est recalculé à chaque fois sans être rangé. Les analyses sont ensuite gardées en mémoire (16 au plus).
- **Groupes peu nombreux** : avec des probabilités calibrées, peu de sociétés atteignent 15 %, donc peu de groupes (0 ou 1 sur la plupart des AO). Ce seuil peut être baissé dans `probables.REGLAGES["seuil_allies"]`.
- **Indépendance entre membres d'un même groupe** : elle n'est pas modélisée, hormis par l'effet commun. Les marchés de 16 offres et plus sont sous-estimés.
- **Table de calibration** : son extrémité haute repose sur peu de sociétés (p de la formule > 0,9 → 0,999).
- **Liste « Test » invisible en local** : sur /ao-ouverts, un visiteur rattaché d'office à un autre client ne voit pas les AO « Test ». C'est le comportement existant ; j'ai donc vérifié le bouton « Analyser » par le code, et la page en ouvrant `/copilote/analyse/REJEU-xx` directement.
- **Ajustement** : les coefficients sont ajustés sur 2025 et mesurés sur 2026. Pour les AO réels de 2027, il faudra relancer `mesures_probables ajuster --annee 2026`.

## Prochaine étape

Étape 5 (taux de chaque société), après ta décision sur les seuils du feu vert.

## À tester par Hamza

1. Lancer le site en local avec `COPILOTE_ACCES` défini, entrer le code, ouvrir `/ao-ouverts` avec un visiteur qui voit le client **Test**. Chaque AO doit avoir un bouton **Analyser**. Sans le code copilote, le bouton ne doit pas apparaître.
2. Ouvrir **REJEU-11** (≈ 30 s la première fois) :
   - ① objet, acheteur, coupure au 25/02/2026 ;
   - ② 20 marchés similaires, avec « Détail du score » ;
   - ③ la fourchette (« Entre 1 et 10 sociétés… ») et le graphique, puis un groupe avec son chef de file et sa stratégie passée, et les sociétés individuelles ;
   - ④ et ⑤ affichent « à venir ».
3. Dans ③ :
   - décocher une société : la ligne est barrée, la fourchette se recalcule et le calcul d'origine reste rappelé, barré ;
   - mettre une probabilité à 90 % : le champ devient orange et « calculée : x % » s'affiche avec un bouton Rétablir ;
   - « Tout rétablir » remet tout à l'origine.
4. Ouvrir « Pourquoi x % » sur une société : comptes bruts, apport de chaque variable, formule et calibration. Vérifier 2 ou 3 chiffres dans le tableau de bord (marchés chez l'acheteur, dernière offre).
5. Déplier « Ce qui s'est réellement passé » : sociétés venues ✓/✗, gagnant prévu ou non, vrai nombre comparé à la fourchette, membres de groupe venus. Comparer avec le PV réel (la référence est affichée).
6. Ouvrir **REJEU-01** (alloti) : un choix de lot s'affiche. Passer d'un lot à l'autre.
7. Sur téléphone (390 px), refaire 2 et 3 : rien ne doit dépasser à droite.
8. Lire la section FEU VERT et choisir une option pour les deux critères KO.
