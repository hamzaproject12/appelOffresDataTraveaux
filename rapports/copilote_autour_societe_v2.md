# Copilote — règle de classement corrigée (noyau, accompagnatrices)

Hamza a validé le travail ; la règle « plus présente quand elle gagne » excluait les partenaires qui gagnent à
tour de rôle. Elle est remplacée, à l'identique en JavaScript (`statique/groupes_calcul.js`) et en Python
(`copilote/autour_societe.py`).

## Règle (chaque seuil est un réglage de la page)

| Lecture | Condition, dans cet ordre |
|---|---|
| **noyau** (gagnent à tour de rôle) | Y présente sur ≥ 3 victoires de X, A ÷ B ≥ 1,3, ET X présente sur ≥ la moitié des victoires de Y |
| **accompagnatrice** | Y présente sur ≥ 3 victoires de X, et (A ÷ B ≥ 1,3 ou C ≥ 60 %), sans être noyau |
| **concurrent direct** | Y gagne ≥ 3 fois quand X est présente, ET A ÷ B < 1 |
| neutre | le reste |

La condition « au plus 2 victoires contre X » est supprimée. Réglages : `min_victoires` 3, `rapport_min` 1,3
(commun au noyau et à l'accompagnatrice), `part_noyau` 0,5, `c_min` 0,6, `min_victoires_y` 3,
`rapport_concurrent` 1, `taille_groupe` 9.

**Groupe** : X + noyaux et accompagnatrices, classés par victoires de X avec Y présente puis A ÷ B. Au plus
**9 sociétés, X comprise** (donc 8 partenaires) ; les suivantes sont dans « Autres sociétés proches », repliée.
La page sépare « Noyau » (X et ses noyaux) et « Accompagnatrices ». Si « au plus 9 » devait s'entendre sans X,
c'est le réglage `taille_groupe` à 10.

« X présente sur les victoires de Y » : l'API envoie, pour chaque société, **toutes ses victoires** dans la base
(`societes[Y].victoires`, un entier). La présence de X sur ces victoires se lit déjà dans les unités de X
(celles que Y a gagnées et où X a déposé) : rien d'autre n'est envoyé, la réponse ne grossit que de quelques
octets par société. L'ETag de la route contient maintenant une version du format (`autour_societe.FORMAT`),
pour qu'un navigateur ne garde pas une ancienne réponse après un changement de format.

## Contrôles

| Contrôle | Résultat |
|---|---|
| awrachtadla : noyau = TATHNOUT HASSAN, STE SONATRADIC | OK (avec X) |
| accompagnatrices dont SONACER TRAV, TIZIZOUA, RICANIER, MARGHICH TRAVAUX | OK : + EQUIPE MAROCAINE CONSTRUCTION, STE GROUMOUINE |
| TIB TRAV concurrent direct | OK (10 victoires contre elle, A ÷ B 0,66) |
| groupe de 9 noms au plus | OK : 9, avec X ; « autres » : STE SORAMETE DE TRAVAUX, ENTREPRISE SALMI OMAR |
| tathnouthassan : AWRACH TADLA et SONATRADIC dans le noyau, présentes sur 4 de ses 4 victoires | OK (A ÷ B 1,36 et 1,58 ; X présente sur 62 % et 100 % de leurs victoires) |
| tathnouthassan : TIB TRAV concurrent direct | OK (5 victoires contre elle, A ÷ B 0,97) |
| Python = JavaScript sur 20 sociétés réelles | OK (avec les réglages d'origine, puis d'autres) |

Valeurs d'awrachtadla : 107 unités, 13 victoires ; TATHNOUT HASSAN A 8/13, B 44/107, C 44/60 (inchangé).

## Page

- Colonne de gauche : « Groupes détectés sur toute la base (calcul de la semaine) » + une phrase d'aide.
- Fiche de droite : « Autour de cette société (calcul instantané) » + une phrase d'aide.
- Colonne « Position de l'offre » : info-bulle demandée, sur l'en-tête et sur chaque cellule.
- Ligne dépliée : « Y a gagné N unités dans la base ; X était présente sur M d'entre elles (P %) ».
- « Comment c'est calculé » décrit les quatre lectures avec les seuils en cours.

## Tests

- 253 tests verts (`python -m unittest discover -s tests -t .`), `tester_ao_ouverts.py` vert.
- `tests/test_autour_societe.py` réécrit : cas faits à la main (noyau, accompagnatrice par A ÷ B ou par C,
  concurrent direct, taille du groupe et « autres », classement), 20 jeux aléatoires où les quatre lectures
  apparaissent, 20 sociétés réelles, contrôles des deux sociétés, route.

### FEU VERT

| Contrôle | État |
|---|---|
| contrôles d'awrachtadla et de tathnouthassan | OK |
| Python = JavaScript, 20 sociétés réelles et 20 jeux aléatoires | OK |
| groupe ≤ 9 sociétés (réglable) | OK |
| calcul navigateur < 200 ms pour la plus grosse société | OK (test inchangé, ~8 ms) |
| routes hors groupes toujours protégées par code | OK |
| aucun mot accusateur | OK |

## Limites

- « X présente sur la moitié des victoires de Y » s'appuie sur les victoires de Y dans toute la base ; une
  société qui n'a jamais gagné ne peut pas être noyau (elle est au mieux accompagnatrice).
- Le groupe dépend de A ÷ B ≥ 1,3 : une société juste sous 1,3 (AGROSSAR pour awrachtadla) en est exclue ; c'est un seuil réglable.
- `rapport_min` est un seul réglage pour le noyau et l'accompagnatrice, comme dans la demande (1,3 pour les deux).

## À tester par Hamza

1. `/copilote/groupes#s=awrachtadla` : « Noyau (gagnent à tour de rôle) » = elle, TATHNOUT HASSAN, STE SONATRADIC ;
   « Accompagnatrices » sous le noyau ; « Autres sociétés proches (2) » repliée.
2. Même page pour TATHNOUT HASSAN : AWRACH TADLA et SONATRADIC dans le noyau, TIB TRAV « concurrent direct » dans le tableau.
3. « Réglages du calcul » : mettre la taille du groupe à 5 puis à 12 ; mettre « A ÷ B d'au moins » à 1,2 : AGROSSAR (A ÷ B juste sous 1,3) rejoint les sociétés proches.
4. Survoler « Position de l'offre » : l'info-bulle demandée.
5. Les deux titres de colonnes (« calcul de la semaine » / « calcul instantané ») et leur phrase d'aide.
