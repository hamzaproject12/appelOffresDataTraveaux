# Copilote — étape A2 : habituées, nombre de concurrents, groupes en étoile, prévisions figées

Tests : `python -m unittest discover -s tests -t .` → **322 OK** (285 + 37 nouveaux, `tests/test_copilote_a2.py`).
`python tester_ao_ouverts.py` → tous les contrôles passent. `serveur.py` n'est pas modifié.
Mesures : `python -m copilote.mesures_probables tout` (≈ 35 min) → `rapports/copilote_etape_A2_mesures.json`.
A = version de l'étape A (figée dans `copilote/probables_A.json`, groupes par union-find).
A2 = cette étape (`copilote/probables.json`). Les deux versions sont apprises sur 2025 et mesurées sur les mêmes marchés.

## REJEU-R (PV récents, jamais vus) : à faire

Le portail est en maintenance, la mesure n'a donc pas pu être faite. Tout est prêt : quand le portail répondra, il suffit de lancer
**`python -m copilote.rejeu_recent`** (voir « Point 6 » plus bas). Le tableau A / A2 sera écrit dans `rapports/copilote_rejeu_recent.md`.

## Mesures (avant → après)

| | 2026, 500 marchés : A | A2 | 20 AO REJEU (27 marchés ou lots) : A | A2 |
|---|---:|---:|---:|---:|
| vrais soumissionnaires dans le top 15 | 33,4 % | **33,6 %** | 41,4 % | 41,4 % |
| vrai gagnant dans le top 30 | 42,8 % | **43,6 %** | 66,7 % | 66,7 % |
| vrai nombre dans la fourchette 80 % | 77,6 % | **80,0 %** | 85,2 % | 70,4 % |
| erreur médiane sur le nombre | 3 | **2** | 4 | 5 |
| largeur moyenne de la fourchette | 13,5 | **10,8** | 13,1 | 10,3 |
| membres de groupe retrouvés | 56,8 % | **60,3 %** | 91,7 % | 80,0 % |
| vrai gagnant dans la liste affichée | 39,0 % | 36,9 % | 63,0 % | 63,0 % |
| groupes par marché | 1,2 | 2,4 | 1,4 | 2,9 |

- **REJEU** : seulement 27 marchés ou lots, souvent de 15 offres et plus. A2 y sous-estime le nombre : le vrai nombre dépasse le haut de la fourchette 8 fois sur 27, contre 3 fois en A.
- **Par taille (2026, A → A2)** :
  - 1 à 3 offres : la couverture passe de 80 à 93 %, la largeur de 11,3 à 8,2 ;
  - 4 à 7 offres : 95 → 91 % ;
  - 8 à 15 offres : 74 → 69 % ;
  - **16 offres et plus : 39 → 30 %**. Les gros marchés restent le point faible.

### Point 1 — probabilités des habituées

Contrôle demandé : sociétés présentes sur au moins 60 % d'au moins 4 marchés passés de même famille chez l'acheteur, sur 24 mois.

| taux de présence passé | n (2026) | prédit A | prédit A2 | réellement venues | écart A2 |
|---|---:|---:|---:|---:|---:|
| 60–70 % | 84 | 26 % | 39 % | 43 % | −3,6 pts ✓ |
| 70–80 % | 67 | 28 % | 41 % | 39 % | +2,6 pts ✓ |
| 80–90 % | 43 | 32 % | 59 % | 47 % | +12,3 pts ✗ |
| 90–100 % | 43 | 33 % | 65 % | 51 % | +13,7 pts ✗ |

- **Fait important** : une habituée revient bien moins souvent que son taux passé.
  - En 2026, une société présente sur 85 % des marchés passés ne vient que 47 % du temps ; sur 97 %, elle vient 51 % du temps.
  - En 2025 c'est pareil : 48 % et 60 %.
  - Une probabilité de 80 à 89 % pour SAGHALI serait donc fausse : la bonne cible tourne autour de 50 %.
