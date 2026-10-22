# Copilote — étape 2 : groupes de présence commune (calcul)

Branche `copilote`, rien de commité. Tests : `python -m unittest discover -s tests -t .` → 73 OK (59 + 14).
`python tester_ao_ouverts.py` → tous les contrôles passent.
Chiffres reproduits par `python -m copilote.mesures_groupes` (19 s, aucune écriture).

Un groupe est un **fait statistique de présence commune**, pas une accusation : partenaires habituels,
même dirigeant, groupement, ou concurrents qui visent les mêmes acheteurs. Les avis humains tranchent.

## Ce qui est en place

| Fichier | Rôle |
|---|---|
| `copilote/fusions_validees.csv` | les 25 fusions validées (copiées depuis ton fichier, toutes les clés existent) |
| `copilote/fusions.py` | lecture du CSV (chaînes suivies, cycle refusé) ; fichier absent ⇒ aucune fusion |
| `copilote/donnees.py` | `charger_unites()` applique les fusions par défaut, aux offres et à l'attributaire |
| `copilote/regions.py` | région déduite du nom de l'acheteur (mots-clés : villes, provinces, régions) |
| `copilote/groupes.py` | matrice, paires, cliques, Louvain, confirmations, score, avis, écriture |
| `copilote/recalculer.py` | `python -m copilote.recalculer groupes [--jusqua AAAA-MM-JJ] [--seuil 0.5] [--tirages 200]` |
| `copilote/mesures_groupes.py` | les chiffres de ce rapport et le tableau FEU VERT |
| `planificateur.py` | `recalcul_hebdomadaire()` : après la mise à jour quotidienne, si le dernier recalcul a 7 jours ou plus (marque `derniere_maj_groupes.txt`), dans un processus à part |

`copilote_calcul.db` : tables `groupes_calculs`, `groupes_paires`, `groupes`, `groupe_membres`,
`groupe_preuves`, rangées par identifiant de calcul (`courant`, `jusqua-2025-12-31`). Un recalcul ne
remplace que les lignes de son identifiant. Durée : 2 à 3 s.
`copilote_retours.db` : table `avis_groupes` créée (`cle_a < cle_b`, `reponse` oui/non/nsp, `commentaire`,
`auteur`, `cree_le`, `groupe`). Elle est vide.

## Méthode (seuil 50 %)

1. **Matrice** sociétés × marchés (scipy). Une colonne = un **marché**, pas un lot. Toute offre déposée
   compte, même écartée. `X·Xᵀ` donne les marchés communs de toutes les paires.
2. **Paire** retenue si elle a au moins 5 marchés communs, et si la part des marchés de A où B est présent
   ET celle des marchés de B où A est présent atteignent toutes deux le seuil.
3. **Groupe** = clique maximale d'au moins 3 sociétés : chaque paire du groupe est retenue. Il faut aussi
   au moins 5 marchés où au moins 3 membres sont présents. **Louvain** range les cliques qui se chevauchent
   en *familles*. J'ai choisi la clique plutôt que la communauté seule, parce qu'une communauté Louvain
   peut réunir des sociétés qui ne se croisent jamais directement.
4. **Confirmations**, calculées sur les marchés de preuve (au moins 3 membres présents) :
   - nombre d'acheteurs, de régions et de domaines (lettre de qualification) ;
   - hors profil : présences dans un domaine qui fait moins de 10 % des marchés de la société *sans* le groupe ;
   - qui gagne : chef de file si un membre a au moins 60 % des victoires du groupe, rotation sinon, et
     « peu de victoires » en dessous de 3 ;
   - taux de victoire de chaque membre avec et sans le groupe ;
   - position des offres non gagnantes, en points de taux, par rapport au gagnant et au prix de référence ;
   - **hasard** : 200 tirages. Chaque membre est remplacé par une société d'activité comparable (de ½ à 2 fois
     ses présences), active chez les mêmes acheteurs sur la même période. Le rapport au hasard vaut
     (observé + 1) / (attendu + 1).
5. **Score de 0 à 100** = force 20 + volume 15 + hasard 30 + contextes 15 + hors profil 5 + gains 15.
   Chaque composante est entre 0 et 1 ; le détail est dans `groupes.score()` et stocké pour chaque groupe.
