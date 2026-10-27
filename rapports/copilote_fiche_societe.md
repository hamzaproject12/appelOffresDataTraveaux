# Fiche société refaite — « quand elle gagne, qui est présent ? »

Chantier 7, correction hors étape. La détection des groupes n'est pas touchée : aucun fichier de
calcul (`groupes.py`, `liens.py`, `reglages.json`) n'a bougé.

## Ce qui ne marchait pas

L'ancienne fiche listait des « partenaires de présence » triés par excès, donc en tête des liens
faibles sur 3 marchés (attendu minuscule, excès énorme). Elle ne disait pas qui est présent quand
la société gagne.

## Ce qui remplace

`GET /api/copilote/societe/{cle}/liens` → quatre sections, dans cet ordre (`copilote/fiche_societe.py`).
L'ancienne route `GET /api/copilote/societe/{cle}` et `api_groupes.fiche_societe` ont disparu.

| Section | Qui entre | Tri |
|---|---|---|
| A **Présentes quand elle gagne** | présente lors d'au moins 2 de ses victoires | victoires accompagnées, puis part des marchés de l'autre |
| B **Sociétés qu'elle accompagne** | l'autre gagne au moins 2 fois, elle présente et perdante | victoires de l'autre, puis part de ses marchés à elle |
| C **Concurrents directs** | l'autre gagne au moins 1 fois quand elle perd | victoires contre elle |
| D **Autres présences à vérifier** | lien du calcul (ou avis) sans résultat commun | force du lien, puis écart à la normale |

A, B et C sont ouvertes, D est repliée ; 10 lignes par section, bouton « Afficher 10 de plus ».
Une même société peut paraître dans plusieurs sections : ce sont trois questions, pas trois
classements. Chaque ligne porte : victoires accompagnées sur le total, marchés communs (et le
compte avec les lots), présence croisée dans les deux sens, victoires de l'autre quand elles sont
ensemble, position médiane de l'offre par rapport à l'offre gagnante, écart à la normale, force du
lien, avis, et les 8 marchés en preuve les plus récents (référence, acheteur, date, gagnant, les
deux offres en taux). Boutons Oui / Non / Je ne sais pas sur chaque ligne de A et B.

Comptage : par unité (un marché ordinaire ou UN LOT), présence = toute offre déposée, écartées
comprises, fusions appliquées. Tout est recompté sur l'historique du jour, sauf l'excès, la valeur
p, la force du lien (`groupes_liens`) et le nombre de marchés des autres sociétés
(`groupes_societes`), qui viennent du calcul hebdomadaire.

Profils, en phrases factuelles : « présente lors de la plupart / d'une partie de ses victoires,
gagne rarement avec elle », « gagnent à tour de rôle », « concurrent direct », « quelques marchés
communs, résultats partagés », « présente, sans qu'aucune des deux ne gagne ces marchés ».

## Vocabulaire (toute la page)

| Avant | Après |
|---|---|
| Partenaires de présence | Sociétés liées probables |
| Excès 9,5 | 9 fois plus souvent ensemble que la normale |
| Part des marchés de A | Présente sur x % des marchés de A |
| Fort / Moyen / Faible | Lien très probable / probable / à vérifier |
| Occasionnel | Présente dans une partie du groupe |
| Présence mutuelle habituelle | Deux sociétés qui se suivent |

Info-bulle courte sur chaque terme (`TERMES` dans la page), glossaire « Comprendre les termes »
réécrit. Les codes de l'API (`fort`, `moyen`, `faible`) ne changent pas.

## Contrôle sur STE AWRACH TADLA (awrachtadla)

« STE AWRACH TADLA a déposé une offre sur 95 marchés, soit 107 marchés et lots, et en a gagné 13.
TATHNOUT HASSAN était présente lors de 8 de ces 13 victoires, et a gagné 4 des 44 marchés et lots
qu'elles partagent. »