- **Sur 2025 (marchés tenus à part de l'ajustement)**, les 4 tranches sont à ±3 points. Sur 2026, les deux tranches hautes dépassent de 12 et 14 points, sur 43 sociétés chacune (erreur type ≈ 7,6 points).
- **Ce qui a été fait** :
  - 3 variables : présence chez l'acheteur, même famille, 24 mois, lissée (k / (n + 2)) ; nombre de ces présences ; présence chez l'acheteur toutes familles, 24 mois ;
  - un segment « habituée » : au moins 2 présences chez l'acheteur dans la même famille en 24 mois ;
  - une formule propre à ce segment, apprise sur 6 015 couples, dont 663 venues ;
  - une table de calibration par segment, avec au moins 40 couples par bloc.
- **AO reconstitués**, voir la limite plus bas (A → A2) :

  | société | A | A2 | présence passée chez l'acheteur, même famille |
  |---|---:|---:|---|
  | SAGHALI | 18 % | 49 % | 8 sur 9 |
  | LAAMARTI PLAN | 27 % | 52 % | 8 sur 11 |
  | BOUKABOUS | 31 % | 74 % | 4 sur 5 |
  | BOUHDOMANE | — | 20 % | 3 sur 5 |

### Point 2 — nombre de concurrents

- **Principe** : on combine l'espérance du modèle M et le nombre médian H des marchés de l'acheteur dans la même famille, sur 3 ans : cible = exp((1 − w)·log M + w·log H), avec w = n / (n + 8).
  - L'effet commun au marché est décalé pour atteindre cette cible ; sa dispersion passe de 0,8 à (1 − w)·0,8 + w·0,5.
  - Le décalage voyage dans le dossier : quand on décoche une société dans la page, le recalcul le garde.
  - Les copies Python et JS sont comparées sur 12 cas communs, dont 2 nouveaux.
- **Choix de k et σ_h** sur 2025 : couverture à ±1 point de 80 %, puis l'erreur la plus faible.
  - Un premier réglage (k = 4, σ_h = 0,3, couverture 76 %) donnait 77 % sur 2026 mais seulement 63 % sur les REJEU : je l'ai abandonné.
- **Mesures** : voir le tableau ci-dessus.
  - Erreur médiane 3 → 2 ; couverture 77,6 → 80,0 % ; largeur 13,5 → 10,8.
  - REJEU : couverture 85 → 70 %, juste à la limite.
- **AO reconstitués** (valeur la plus probable, A → A2) :

  | AO | A | A2 | médiane des marchés passés |
  |---|---:|---:|---:|
  | Séfrou | 4 | 11 (5 à 27) | 20 |
  | Jerada | 11 | 15 (7 à 31) | 19 |
  | Guercif | 7 | 9 (4 à 22) | 13 |

  L'écart se réduit, mais le modèle reste en dessous de l'historique quand celui-ci a peu de marchés (Guercif : n = 5, poids 38 %).

### Point 3 — groupes en étoile

- **Règle** :
  - on classe les sociétés par score = probabilité de venir × taux de victoire lissé sur les 300 marchés les plus similaires ;
  - dans cet ordre, une société qui a des alliées devient chef de file, avec ses seules alliées directes ;
  - une alliée d'un chef déjà retenu reste simple accompagnatrice, **sauf si elle a au moins 2 alliées directes dans la liste** (sans compter les chefs) : elle garde alors son groupe, et les deux groupes affichent « travaille aussi avec X » ;
  - une accompagnatrice de deux chefs va sous le plus probable ; l'autre groupe affiche « accompagne aussi X ».
- **Seuil de recherche des alliées** : 15 % → 7 % pour A2 seulement.
  - Mesuré sur 200 marchés de 2026 : membres retrouvés 41 % à 15 %, 50 % à 10 %, 57 % à 7 %, contre 52 % pour A.
  - Groupes plus nombreux (2,4 par marché) et plus petits (2,9 sociétés).
- **Contrôles, sur les AO reconstitués** :
  - **1042468** : TIB TRAV = AGRO ELEC, ORN TRAV, SONATRAP, GUERCIF GREEN, UNTRAVOB, AGROSSAR, TAJDIR. **Entier ✓.** BOURGEON VERT forme son propre groupe (« travaille aussi avec TIB TRAV »).
  - **1044485** : deux groupes distincts ✓.
    - BOUKABOUS = GUETTAF, SPECIALISEE-VOIRIE, ANASSI ; « travaille aussi avec » ROAD-SUC et BOUHDOMANE.
    - AMMARTI PLANTE = BAALI MOHAMED, plus GUETTAF « accompagne aussi » ✓.
    - **E.G.R n'y est pas** : sur tout l'historique, E.G.R ne fait pas partie des 6 alliées directes de BOUKABOUS (ANASSI, FADRIK SUD, SPECIALISEE-VOIRIE, HORIZON CLAIRE, GUETTAF, ROAD-SUC). En A, E.G.R n'arrivait dans ce groupe que par chaînage. Le groupe en étoile ne peut pas l'y mettre sans réintroduire ce chaînage : à toi de dire si c'est voulu.
- **Tests** : sans chaînage (A2 contre union-find) ; chef de file = probabilité × taux de victoire ; **un chef probable n'est pas absorbé** (2 alliées dans la liste → « travaille aussi avec ») ; absorption à une seule alliée ; « accompagne aussi » ; association.

### Point 4 — prévisions figées

- **Enregistrement** : table `previsions` dans `copilote_retours.db`. À la première ouverture de l'analyse d'un vrai AO, puis au plus une fois par jour (UTC), on enregistre :
  - probabilités, segment et place dans la liste ;
  - fourchette et loi du nombre de concurrents ;
  - groupes, avec les « accompagne aussi » ;
  - la version des réglages : `A2`, empreinte `929f110b0aa9`.
  - Les REJEU ne sont jamais figés.
- **Comparaison** : quand le PV arrive (marché `c` + référence de l'AO dans `pv_travaux.db`, lot par lot), le bloc **« Prévu / Réel »** s'affiche sous ③. La copie comparée est la dernière figée au plus tard le jour de l'ouverture.
  - On y voit : sociétés venues ✓/✗ avec leur rang, prévues mais absentes, vrai nombre et fourchette, gagnant, son rang et son groupe, membres venus par groupe.
  - Tant que le PV n'est pas là, une ligne indique : « Prévision figée le … ».
- **API** : `GET /api/copilote/prevision/{ref}?lot=` (protégée comme le reste du copilote).
- **Commandes** :
  - `python -m copilote.suivi` : liste des AO prévus et leur score ;
  - `--detail REF[:LOT]` : le détail d'un AO ;
  - `figer REF[:LOT] …` : figer à la main sur la machine qui sert le site.
- **AO expiré** : l'analyse marche toujours (testé avec un AO dont la date limite est passée). Seule la liste /ao-ouverts cache les expirés par défaut.

### Point 5 — mise à jour automatique et page des lots

- **`maj_quotidienne.py`** renvoie le code **3** quand la sonde échoue avant la collecte (avant : 1, comme les autres échecs).
- **`planificateur.py`** : sur un code 3, nouvel essai toutes les heures, **6 essais au plus par jour** (variable `MAJ_ESSAIS`). La journée n'est notée faite qu'après. Tout autre code arrête les essais du jour.
- **À mettre sur Railway** : **`MAJ_HEURE=11:00`**. La sonde a réussi à 11:26 et 22:35 UTC ; avec les nouveaux essais, la dernière tentative tombe à 16:00 UTC. Je n'ai rien changé en production.
- **Page d'analyse** :
  - sans lot dans l'adresse, la page lit `GET /api/copilote/analyse/{ref}/lots` (réponse 200) et propose les lots ;
  - avec un lot, elle n'appelle l'analyse **qu'avec ce lot**, et la liste des lots vient du dossier lui-même ;
  - le bouton « Analyser » d'un AO alloti ouvre directement le lot 1.
  - Vérifié en local sur REJEU-01 : seuls `/lots` et `?lot=1` sont appelés, tous deux en 200.

### Point 6 — REJEU-R, prêt

**`python -m copilote.rejeu_recent`** fait tout, dans l'ordre :

- **a)** `maj_quotidienne.py --base travaux --jours J --relance 0`, avec J = jours depuis le 01/10/2026 + 30. Donne le nombre de nouveaux marchés avec PV.
  - Portail injoignable (code 3) : arrêt, rien n'est changé.
- **b)** Vérifie que A et A2 ont été appris avant le 01/10 (`appris_jusqu_au` = 2025-12-31). Ne réapprend rien.
- **c)** Choisit 30 marchés ouverts depuis le 01/10, dont au moins 3 allotis, un seul par acheteur, variés en famille, classe et montant, et les nomme **REJEU-R-01 à 30**.
  - Coupure la veille de l'ouverture, vérité dans `rejeu_verite`.
  - Les autres REJEU ne sont pas touchés.
  - Les annonces sont envoyées au site **local** sous le client « Test » (`JETON_SERVICE` requis).
  - Sur /ao-ouverts, une section repliable **« AO de test »** les montre malgré le client imposé « Hicham », pour les visiteurs qui ont l'accès copilote.