6. **Avis** : un avis « oui » retient la paire même sous le seuil, un avis « non » l'exclut. Pour une même paire,
   c'est le dernier avis tranché qui compte. Avec `--jusqua`, seuls les avis donnés jusqu'à cette date sont lus.
   L'accord score/avis se mesure sur un calcul fait *sans* avis.
7. **`--jusqua`** : seuls les marchés datés jusqu'à ce jour inclus sont gardés, et les dates douteuses sont
   écartées. Les profils, le hasard et les avis sont calculés dans la même fenêtre.

## Données

17 980 marchés, 11 577 sociétés, 151 645 présences. Les 25 fusions sont appliquées (25 sociétés en moins).
Région trouvée pour 76,8 % des marchés, domaine pour 40,0 % : les autres marchés n'ont pas de qualification.

## Seuils 30 / 50 / 70 %

| seuil | paires ≥ 5 marchés | retenues | groupes | sociétés | familles | taille méd. | max | 3–7 | score méd. | top 50 : rapport méd. | top 50 : p ≤ 0,05 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 30 % | 41 806 | 2 797 | 516 | 815 | 327 | 4 | 13 | 94,2 % | 45 | 10,2 | 100 % |
| **50 %** | 41 806 | 564 | **66** | 185 | 207 | **3** | 9 | 98,5 % | 54 | 6,0 | 98 % |
| 70 % | 41 806 | 167 | 27 | 72 | 83 | 3 | 6 | 100 % | 60 | 4,2 | 88,9 % |

« Familles » compte aussi les paires isolées. Les 66 groupes à 50 % se rangent en 45 familles.

## Distribution des scores (50 %)

| score | 10–19 | 20–29 | 30–39 | 40–49 | 50–59 | 60–69 | 70–79 | 80–89 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| groupes | 2 | 4 | 9 | 13 | 13 | 14 | 10 | 1 |

Composantes moyennes : force 0,53 · volume 0,65 · hasard 0,64 · contextes 0,46 · hors profil 0,02 · gains 0,40.
Répartition des victoires : peu de victoires 28, rotation 26, chef de file 12.

## Top 20 (50 %)