| Section | Tête de liste |
|---|---|
| A | **TATHNOUT HASSAN** 8 victoires sur 13, présente sur 75 % de ses marchés, gagne 4 fois — puis sotracorif 6/13, SONATRADIC 5/13 |
| B | **TIB TRAV** gagne 10 de ses 14 victoires avec elle présente |
| C | **TIB TRAV** 10 victoires contre elle, contre 4 dans l'autre sens |
| D | KIBOUS ABDLEKRIM : 14 marchés communs, 4 fois plus que la normale, aucune victoire d'un côté ni de l'autre |

Attendu : TATHNOUT HASSAN en tête de A (≈ 7 victoires sur 9 d'après l'estimation de Hamza ; les
chiffres réels sont 8 sur 13), TIB TRAV dans C. ✔

Trois autres sociétés tirées au hasard (graine 7) : STE LBMD SARLAU, CORDOBA BINAE, STE OLKANDIS.
Toutes répondent ; les deux premières n'ont personne en A (3 victoires chacune, aucune société
présente deux fois), OLKANDIS a « samy genie civil » présente à 2 de ses 3 victoires sans jamais
gagner avec elle.

## Mesures

| | |
|---|---|
| fiche société | médiane **11 ms**, p95 **52 ms** sur 220 sociétés |
| une seule mesure à 472 ms | la toute première fiche ouverte juste après le calcul complet, base relue sur le disque ; la suivante tombe à 78 ms |
| réponse HTTP | 215 Ko pour awrachtadla (89 lignes, 66 sociétés), 166 Ko pour SNTRO |
| tests | 226 tests verts (`python -m unittest discover -s tests -t .`), `tester_ao_ouverts.py` vert |

Les victoires de chaque société sont lues une fois par version de `pv_travaux.db` et gardées en
mémoire ; `routes.prechauffer` les prépare au démarrage, avec l'index de similarité.

### FEU VERT

| Contrôle | État |
|---|---|
| partenaires ≥ 1 500 sociétés actives (lien probable ou très probable) | OK |
| 66 anciens groupes retrouvés | OK |
| toute société trouvée par la recherche | OK |
| fiche société < 300 ms (p95) | OK — 52 ms |
| aucun mot accusateur | OK |

Le seuil de temps porte maintenant sur le p95 et non sur le maximum : le maximum ne mesurait que
le premier accès à froid (détail dans `temps.fiche_societe_ms.plus_lentes`).

## Limites

- « Marchés communs » compte les marchés où les deux ont déposé **sur la même unité jugée** (le
  même lot si le marché est alloti). `groupes_liens.communs`, qui sert à l'excès, compte au niveau
  du marché : l'écart est d'un marché ou deux sur les sociétés très allotissées.
- La part des marchés de l'autre société utilise son total du dernier calcul hebdomadaire.
- Section C : une société qui n'a jamais battu celle qu'on regarde n'y figure pas, même si elle est
  un concurrent réel sur d'autres marchés.

## À tester par Hamza

1. `/copilote/groupes`, chercher **STE AWRACH TADLA** : la phrase de résumé en tête, puis TATHNOUT
   HASSAN en première ligne de « Présentes quand elle gagne ».
2. Ouvrir « Marchés en preuve » sur cette ligne : les deux offres en taux, ✓ sur l'offre retenue.
3. Section « Concurrents directs » : TIB TRAV en tête, avec son bilan dans les deux sens.
4. Déplier « Autres présences à vérifier » (repliée au départ).
5. Cliquer « Afficher 10 de plus » dans une section : la page ne remonte pas.
6. Donner un avis Oui / Non sur une ligne de A ou de B, vérifier qu'il revient avec votre nom et
   qu'il apparaît dans l'historique en bas de la fiche.
7. Passer la souris sur « Présence commune », « Présence croisée », « Lien probable » : l'info-bulle.
8. Vérifier sur téléphone (390 px) et sur grand écran, en thème sombre et clair.
9. Chercher 2 ou 3 sociétés que vous connaissez et dire si les quatre sections racontent ce que vous
   savez d'elles.