- **d)** Mesure A puis A2 sur ces marchés et écrit `rapports/copilote_rejeu_recent.md` : top 15, gagnant dans le top 30, fourchette, erreur médiane, membres de groupe retrouvés.
  - La mesure lit les annonces dans une base en mémoire : elle ne dépend pas du serveur.

Options : `--sans-maj`, `--renouveler`, `--sans-envoi` ; sous-commandes `mesurer`, `envoyer`, `etat`. Testé sur une base simulée.

## FEU VERT

| critère | 2026 (A2) | REJEU (A2) | |
|---|---|---|---|
| ≥ 60 % des vrais soumissionnaires dans le top 15 | 33,6 % | 41,4 % | **KO** (inchangé) |
| vrai gagnant dans la liste ≥ 70 % | 36,9 % | 63,0 % | **KO** (inchangé) |
| nombre dans la fourchette 80 % entre 70 et 90 % | 80,0 % | 70,4 % | **OK** (REJEU à la limite) |
| habituées à ±10 points par tranche | 2 tranches sur 4 (A : 0 sur 4) | trop peu de cas | **KO partiel** |
| groupes 1044485 et 1042468 | séparés ; TIB TRAV entier ; E.G.R absent | — | **OK sauf E.G.R** (AO reconstitués) |
| page sans erreur à 390 et 1 440 px | aucune erreur console, aucun débordement | — | **OK** |
| test REJEU-R | — | — | **à faire** (portail) |

