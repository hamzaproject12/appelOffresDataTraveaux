# Corrections des étapes 2 à 4 (après validation de Hamza)

Étapes 1 à 4 et « 3 bis AO rejoués » cochées dans CLAUDE.md.

## Ce qui a changé

| # | décision | réalisation |
|---|---|---|
| 2 | famille = unité affichée et utilisée | `groupes.ranger_familles` : permanents = dans toutes les cliques, occasionnels = dans certaines ; score = meilleure clique. Tables `familles`, `famille_cliques`. Page : une carte par famille, cliques consultables, fiche de clique inchangée. API : `/api/copilote/groupes` (familles), `/api/copilote/famille/{id}`, `/api/copilote/groupe/{gid}` (clique, + `famille_id`) |
| 3 | identifiants stables | id = empreinte (SHA-256, 8 car.) de la liste triée des permanents. Lien dont la composition a changé → famille actuelle qui partage le plus de sociétés, avec message. Mémoire des compositions : table `familles_anciennes` (jamais vidée par un recalcul) |
| 4 | seuil 50 % conservé ; 30–50 % rangés | table `liens_faibles` (2 233 paires dans « courant »), non affichée |
| 5 | bases simples | grille 0,80 → 1,05 (101 taux) ; `meilleur_par_tranche` ; tableau ajouté à `python -m copilote.mesures` |
| 6 | données de test locales | serveur lancé avec jeton d'essai, 20 AO rejoués, calculs `jusqua-…` et `courant` refaits |

## Chiffres

| mesure | valeur |
|---|---|
| familles (courant) | **48**, pour 66 cliques (37 familles d'une clique, 7 de 2, 3 de 3, 1 de 6) |
| familles avec occasionnels | 11 |
| ancienne « famille 5 » (Morocco Energy…) | 3 permanents, 7 occasionnels, 6 cliques, score 78 |
| rejeu | 20 reçus, 20 créés, 0 rejetés ; calculs `jusqua-…` : 4 à 8 s chacun |
| `/api/copilote/similaires/REJEU-02` | 200, 50 marchés, 8,8 s (1ʳᵉ requête, corpus) |
| `/api/copilote/similaires/REJEU-01` | **400 sans `?lot=`** (AO alloti à 2 lots : voulu, la réponse liste les lots) ; **200 avec `?lot=1`**, 50 marchés, 0,2 s |
| tests | 192 / 192 ; `tester_ao_ouverts.py` : « Tous les contrôles passent » |

Meilleur taux fixe par nombre d'offres (grille 0,80–1,05, 2026) :

| offres | n | meilleur taux | victoire |
|---|---:|---:|---:|
| 1-2 | 2 490 | 0,8025 | 93,2 % |
| 3-4 | 1 236 | 0,8000 | 66,8 % |
| 5-7 | 922 | 0,8050 | 39,2 % |
| 8+ | 1 653 | 0,8050 | 12,3 % |

Toutes années : meilleur taux fixe 0,8025 → 58,8 % (2026), 55,0 % (2025).

## Limites

- **Découpage de familles** : 3 des 45 regroupements de Louvain contenaient des cliques sans aucun membre commun (donc ni permanent, ni empreinte). Ils sont découpés (45 → 48 familles) : une clique rejoint le morceau avec lequel elle partage le plus de membres. Un permanent suffit (certaines familles n'en ont qu'un) ; exiger 2 découperait davantage.
- **Bases simples : l'optimum est au bord de la grille** (0,80) pour toutes les tranches. Le taux fixe « le plus bas gagne » est donc une base très forte et ignore la marge (aucun coût). Il faudra décider à l'étape 6 si on étend encore la grille ou si on borne par la rentabilité.
- **Mémoire des liens** : `familles_anciennes` est dans `copilote_calcul.db`. Si cette base est supprimée, les anciens liens ne peuvent plus être redirigés (404). Seules les familles du calcul « courant » y sont archivées.
- Un avis agit toujours au recalcul suivant ; il porte sur les paires, pas sur la famille.
- Les calculs `jusqua-…` des AO rejoués sont refaits avec les familles, mais l'étape 4 ne les lit pas.
- Les AO « REJEU- » et le jeton d'essai sont dans les bases locales (ignorées par git).

## FEU VERT

| critère | valeur | |
|---|---|---|
| Familles : permanents = toutes les cliques, occasionnels = certaines, score = meilleure clique | testé (cas synthétiques + base de poche) ; 48 familles réelles, toutes avec ≥ 1 permanent | **OK** |
| Page : une carte par famille, deux catégories, cliques en détail, 0 erreur console | vérifié dans le navigateur (famille, clique, retour) ; pas de défilement horizontal | **OK** |
| Identifiant = empreinte des permanents, stable à composition égale | test : même empreinte quel que soit l'ordre ou les occasionnels ; 48 id uniques | **OK** |
| Ancien lien → famille la plus proche + message ; sans point commun → 404 | tests API | **OK** |
| 30–50 % rangés dans `liens_faibles`, non affichés ; seuil 50 % inchangé | 2 233 paires ; un avis « non » en retire une (test) ; 66 groupes comme avant | **OK** |
| Grille jusqu'à 0,80 + meilleur taux par tranche | 101 taux ; tableau ci-dessus | **OK** (limite : optimum au bord) |
| Données locales : rejeu, groupes `jusqua-…`, `courant` ; similaires de REJEU-01 répond | voir chiffres (400 voulu sans `?lot=`, 200 avec) | **OK** |
| Tests verts, `tester_ao_ouverts.py` vert | 192 / 192 ; « Tous les contrôles passent » | **OK** |
| `pv_travaux.db` et `serveur.py` intacts | date de la base inchangée, 0 ligne de diff dans `serveur.py` | **OK** |

## À tester par Hamza

1. `/copilote/groupes` : 48 familles. Ouvrir celle de MOROCCO ENERGY… (3 permanents, 7 occasionnels) : les deux catégories te semblent-elles justes ?
2. Ouvrir une clique : fiche et avis comme avant ; « Retour à la famille ».
3. Copier l'adresse d'une famille (`#f=…`) : après `python -m copilote.recalculer groupes`, elle doit rouvrir la même famille.
4. Dire si un seul permanent suffit pour former une famille, ou s'il en faut deux.