| # | score | famille | membres | marchés ≥3 / complet | acheteurs | régions | domaines | victoires | rapport au hasard (p) |
|---:|---:|---:|---|---:|---:|---:|---|---|---:|
| 1 | 81 | 17 | ATELIERS INDUSTRIELS CHER…, CHAMAL SIGNALISATION, INJAZ WORKS, LA LIGNE BLANCHE, STE PINTAPLANA MAROC | 153 / 80 | 65 | 11 | X | rotation — INJAZ WORKS 29,9 % de 137 | 15,6 (0,005) |
| 2 | 78 | 26 | 4K INVEST, RH3M SARLAU, SAT TELECOM | 24 / 24 | 22 | 8 | — | rotation — SAT TELECOM 50 % de 8 | 24,6 (0,005) |
| 3 | 78 | 5 | MOROCCO ENERGY AND MARITI…, NORVIA, RABBES, SOZIFI DES TRAVAUX DIVERS, TECOS MARITIME, TRAVAUX BATIMENT PUBLIC M… | 20 / 9 | 13 | 8 | EJ | rotation — TECOS MARITIME 38,5 % de 13 | 8,6 (0,005) |
| 4 | 77 | 66 | FOREFIL, GFTR, SBTP | 28 / 28 | 22 | 3 | B | rotation — GFTR 50 % de 12 | 23,8 (0,005) |
| 5 | 76 | 28 | ALBINAE GREEN, CH-TRA, TECHNIQUE GENIE BAT | 21 / 21 | 16 | 5 | ANQ | chef de file — ALBINAE GREEN 66,7 % de 3 | 21,7 (0,005) |
| 6 | 76 | 49 | BAB ALMAJD, SOBENMED, STE TRAMANE SARL | 12 / 12 | 7 | 1 | A | rotation — BAB ALMAJD 40 % de 5 | 10,1 (0,005) |
| 7 | 73 | 5 | HE2B, MOROCCO ENERGY AND MARITI…, NORVIA, RABBES, SOZIFI DES TRAVAUX DIVERS, TRAVAUX BATIMENT PUBLIC M… | 18 / 5 | 12 | 8 | EJ | rotation — TRAVAUX BATIMENT PU… 30 % de 10 | 7,2 (0,005) |
| 8 | 73 | 2 | STE ALLUM SADAKA, STE A N B LOCATION, STE ART D'IMPRIMERIE ET D…, EL ASSRY FATIMA, MEKOUAR AHMED, STE MPUB SARL, STE PROTECTION DU CENTRE …, STE RIYAD EXLENT SARL AU, SOCIETE T.D.T.F SARL A.U | 15 / 4 | 9 | 2 | — | rotation — SOCIETE T.D.T.F SAR… 55,6 % de 9 | 7,7 (0,005) |
| 9 | 73 | 22 | DHD, STE GENATR, STE M2TB SARL AU | 15 / 15 | 9 | 3 | — | chef de file — DHD 100 % de 9 | 10,9 (0,005) |
| 10 | 72 | 23 | STE KHADAMAT COMPANY, LEVEL TRAV SAHEL, SAOUAID LKHAYR | 25 / 25 | 18 | 6 | AO | rotation — SAOUAID LKHAYR 42,9 % de 7 | 8,0 (0,005) |
| 11 | 70 | 45 | ALFA BEAUTE, MAROC INFRA ROUTIERE, STE MEDLA | 47 / 47 | 15 | 8 | — | rotation — ALFA BEAUTE 46,2 % de 13 | 22,3 (0,005) |
| 12 | 68 | 12 | STE BOUBAMOU TRAVAUX DIVE…, CHANTIERS GEANTS, GRANDE SOCIETE DES EQUIPE… | 25 / 25 | 13 | 2 | — | rotation — GRANDE SOCIETE DES … 50 % de 10 | 15,6 (0,005) |
| 13 | 68 | 5 | MOROCCO ENERGY AND MARITI…, NORVIA, RABBES, SIGMA MARITIME SERVICES, SOZIFI DES TRAVAUX DIVERS, TRAVAUX BATIMENT PUBLIC M… | 17 / 9 | 12 | 8 | E | rotation — TRAVAUX BATIMENT PU… 42,9 % de 7 | 7,0 (0,005) |
| 14 | 67 | 5 | aquasub services, HE2B, MOROCCO ENERGY AND MARITI…, NORVIA, RABBES, SOZIFI DES TRAVAUX DIVERS | 17 / 6 | 12 | 8 | E | rotation — SOZIFI DES TRAVAUX … 25 % de 8 | 6,1 (0,005) |
| 15 | 67 | 21 | COGIMAROC, COMPAGNIE DE TRANSPORT DE…, STE EDMOND AFRICA, HNTL, SOCITE AFRICAINE DE SERVI… | 11 / 6 | 4 | 3 | — | rotation — STE EDMOND AFRICA 33,3 % de 3 | 9,6 (0,005) |
| 16 | 66 | 30 | STE ARASCOM MULTISERVICES…, STE GLOBALE CONSTRUCTION …, STE LAKLIDA TRAVAUX SARL … | 37 / 37 | 24 | 1 | — | rotation — STE ARASCOM MULTISE… 41,7 % de 12 | 7,1 (0,005) |
| 17 | 66 | 5 | aquasub services, MOROCCO ENERGY AND MARITI…, NORVIA, RABBES, SIGMA MARITIME SERVICES, SOZIFI DES TRAVAUX DIVERS | 17 / 9 | 12 | 8 | E | rotation — SOZIFI DES TRAVAUX … 33,3 % de 6 | 6,9 (0,005) |
| 18 | 65 | 42 | AGROSSAR, EXPERTISE AGRICOLE AGROBE…, HYDRAU AGRICOL DE L'ORIEN… | 25 / 25 | 12 | 5 | — | chef de file — HYDRAU AGRICOL DE L… 66,7 % de 3 | 14,0 (0,005) |
| 19 | 64 | 35 | STE CONSTRULLAGE, STE GROUPE GSA SARL AU, STE RIO AMENAGEMENT, STE SMARKAND BUSINESS | 17 / 12 | 12 | 4 | A | chef de file — STE GROUPE GSA SARL… 100 % de 3 | 5,9 (0,005) |
| 20 | 63 | 55 | BOVEN FH, BUILDUNITY ENTERPRISES, NORTHERN OF PUBLIC WORKS … | 17 / 17 | 1 | 1 | — | chef de file — BOVEN FH 66,7 % de 3 | 10,4 (0,005) |

