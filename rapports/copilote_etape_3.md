# Copilote — étape 3 : page /copilote/groupes

Branche `copilote`, rien de commité. Tests : `python -m unittest discover -s tests -t .` → 99 OK (73 + 26 nouveaux).
`python tester_ao_ouverts.py` → tous les contrôles passent. `serveur.py` n'est pas modifié (la ligne d'inclusion date de l'étape 1).
Page essayée dans Chrome sur le vrai serveur (données réelles, base d'avis temporaire), à 1 440 px et à 390 px.

## Ce qui est en place

| Fichier | Rôle |
|---|---|
| `copilote/api_groupes.py` | lecture des groupes, fiche, explication du score en phrases, avis (lecture et écriture) |
| `copilote/routes.py` | 6 routes ajoutées, toutes sur le routeur protégé |
| `statique/copilote_groupes.html` | la page : liste par familles, fiche, avis, historique |
| `statique/ao_ouverts.html`, `dashboard.html` | onglet « Groupes », masqué jusqu'à ce que `/api/copilote/moi` réponde 200 |
| `copilote/donnees.py` | `charger_unites(refs=…)` : lit seulement les marchés demandés (pas de logique recopiée) |
| `tests/test_page_groupes.py`, `tests/outils.py` | 26 tests ; colonne `objet` ajoutée à la base de poche |

| Route | Rôle |
|---|---|
| `GET /copilote/groupes` | la page (ETag, revalidation, comme les autres) |
| `GET /api/copilote/groupes?q=&tri=` | recherche par nom (sans accent ni casse), tri `score` / `marches` / `taille` / `recent` |
| `GET /api/copilote/groupe/{id}` | membres et rôles, score détaillé en phrases, paires, marchés communs (réf., acheteur, objet, date, gagnant, taux de chaque membre), avis, historique |
| `POST /api/copilote/avis` | une paire (`cle_a`, `cle_b`) **ou** tout un groupe (`groupe`) ; réponse `oui` / `non` / `nsp` ; commentaire ≤ 1 000 car. |
| `GET /api/copilote/avis` | derniers avis, tous groupes (ajoutée : sert au bloc « Derniers avis » de la liste) |
| `GET /api/copilote/moi` | 200 + nom, ou 401 : sert à afficher l'onglet (ajoutée) |

## Comment ça marche

- **Auteur** : toujours le nom lié au code d'accès, jamais celui de la requête (testé : un champ `auteur` envoyé est ignoré).
- **Écriture** : uniquement dans `copilote_retours.db` ; `pv_travaux.db` est lue en `mode=ro` (empreinte identique avant/après, testé).
- **Badge** : « Contesté » dès qu'une paire du groupe a un dernier avis tranché « non » ; « Confirmé » s'il y a au moins un « oui » et aucun « non ». « Je ne sais pas » ne tranche rien (même règle que `groupes.lire_avis`, donc que le recalcul).
- **Avis pour tout le groupe** : écrit une ligne par paire (même date, même commentaire) ; l'historique les relit comme un seul avis.
- **Familles** : la liste montre la meilleure variante de chaque famille, les autres sont repliées (« 5 autres variantes de cette famille »), comme demandé dans les limites de l'étape 2.
- **Pourquoi le score** : six lignes (présence mutuelle, nombre de marchés, hasard, variété, hors profil, victoires), chacune avec ses points, une phrase, et « Comment c'est calculé ». Tous les nombres viennent du calcul stocké. Groupe 1 : 10,2 + 15 + 30 + 11,2 + 0 + 15 = 81,4 → 81.
- **Rôles** : chef de file probable / membre qui gagne aussi / membre qui ne gagne pas / membre (peu de victoires).
- **Liens** : chaque nom ouvre `/app#/societes?fiche=s:<clé>` dans un nouvel onglet (vérifié : la fiche INJAZ WORKS s'ouvre).

## Limites

- **Numéro de groupe instable** : l'identifiant `#g=12` est celui du calcul courant. Après le recalcul hebdomadaire, un lien enregistré peut montrer un autre groupe. Les avis, eux, sont rangés par paire de sociétés et ne bougent pas.
- **Un avis agit au recalcul suivant** : un « non » exclut la paire, donc un groupe « contesté » peut disparaître de la liste après le recalcul de la semaine ; un « oui » peut en faire apparaître. La page le dit.
- **Onglet masqué** : pour un visiteur sans accès, chaque page déclenche un appel `/api/copilote/moi` qui répond 401 (visible dans l'onglet Réseau, pas à l'écran). Cela évite une route publique, interdite par CLAUDE.md.
- **Marchés détaillés** : les 400 plus récents au plus, 25 affichés d'abord ; le groupe 1 en a 153.
- **Barre du bas du mobile** : dans `dashboard.html` elle passe de 4 colonnes fixes à « autant de colonnes que d'onglets » (4 ou 5). Dans `ao_ouverts.html` elle avait déjà 5 colonnes, inchangée.
- **390 px** : mesuré dans un cadre de 390 px de large (la fenêtre du navigateur de test ne se réduit pas), pas sur un vrai téléphone.
- **Accord score/avis** : calculable dès les premiers avis (`groupes.accord`), pas encore affiché dans l'interface.
- Les rôles et les phrases se fondent sur peu de victoires pour beaucoup de groupes (« chef de file — 66,7 % de 3 ») : ce sont des indications.

## À tester par Hamza

1. Définir `COPILOTE_ACCES` (ex. `Hamza:ton-code`), lancer le site, entrer ton nom puis aller sur `/copilote/entrer` avec le code.
   Vérifier que l'onglet **Groupes** apparaît dans En cours, Marchés, Sociétés, Maîtres d'ouvrage — et n'apparaît pas pour quelqu'un qui n'a que le nom.
2. `/copilote/groupes` : 66 groupes en 45 familles. Chercher une société que tu connais (ex. `injaz`), puis tester les quatre tris.
3. Ouvrir 3 groupes que tu connais. Lire « Pourquoi ce score » : les phrases sont-elles claires, et justes sur ce que tu sais ?
4. Dans le tableau des marchés communs, comparer 3 marchés avec le portail : référence, acheteur, objet, gagnant, taux de chaque membre.
5. Cliquer sur le nom d'une société : sa fiche du tableau de bord s'ouvre-t-elle ?
6. Donner un avis sur une paire (Oui, Non ou Je ne sais pas, avec un commentaire), puis recharger : l'avis, ton nom et l'heure doivent rester. Vérifier le badge Confirmé / Contesté dans la liste.
7. Essayer « Pour tout le groupe » : l'historique doit montrer une seule ligne « Pour tout le groupe ».
8. Se connecter avec un autre code : l'historique montre-t-il bien l'autre auteur ?
9. Ouvrir la page sur ton téléphone : liste, fiche, tableau des marchés et formulaire d'avis doivent tenir sans défilement latéral.
10. Dire si la question posée et le mot « Contesté » te conviennent, ou s'il faut d'autres libellés.
11. Les avis donnés pendant ces essais sont réels : si ce sont des essais, ils sont dans `copilote_retours.db`, table `avis_groupes`.

## FEU VERT

Critères fixés avant la mesure, à partir du cahier de l'étape.

| critère | valeur mesurée | |
|---|---|---|
| Accès : les 6 routes de l'étape refusées sans code (aucun cookie, cookie visiteur seul) ; un avis refusé n'écrit rien | 6 / 6 refusées dans les deux cas (302 ou 401) ; 0 ligne écrite | **OK** |
| Un avis est enregistré puis relu (réponse, commentaire, auteur de la session, date) par un autre utilisateur | relu tel quel ; auteur = « Alice » alors que la requête disait « Mallory » ; dans le navigateur : « Non, Essai, 22/10/2026 16:51 » | **OK** |
| Aucune écriture hors `copilote_retours.db` : `pv_travaux.db` identique avant/après, `serveur.py` non modifié, aucune table d'avis dans `copilote_calcul.db` | empreinte identique ; 0 ligne de diff dans `serveur.py` ; 0 table d'avis | **OK** |
| Onglet « Groupes » : masqué par défaut, révélé seulement par une réponse 200 | `hidden` dans les 2 pages ; visible avec l'accès (navigateur) ; 401 sans accès | **OK** |
| Les noms de sociétés ouvrent la fiche existante du tableau de bord | fiche INJAZ WORKS ouverte depuis `s:injazworks` | **OK** |
| Fiche complète : membres + rôles, 6 critères dont la somme des points est à ±0,5 du score, marchés communs avec taux, paires avec la question neutre | groupe 1 : 5 membres, 6 critères, 81,4 pour un score de 81, 153 marchés tous avec offres, 10 paires | **OK** |
| Lisible à 390 px et 1 440 px : aucun élément plus large que l'écran | 390 px : 0 élément qui déborde, défilement horizontal nul ; 1 440 px : liste + fiche côte à côte | **OK** |
| Rapide : liste ≤ 500 ms, fiche du plus gros groupe ≤ 1 s | liste 29 ms ; fiche de 153 marchés 15 ms ; fiche de 36 paires 4 ms | **OK** |
| Vocabulaire neutre : aucun mot « entente », « collusion », « fraude », « cartel » dans la page ni les phrases | 0 occurrence (test automatique) | **OK** |
| Tests : tous verts, `tester_ao_ouverts.py` vert | 99 / 99 ; « Tous les contrôles passent » | **OK** |

Aucun critère KO, donc aucune correction. Prochaine étape après validation : 4 — similarité des marchés.