## Limites

- **AO reconstitués** : en local, `ao_ouverts.db` ne contient que les REJEU.
  - 1042468 est exact (objet, acheteur, estimation, qualification 10.1 classe 4, d'après la fiche de consultation locale).
  - 1044485 et 1041802 (lot 1) sont approchés : acheteur et famille justes, objet et estimation inventés.
  - Les compositions exactes sont à vérifier en production, après le déploiement.
- **Gros marchés** (16 offres et plus) : la couverture est de 30 %, encore trop basse.
- **Tranches hautes des habituées** : 43 sociétés chacune. Pour corriger, il faudrait apprendre la formule des habituées sur 2024 + 2025 (2 fois plus de couples).
- **`probables.json` a changé** : l'empreinte des prévisions figées changera à chaque réajustement. C'est voulu : la version figée dit avec quels réglages la prévision a été faite.

## Comment vérifier les 4 copies en production

Après le déploiement, ouvre les 4 analyses (en étant entré avec un nom de visiteur) :

- `/copilote/analyse/1044485`
- `/copilote/analyse/1041802?lot=1`
- `/copilote/analyse/1041802?lot=2`
- `/copilote/analyse/1042468`

Comme ces AO ont expiré le 02/11, ils n'apparaissent dans /ao-ouverts qu'après avoir coché « Voir aussi les appels expirés ».

Puis, pour chacun :

1. Sous ③, la ligne « Prévision figée le JJ/MM/2026 (version A2, réglages 929f110b0aa9). Le PV n'est pas encore dans l'historique… (1 copie(s) enregistrée(s)) » doit apparaître. Si elle manque à la première ouverture, recharge la page : la copie est écrite pendant le premier calcul.
2. Ou ouvre directement :
   - `/api/copilote/prevision/1044485`
   - `/api/copilote/prevision/1041802?lot=1`
   - `/api/copilote/prevision/1041802?lot=2`
   - `/api/copilote/prevision/1042468`

   Chacune doit répondre en JSON avec `"copies": [{"jour": "2026-11-…", "version": "A2", …}]`. Une réponse 404 veut dire : pas encore figée.
3. Depuis un terminal du conteneur (`railway ssh`, si tu l'utilises) : `python -m copilote.suivi` liste les 4 lignes.

## À tester par Hamza

1. **Railway** : mettre `MAJ_HEURE=11:00` et laisser `MAJ_ESSAIS` vide (6 par défaut). Le lendemain, les journaux doivent montrer « mise à jour terminée … code 0 ». Si le portail ne répond pas : « portail injoignable (essai 1 sur 6) : nouvel essai dans 60 min ».
2. **Production** : ouvrir les 4 analyses ci-dessus et vérifier les copies (section précédente). Sur 1044485 :
   - deux groupes séparés, BOUKABOUS et AMMARTI PLANTE, avec GUETTAF en « accompagne aussi » dans le second ;
   - noter où est E.G.R et me dire s'il doit être avec BOUKABOUS.

   Sur 1042468 : le groupe de TIB TRAV avec ses 7 membres.
3. **Probabilités** : sur 1041802 lot 1, SAGHALI doit être autour de 50 %. Ouvrir « Pourquoi x % » : la phrase « Chez l'acheteur sur 24 mois : 8 de ses 9 marchés de la même famille… », le segment « habituée », et la constante « (formule des habituées) ».
4. **Nombre de concurrents** : sous la fourchette, la ligne « Historique de l'acheteur : n marché(s)… médiane … ». Décocher une société : la fourchette se recalcule, et le décalage reste.
5. **Lots** : sur `/ao-ouverts`, le bouton « Analyser » d'un AO alloti ouvre `?lot=1`. Ouvrir ensuite `/copilote/analyse/1041802` sans lot : le choix des lots s'affiche, sans message d'erreur. Le journal Railway ne doit plus montrer de 400 sur `/api/copilote/analyse/…`.
6. **REJEU-R**, quand le portail reviendra : sur le PC, `python -m copilote.rejeu_recent`, avec le site local lancé et `JETON_SERVICE` défini. Ensuite :
   - ouvrir `/ao-ouverts` : en bas, « AO de test (… appels) » liste les REJEU et les REJEU-R ;
   - ouvrir **REJEU-R-01**, un REJEU-R alloti (lot 1) et deux ou trois autres : bloc « Ce qui s'est réellement passé », gagnant, fourchette ;
   - lire `rapports/copilote_rejeu_recent.md`.
7. **Décision** :
   - seuils du feu vert (top 15, gagnant dans la liste) ;
   - E.G.R ;
   - faut-il apprendre la formule des habituées sur 2024 + 2025.

## Prochaine étape

REJEU-R dès que le portail répond, puis l'étape 5 (taux de chaque société).
