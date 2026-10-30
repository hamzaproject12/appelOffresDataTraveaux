# Copilote — fiche société « autour d'elle » et page des groupes ouverte aux visiteurs

## Ce qui change

**A. Calcul dans le navigateur.** Choisir une société sur `/copilote/groupes` charge ses données brutes
(`GET /api/groupes/societe/{cle}/donnees`) et `statique/groupes_calcul.js` calcule, pour chaque autre société Y :
A, B, C, A ÷ B, victoires ensemble, position médiane de l'offre, lecture. Les seuils se règlent dans
« Réglages du calcul » et tout se recalcule à la frappe. Le même calcul existe en Python
(`copilote/autour_societe.py`, pour l'étape 5).

**B. Page visible par tous les visiteurs connectés.** Le cookie visiteur suffit pour la page, ses API
(liste, famille, groupe, société, avis, données) et le script de calcul. Tout le reste du copilote reste
sous COPILOTE_ACCES. Bandeau permanent, `noindex` (balise + en-tête), auteur d'un avis = nom du visiteur,
requêtes comptées dans les limites du serveur, journal avec l'adresse IP. L'onglet « Groupes » est
visible pour tous (`dashboard.html`, `ao_ouverts.html`).

## Contrôles demandés (awrachtadla = STE AWRACH TADLA)

| Contrôle | Attendu | Obtenu |
|---|---|---|
| unités / victoires | 107 / 13 | 107 / 13 ✔ |
| TATHNOUT HASSAN, A | 8/13 = 62 % | 8/13 = 62 % ✔ |
| TATHNOUT HASSAN, B | 44/107 = 41 % | 44/107 = 41 % ✔ |
| TATHNOUT HASSAN, C | 44/60 = 73 % | 44/60 = 73 % ✔ |
| TIB TRAV | concurrent direct | concurrent direct (10 victoires contre elle, 4 pour elle) ✔ |
| groupe : SONACER TRAV, TIZIZOUA, RICANIER, MARGHICH, SALMI OMAR, EQUIPE MAROCAINE | présents | présents ✔ |
| groupe : **TATHNOUT HASSAN** | présent | **absent** ✘ (voir ci-dessous) |

Groupe obtenu avec les seuils d'origine : STE AWRACH TADLA + EQUIPE MAROCAINE CONSTRUCTION, STE GROUMOUINE,
MARGHICH TRAVAUX, STE RICANIER, ENTREPRISE SALMI OMAR, STE SONACER TRAV, STE SORAMETE DE TRAVAUX, STE TIZIZOUA.

**Écart sur TATHNOUT HASSAN.** Avec les trois seuils écrits dans la demande (≥ 3 victoires, A ÷ B ≥ 1,5,
au plus 2 victoires contre elle), TATHNOUT HASSAN est « neutre », pour deux raisons :
A ÷ B = 0,615 ÷ 0,411 = **1,497** (affiché « 1,50 », juste sous 1,5) et elle gagne **4** unités contre
STE AWRACH TADLA (seuil : 2). Le groupe est défini comme « la société + celles dont la lecture est *plus
présente quand elle gagne* » ; je n'ai pas modifié la règle pour faire entrer TATHNOUT. Deux sociétés
non attendues y entrent (GROUMOUINE, SORAMETE : 3 victoires, A ÷ B de 3,5 et 2,5, peu d'unités en commun).
Aucun groupe de la détection hebdomadaire ne contient ces sept noms : la liste attendue ne vient donc pas
de `groupes`. À trancher : voir « Décisions à prendre ».

## Mesures

| | |
|---|---|
| plus grosse réponse (rittrav : 285 unités, 1 379 sociétés) | 396 Ko bruts, **99 Ko** compressés (gzip) |
| awrachtadla (107 unités, 510 sociétés) | 147 Ko bruts, 37 Ko compressés |
| calcul JavaScript (Node 20), rittrav | **7,8 ms** (cible < 200 ms) ; awrachtadla 3,6 ms |
| première ouverture d'une société après démarrage | 3 à 4 s (parcours complet pour le total d'unités de chaque société) ; préchauffé au démarrage |
| ouvertures suivantes | ~0,1 s côté serveur, puis 304 (ETag) ou mémoire |

La réponse dépend de l'historique, des fusions, du calcul hebdomadaire et des avis de la société : l'ETag
change dès qu'un avis est donné.

## Définitions (écrites aussi dans la page, « Comment c'est calculé »)

- unité = marché ordinaire ou lot ; présence = toute offre déposée, écartée comprise ; fusions appliquées ;
- A = victoires de X avec Y présente ÷ victoires de X ; B = unités communes ÷ unités de X ;
  C = unités communes ÷ unités de Y dans toute la base ;
- position = médiane de (taux de Y ÷ taux de X − 1) sur les victoires de X ;
- « plus présente quand elle gagne » : ≥ 3 victoires de X avec Y, A ÷ B ≥ 1,5, ≤ 2 victoires de Y contre X ;
- « concurrent direct » : ≥ 3 victoires de Y contre X **et** plus que X n'en a quand Y est présente
  (règle que la demande ne chiffrait pas ; son seuil, 3, est le quatrième réglage de la page) ;
- sinon « neutre ».

## Tests

- 247 tests verts (`python -m unittest discover -s tests -t .`), `tester_ao_ouverts.py` vert.
- `tests/test_autour_societe.py` : un cas calculé à la main ; 20 jeux aléatoires, Python = JavaScript, avec
  les réglages d'origine puis d'autres ; **20 sociétés réelles** (awrachtadla + 19 réparties), Python =
  JavaScript à 1e-12 ; contrôles d'awrachtadla ; temps de la plus grosse société < 200 ms ; la route
  (contenu, gzip, 304, avis qui change l'ETag, 404, historique intact). Les tests sur la vraie base et sur
  JavaScript sont **ignorés** (pas verts) si `pv_travaux.db` ou Node manquent.
- `tests/test_acces.py` : la liste exacte des routes ouvertes au cookie visiteur seul est figée ; toutes
  les autres routes du copilote répondent 401/302 sans code.
- `tests/test_page_groupes.py` : bandeau, noindex, avis au nom du visiteur sans code, comptage et journal.

### FEU VERT

| Contrôle | État |
|---|---|
| 107 unités, 13 victoires ; A, B, C de TATHNOUT HASSAN ; TIB TRAV concurrent direct | OK |
| groupe d'awrachtadla = les sept noms attendus | **PAS OK** : TATHNOUT HASSAN absent (écart expliqué ci-dessus) |
| Python = JavaScript sur 20 sociétés | OK |
| calcul navigateur < 200 ms pour la plus grosse société | OK — 7,8 ms |
| routes non-groupes toujours protégées par code | OK (test) |
| aucun mot accusateur (page, API) | OK (test existant) |
| lisible à 390 px et 1 440 px | OK (vérifié dans le navigateur) |

## Limites

- Les données de la société sont chargées entières (jusqu'à 99 Ko compressés) ; le tableau n'affiche que 25
  lignes à la fois.
- Le total d'unités de Y (pour C) vient de l'historique du jour, comme le reste du calcul.
- Les avis sont comptés par paire ; une société avec avis mais sans unité commune apparaît sous le tableau.
- Un visiteur peut donner un avis sans code : tout nom saisi à l'entrée est accepté, rien ne l'authentifie.

## Décisions à prendre

1. TATHNOUT HASSAN dans le groupe ? Il suffirait par exemple d'un réglage « au plus 4 victoires contre elle »
   et « A ÷ B ≥ 1,49 », ou d'une lecture « gagnent à tour de rôle » comptée dans le groupe. Dites laquelle.
2. Règle du « concurrent direct » : confirmer celle ci-dessus.

## À tester par Hamza

1. Ouvrir `/copilote/groupes` sans code copilote (visiteur simple) : la page s'ouvre, le bandeau est en haut.
2. Chercher **STE AWRACH TADLA** : phrase de résumé, puis le groupe sous forme de pastilles.
3. Tableau : TATHNOUT HASSAN en tête (62 %, 41 %, 73 %) ; survoler ou toucher un pourcentage montre « 8 / 13 = 62 % ».
4. Cliquer la ligne **TIB TRAV** : « concurrent direct » et les marchés en preuve.
5. « Réglages du calcul » : mettre « au plus … victoires contre la société » à 4 puis « A ÷ B » à 1,49 : TATHNOUT
   entre dans le groupe aussitôt ; la valeur d'origine est rappelée barrée ; « Revenir aux valeurs d'origine ».
6. Oui / Non / Je ne sais pas sur une ligne : l'avis revient avec votre nom de visiteur.
7. `/copilote` et `/api/copilote/etat` demandent toujours le code.
8. À 390 px : le tableau devient une liste de fiches lisible, sans défilement horizontal.