p = 0,005 est la plus petite valeur possible avec 200 tirages : aucun tirage n'a égalé le groupe.
La position des offres et les taux de victoire avec et sans le groupe sont dans la sortie de
`mesures_groupes` et stockés pour chaque groupe (`groupes.details`, `groupe_membres`). Exemple, groupe 1 :
les offres non gagnantes des membres sont en médiane à +4,5 points du gagnant (+5,9 pour les autres
soumissionnaires) ; INJAZ WORKS gagne 29,7 % de ses unités avec le groupe (138) et 50 % sans lui (14).

## Avis

Aucun avis enregistré : l'accord entre score et avis n'est pas encore mesurable. Le calcul est prêt
(`groupes.accord`) et s'affichera dès les premiers avis saisis à l'étape 3.

## Limites

- **Cliques qui se chevauchent** : la famille 5 apparaît 5 fois dans le top 20, sous des cliques de 6 sociétés
  qui diffèrent d'un membre. Le top 20 ne contient que 16 familles distinctes. La page de l'étape 3 devra
  regrouper l'affichage par famille.
- **Hors profil** presque muet (moyenne 0,02) : 60 % des marchés n'ont pas de qualification. Son poids est
  donc faible (5).
- **Chef de file** : il peut être désigné dès 3 victoires (« 66,7 % de 3 »). C'est une indication, pas une conclusion.
- **Région** inconnue pour 23 % des marchés (acheteurs nationaux : ONEE, ANEP, ONDA…). Le lieu de
  ao_ouverts.db est vide et ne peut pas compléter.
- Le groupe 8 (imprimerie, location, publicité) a la plus grande taille (9) : la base contient aussi des marchés
  qui ne sont pas des travaux.
- Stabilité : 55,7 % des groupes reviennent sur au moins un marché de 2026, mais seulement 32,8 % sur au moins 3.
  Il n'y a que 9 mois de 2026 dans la base.
- Les poids du score sont fixés a priori : aucun avis n'existe encore pour les ajuster.

## À tester par Hamza

1. `python -m copilote.recalculer groupes` doit afficher environ 66 groupes en quelques secondes. Ensuite
   `python -m copilote.recalculer groupes --jusqua 2025-12-31` doit en afficher environ 61.
2. Ouvrir `copilote_calcul.db` (DB Browser). Dans la table `groupes`, filtrer `calcul = 'courant'` et prendre
   5 groupes du top 20 que tu connais. Dans `groupe_preuves`, vérifier que les marchés listés réunissent bien
   ces sociétés.
3. Pour ces 5 groupes, noter ta réponse : liées (partenaires, même dirigeant, groupement) oui / non / je ne
   sais pas. Ce sera la première mesure de l'accord score/avis.
4. Dire si la famille 5 (MOROCCO ENERGY AND MARITIME, NORVIA, RABBES, SOZIFI…) doit apparaître comme un seul
   groupe de 7 à 8 sociétés plutôt que 6 cliques.
5. Seuil : 50 % donne 66 groupes de 3 sociétés (médiane) ; 30 % en donne 516 de 4. Lequel correspond au terrain ?
6. Sur Railway : le recalcul se lance seul après la première mise à jour quotidienne (aucune marque encore),
   puis chaque semaine. Il dépend de la boucle du planificateur : avec `MAJ_AUTO=0`, il ne tourne pas.
   Dans ce cas, lancer la commande à la main.

## FEU VERT

Les seuils ont été fixés dans `mesures_groupes.py` avant la mesure.

| critère | valeur mesurée | |
|---|---|---|
| Stabilité : groupes calculés jusqu'au 31/12/2025, part revue en 2026 (au moins 3 membres sur au moins 1 marché) ; seuil : ≥ 50 % et ≥ 3 × le hasard | **55,7 %** des 61 groupes (top 20 : 85 %) ; ensembles semblables tirés au hasard : 14,7 % (n = 3 050) | **OK** |
| Top 50 nettement au-dessus du hasard ; seuil : rapport médian ≥ 3 et ≥ 90 % à p ≤ 0,05 | rapport médian **6,0** ; **98 %** à p ≤ 0,05 | **OK** |
| Aucun groupe formé uniquement des 20 sociétés les plus actives (≥ 149 marchés chacune) | **0** groupe ; 2 groupes en contiennent une | **OK** |
| Nombre et taille raisonnables ; seuil : 20 à 500 groupes, médiane de 3 à 7, ≥ 90 % de 3 à 7 | **66** groupes, médiane **3**, 98,5 % de 3 à 7, maximum 9 | **OK** |

Aucun critère KO, donc aucune correction. Prochaine étape après validation : 3 — page /copilote/groupes.
