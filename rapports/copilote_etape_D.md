# Copilote — étape D : notre entreprise et la décision

Tests : `python -m unittest discover -s tests -t .` → **429 OK** (61 nouveaux : `test_decision.py`, `test_etape_d.py`).
`python tester_ao_ouverts.py` → tous les contrôles passent. `serveur.py` n'est pas modifié.
Mesures : `python -m copilote.mesures_decision courbes` puis `backtest` → `rapports/copilote_etape_D_mesures.json`.

**Statut : étape D livrée, avec le critère du taux fixe KO ; amélioration prévue en D2.** Le critère principal du backtest est KO (voir ci-dessous) : la règle bat « toujours 0,95 » aux 3 coûts mais pas le meilleur taux fixe de 2025. Hamza accepte l'écart pour l'instant (04/11/2026) et demande, avant la fusion, une ligne « Règle simple apprise sur 2025 » dans le tableau de choix pour comparer à l'œil (§ 3). Fusion dans `main` faite ensuite.

## FEU VERT D

| critère | résultat | seuil | |
|---|---|---|---|
| Règle « GO + prix recommandé » ≥ « toujours 0,95 » (2026) | **3 coûts sur 3** (+17 %, +41 %, +81 %) | ≥ 2 sur 3 | OK |
| Règle ≥ « meilleur taux fixe choisi sur 2025 » (2026) | **0 coût sur 3** (−13 %, −1 %, −16 %) | ≥ 2 sur 3 | **KO** (écart accepté, D2) |
| Règle ≥ les deux à la fois | **0 sur 3** | ≥ 2 sur 3 | **KO** |
| Tests, `tester_ao_ouverts.py` | verts | verts | OK |

## 1. Backtest 2026 (493 marchés attribués, marge minimale 5 %)

Gain total en M DH ; chaque ligne : gain / AO joués / taux de victoire. Seuils GO choisis sur 2025 (296 marchés), taux fixes choisis sur 2025.

| coût (× estimation) | règle GO + prix recommandé | toujours 0,95 | meilleur taux fixe de 2025, global | idem par tiers de nombre prévu | taux fixe de C2 (0,82, meilleure victoire) |
|---|---|---|---|---|---|
| 0,80 | **617** / 493 / 42,0 % | 527 / 493 / 47,9 % | **706** (1,0375) / 493 / 33,7 % | 659 / 493 / 36,1 % | 71 / 493 / 58,0 % |
| 0,85 | **497** / 493 / 42,0 % | 352 / 493 / 47,9 % | **504** (1,05) / 493 / 26,2 % | 491 / 493 / 28,4 % | −106 / 493 / 58,0 % |
| 0,90 | **319** / 492 / 43,7 % | 176 / 493 / 47,9 % | **378** (1,05) / 493 / 26,2 % | 378 / 493 / 27,0 % | −283 / 493 / 58,0 % |

- Le « taux fixe de C2 » (celui qui gagne le plus souvent) perd de l'argent dès que le coût dépasse 0,82 : maximiser la chance et maximiser le gain sont deux objectifs différents.
- Le « meilleur taux fixe de 2025 » est ici choisi pour le **gain** sous la même contrainte que la règle (jamais sous 0,82 ni sous coût × 1,05). C'est la comparaison la plus dure ; je ne l'ai pas affaiblie.
- La règle par type de marché (coût 0,85) : petits marchés (< 5 concurrents prévus, 198 marchés) 426 M, victoires 57 % ; gros marchés (295) 71 M, victoires 32 %.

## 2. Pourquoi la règle perd contre le taux fixe

La chance annoncée est bien calibrée en moyenne à chaque prix (2026, au prix recommandé : 41,3 % annoncé contre 42,0 % réel ; à 1,04 : 29,5 % contre 31,4 %). Le problème est ailleurs :
- le prix recommandé tombe presque toujours à **1,006** (10 %–90 % : 0,994 à 1,018). Le gain espéré est plat entre 1,00 et 1,06 ; le maximum par marché est un choix instable ;
- sur 2025 comme sur 2026, la chance à **1,00 pile** est surestimée (2025 : 37,6 % annoncé, 33,8 % réel ; 2026 : 42,2 % contre 38,1 %). Le gain réel est meilleur un peu plus haut : le meilleur taux fixe de 2026 après coup est 1,0425 (570 M à coût 0,85), celui choisi sur 2025 est 1,05 ;
- la calibration à 3 paramètres de C2 (logistique + terme linéaire en x) ne sait pas reproduire ce creux local ; c'est la limite déjà annoncée dans C2.

Autre constat : **la décision GO / NO-GO ne trie presque rien** dans ce backtest. Aux coûts 0,80 à 0,90, 492 ou 493 marchés sur 493 sont GO. Le gain espéré est positif presque partout parce qu'on peut toujours viser 1,00 et plus. Le choix des seuils sur 2025 est plat (25 couples essayés, beaucoup à égalité) : `gain.go = 1,5 %` et `chance.go = 10 %` sont le plus exigeant des ex æquo. La décision servira surtout quand le coût est proche de l'estimation, ou avec les signaux qui n'entrent pas dans le backtest (éligibilité, historique de l'acheteur, groupes, délai, plan de charge).

## 3. Ce qui est livré

**Entreprise** (`/copilote/entreprise`, `copilote/entreprise.py`, `copilote_retours.db`) : marge minimale (sur le coût), coût par défaut global et par famille (repli sur le global, dit sur la page), plan de charge, qualifications et classes (S, 1 à 5), régions, familles, partenaires déclarés (groupement, sous-traitant, fournisseur ; spécialité, qualifications, effet sur le coût en % de l'estimation ou en DH). Chaque ligne garde auteur et date ; un journal (`entreprise_journal`) garde chaque changement.

**Étape ⑥ « Décision »** (`copilote/decision.py` ↔ `statique/decision.js`, mêmes résultats vérifiés sur 15 cas communs `tests/cas_decision.json`) :
- éligibilité (règles strictes de ton message), apport d'un groupement déclaré ;
- coût saisi ou par défaut ; marge minimale modifiable (valeur d'origine barrée, bouton Rétablir) ;
- plage de prix de `max(0,82 ; coût × (1 + marge min))` à 1,20 ; prix recommandé = maximum du gain espéré (chance calibrée de C2) ; zone ; point de meilleure chance pour comparaison ; avertissement sous 0,85 ;
- tableau de choix 0,85 / 0,90 / 0,95 / 1,00 / point recommandé : chance, marge en DH et en %, gain espéré ;
- scénarios seul / groupement / sous-traitance (un partenaire ne change que le coût et l'éligibilité) ;
- GO / À ÉTUDIER / NO-GO, seuils dans `reglages.json` (section `decision`), raisons en phrases (`decision_textes.json`) remplies avec les seuls chiffres calculés ; phrase « petit marché / gros marché » ; hypothèse « les concurrents gardent leur comportement habituel » ;
- « J'ai soumissionné » : prix, coût, scénario, décision et instantané enregistrés.
- **Ligne « Règle simple apprise sur 2025 »** (dernière ligne du tableau de choix) : le taux fixe qui aurait rapporté le plus en 2025 aux marchés de la même tranche de nombre prévu de concurrents (moins de 5,5 / 5,5 à 10,5 / plus de 10,5 ; toutes tranches si le nombre est inconnu), pour NOTRE coût et NOTRE marge minimale (jamais sous max(0,82 ; coût × (1 + marge), jamais au-dessus de 1,20). Sa chance, sa marge et son gain espéré sont lus sur la courbe de CET AO, comme les autres lignes. Les données (`copilote/regle_simple_2025.json`, 97 / 97 / 102 marchés de 2025) sont la part des estimations gagnées à chaque taux fixe ; elles donnent le bon taux pour n'importe quel coût et se régénèrent par `python -m copilote.mesures_decision regle-simple`. Elle ne change ni le prix recommandé ni la décision (test). Exemple, REJEU-11 : recommandé 1,034 (gain espéré 2,46 M DH) ; règle simple 1,043 (chance 64,1 %, gain espéré 2,43 M DH).

**Suivi** (`/copilote/suivi`) : chaque AO soumis ou figé, prévu contre réel, rang de notre offre (replacée parmi les vraies offres), bilan (taux de victoire, marge moyenne, gain). Les AO de test (REJEU) sont montrés mais ne comptent pas dans le bilan.

**Interrupteur** `COPILOTE_PRIVE=1` (éteint par défaut) : exige le code pour entreprise, décision, soumissions et suivi, et eux seuls. Testé éteint et allumé. Noté dans CLAUDE.md : « À activer avant d'ouvrir le site à des clients ».

**Prévisions figées** : les lignes existantes ne sont jamais réécrites (test). La décision n'est ajoutée qu'aux nouvelles copies, et seulement si un coût par défaut est saisi ; sans profil, la copie est identique à celle d'avant (test, même ordre de clés).

## 4. Trouvaille sur l'ordre des classes (à connaître)

Vérifié sur la base (estimation médiane par classe) :
- **Équipement et Agriculture** : S (63 M DH) > 1 (26 M) > 2 (14 M) > 3 (6 M) > 4 (2 M) > 5 (1 M). Ton ordre est confirmé.
- **Habitat : ordre inversé** (classe 6 : 46 M DH, classe 1 : 2 M DH).
- La classe S est stockée sans chiffre dans l'historique (« Classe S »).
- Les codes à chiffre (« 4.1.Travaux de séguia », « 2.5 Assises… ») relèvent de l'Agriculture ou de l'Habitat et ne se distinguent pas par le texte : ils sont « non reconnus » et l'AO passe en **À ÉTUDIER** (c'est le cas de REJEU-11). Seuls les codes à lettre (B.1, O.2…) sont comparés.

## 5. Limites

- Le backtest ne voit que gain, chance, marge et nature du marché. Éligibilité, acheteur, groupes, délai et plan de charge n'y sont pas.
- Les seuils sont choisis sur 296 marchés de 2025, sur lesquels la calibration de C2 a aussi été apprise : léger optimisme sur 2025, sans effet sur 2026.
- Régions et familles sont enregistrées et rappelées sur la page, mais ne changent pas le calcul.
- La décision figée avec une nouvelle prévision n'a que les signaux du dossier (pas l'historique de l'acheteur) et ne lit que les qualifications reconnues.
- 390 px : vérifié par mesure (aucun débordement horizontal sur les trois pages), pas à l'œil sur un téléphone.

## 6. Pistes pour le critère KO : D2

1. **Calibration plus fine près de 1,00**, apprise sur 1 000 marchés de 2025 (environ 1 h), comme proposé en C2 ; elle corrige le creux à 1,00 et devrait déplacer le prix recommandé vers 1,02–1,05.
2. **Prix recommandé stabilisé** : lisser davantage le gain espéré (±0,02 au lieu de ±0,005), ou prendre le maximum sur la moyenne des marchés similaires. Choisi sur 2025.
3. **Accepter l'écart** : la règle bat « toujours 0,95 » partout, et perd de 1 à 16 % contre un taux fixe de 1,04–1,05 qui ne tient aucun compte du marché. L'intérêt de la règle est alors surtout l'éligibilité, le NO-GO et les signaux, pas le prix.

**Décision de Hamza (04/11/2026)** : écart accepté pour l'instant, étape D livrée avec le critère du taux fixe KO ; les pistes 1 et 2 sont l'amélioration prévue en **D2**. D'ici là, la ligne « Règle simple » du tableau de choix permet de comparer le prix recommandé au taux fixe appris sur 2025.

## À tester par Hamza

1. Ouvrir `/copilote/entreprise` : saisir marge 5, coût 85, plan de charge 3, ajouter B.1 classe 3 et B.6 classe 2, cocher une région, enregistrer. Vérifier « modifié par … le … » et l'historique en bas. Déclarer un partenaire en groupement avec B.6 classe 1 et un effet de −2 %.
2. Ouvrir `/copilote/analyse/REJEU-11`, descendre à l'étape ⑥ : lire le verdict et ses raisons. Elles disent « qualification non reconnue » (code à chiffre) : c'est voulu.
3. Changer le coût (champ « Coût de cet AO ») : verdict, courbe et tableau se recalculent ; la valeur d'origine est barrée ; « Rétablir » revient au profil.
4. Décocher une société en ③ ou changer un taux en ④ : l'étape ⑥ suit.
5. Passer le scénario à « Groupement déclaré », choisir le partenaire : coût et éligibilité changent, jamais le prix des autres.
6. Ouvrir un AO avec B.1 ou B.6 (par exemple REJEU-12, REJEU-08) : regarder la ligne d'éligibilité (classe insuffisante, absente, couverte par un groupement).
7. Cliquer « J'ai soumissionné », enregistrer, puis ouvrir `/copilote/suivi` : voir le dépôt, le résultat réel, le rang de notre offre, le bilan (REJEU = marqué « test », hors bilan).
8. Vérifier le tableau « Choisir un prix » : 0,85 / 0,90 / 0,95 / 1,00 / point recommandé / **Règle simple apprise sur 2025**, avec chance, marge en DH et %, gain espéré. Changer le coût : la règle simple change de taux (jamais sous coût × (1 + marge minimale)).
9. Interrupteur : lancer le serveur avec `COPILOTE_PRIVE=1` et `COPILOTE_ACCES`, vérifier que ces quatre parties demandent le code et que l'analyse reste ouverte.
