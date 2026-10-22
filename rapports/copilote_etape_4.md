# Copilote — étape 4 : moteur de marchés similaires

Branche `copilote`, rien de commité. Tests : `python -m unittest discover -s tests -t .` → **181 OK** (123 + 58 nouveaux).
`python tester_ao_ouverts.py` → tous les contrôles passent. `serveur.py` n'est pas modifié (`git diff` vide).
Backtest complet : `python -m copilote.mesures_similarite` (10 min) → `rapports/copilote_etape_4_backtest_2026.json`.

**À savoir d'abord** : `ao_ouverts.db` locale ne contient aucun AO (0 ligne). Les « 3 AO ouverts réels » demandés sont donc
remplacés par 3 marchés de 2026 (+ 1 lot) rejoués comme des AO, par le même code que l'API (voir §5). À refaire sur ton serveur.

## Ce qui est en place

| Fichier | Rôle |
|---|---|
| `copilote/nature_mots.json`, `nature.py` | familles et nature par mots-clés (sans accents) — **c'est ce fichier que tu complètes** |
| `copilote/qualifs.py` | code `B.1` extrait de « B.1- Travaux… », classe, proximité de classe |
| `copilote/corpus.py` | 18 713 unités (marché ou lot) en tableaux numpy, rangées par date |
| `copilote/index_texte.py` | TF-IDF caractères, rangé dans `copilote_calcul.db` (table `sim_index`) |
| `copilote/similarite.py`, `reglages.json`, `reglages.py` | filtre + score sur 100 + détail |
| `copilote/api_similaires.py`, `routes.py` | l'API (4 routes ajoutées sur le routeur protégé) |
| `copilote/acheteurs.py` | types d'acheteurs, rapprochement rapidfuzz, table `correspondances_acheteurs` |
| `copilote/mesures_nature.py`, `mesures_similarite.py` | les chiffres de ce rapport |
| `copilote/recalculer.py` | `python -m copilote.recalculer similarite [--jusqua AAAA-MM-JJ]` |
| `tests/test_similarite.py` | 58 tests ; `tests/test_acces.py` : le contrôle des routes teste maintenant chaque méthode (une route POST seule répondait 405 à un GET) |

Routes (toutes derrière `COPILOTE_ACCES`) : `GET /api/copilote/similaires/{ref_annonce}?lot=&poids=`,
`GET /api/copilote/correspondances`, `POST /api/copilote/correspondance`, `GET /api/copilote/acheteurs?q=`.
Aucune modification de Dockerfile ni de requirements (scikit-learn, rapidfuzz, numpy déjà ajoutés à l'étape 1 ; les JSON sont dans `copilote/`, déjà copié).

## 1. Nature des travaux

14 familles (route/piste, ouvrage d'art, assainissement, eau potable, éclairage/électricité, bâtiment neuf, réhabilitation de bâtiment,
aménagement urbain, VRD, sport, hydro-agricole, forêt/environnement, clôture, fourniture) + nature neuf / réhabilitation / entretien.
Le bâtiment se range « neuf » ou « réhabilitation » selon la nature. Règle : première famille de la liste `ordre` dont un mot figure
dans l'objet ; nature : mot cité le plus tôt.

| 21 082 unités | 1ʳᵉ version | après ajout de mots-clés |
|---|---:|---:|
| famille reconnue | 91,7 % | **95,4 %** |
| nature reconnue | 83,0 % | **91,8 %** |
| ni l'une ni l'autre | 266 | 83 |

Précision : 30 objets tirés au hasard et relus à l'œil → 27 plausibles, 3 douteux (≈ 90 %, **échantillon trop petit pour être une mesure**).
Familles les plus grosses : bâtiment neuf 16,4 %, route/piste 13,7 %, forêt 11,9 % (les marchés alloti forestiers comptent un lot chacun), eau potable 11,0 %.

**Les 100 objets non classés les plus fréquents** : `rapports/copilote_non_classes.csv` (séparateur `;`, objet réduit au « quoi » avant le lieu,
nombre, exemple complet). Les plus fréquents : « mise à niveau de la… » (14), « élargissement et renforcement de la… » (8), « travaux de lutte contre l… » (6),
« renouvellement des branchements » (5), « recherche et réparation des fuites dans les réseaux » (4), « démolition des bâtisses menaçant ruine » (4),
« club de surf » (4), « citerne enterrée » (4), « conservation des sols » (4). Mots récurrents hors règles : **port, parc, unité, station, traitement, cercle, opération**.
Après modification de `nature_mots.json` : `python -m copilote.mesures_nature`.

## 2. Index de similarité de l'objet

TF-IDF `char_wb` 3-4 grammes, `min_df=2`, tf sous-linéaire, norme L2 — **identique à `TfidfVectorizer`** (test à 1e-6) mais écrit en deux temps
pour ranger vocabulaire (JSON), idf (npy) et matrice (npz) **sans pickle**.

| | |
|---|---:|
| documents / traits / valeurs | 18 713 / 23 618 / 3,66 M |
| taille dans `copilote_calcul.db` | 16,6 Mo par calcul (deux rangés : « courant » et « jusqua-2025-12-31 » du backtest ; supprimables) |
| calcul | 4,7 s |
| rechargement | **0,29 s** |

Une signature (identifiants + textes) accompagne l'index : si `pv_travaux.db` a changé, l'index ne correspond plus, il est refait et rangé, jamais servi tel quel.
Au démarrage du serveur, un fil d'arrière-plan recharge l'index (jamais fatal). Les textes retirent les nombres seuls (pk, tranches) ; un lot est
« intitulé du lot + objet du marché ».
Exclus du corpus : 179 unités sans estimation, 2 190 sans prix de référence calculable (elles ne peuvent pas informer le prix de référence).

## 3. Score sur 100 (`reglages.json`)

Filtre d'abord : antérieur au jour de l'AO (jour exclu) ; montant entre 0,3× et 3× ; qualification compatible = au moins un **code** en commun
quand les deux côtés en ont (un marché sans qualification reste comparable).
Chaque critère vaut 0 à 1, note = 100 × Σ poids·critère / Σ poids.

| critère | poids | comment (choix de ma part, modifiables dans `reglages.json`) |
|---|---:|---|
| objet et nature | 40 | 0,6 cosinus TF-IDF + 0,25 même famille + 0,15 même nature ; famille/nature inconnues côté AO ⇒ seul le texte compte |
| acheteur | 30 | 1 si même acheteur (ou rapproché), 0,3 si même type (commune, direction…), sinon 0 |
| qualification/classe | 10 | part des codes de l'AO présents, pondérée par la classe (même 1, ±1 : 0,6, ±2 : 0,3) ; marché sans qualification : 0,4 |
| montant | 10 | 1 − \|ln ratio\| / ln 3,33 (0 aux bornes) |
| région | 5 | même région (déduite du nom de l'acheteur) |
| récence | 5 | demi-vie 365 jours |

**Un critère sans information côté AO sort du dénominateur** (pas de qualification, pas d'estimation, région inconnue) : sinon il vaut 0 pour
tous et plafonne la note (90 au lieu de 100 pour un lot sans qualification, vu sur un exemple). Le classement n'en change pas. L'API renvoie `poids_effectif` par critère.
Détail par marché : poids, valeur 0-1, points, phrase d'explication, chiffres (texte 50 %, ×0,78, « il y a 4 mois »…).

## 4. API et chiffres de service

`GET /api/copilote/similaires/{ref_annonce}?lot=&poids=acheteur:50,montant:5` renvoie : le profil de l'AO (famille, nature, qualifications par code, région),
le rapprochement d'acheteur, les poids, le nombre de marchés lus/comparables, la **médiane du prix de référence (Q1-Q3) des 20 premiers**, et les 50 meilleurs
(score, détail, gagnant — attributaire réel, sinon celui de la règle —, prix de référence en taux, offres retenues, concurrents).
AO alloti sans `lot` → 400 avec la liste des lots. `poids` illisible → 422. AO « REJEU- » : ne voit que ce qui précède son ouverture (409 sans coupure).
Latence : 7,5 s à la première requête après un déploiement (corpus en mémoire), puis **≈ 0,04 s**.

## 5. Backtest 2026 (6 401 marchés, strict)

Pour un marché du jour d : candidats datés **avant** d ; idf et vocabulaire ajustés sur les 12 312 marchés **d'avant 2026**. Cible : prix de référence du marché (taux).
Erreur absolue moyenne (MAE) en points de taux :

| prédiction | MAE | médiane des erreurs | à ±2 points |
|---|---:|---:|---:|
| médiane globale (tous les marchés antérieurs) | 2,80 | 1,82 | 55,0 % |
| 20 derniers marchés de la même famille | 2,83 | 2,15 | 47,3 % |
| 20 derniers marchés (récence seule) | 2,89 | 2,19 | 46,2 % |
| 20 derniers marchés du même acheteur (≥ 3) | 2,66 | 1,85 | 53,0 % |
| **médiane des 20 plus similaires (poids par défaut)** | **2,43** | **1,65** | **57,1 %** |

Contre la médiane globale : **−0,37 point** (IC à 95 % −0,42 ; −0,32), gagne sur 58 % des marchés. Contre la base « famille » : −0,40 [−0,45 ; −0,35].

Jeux de poids (même protocole) : défaut 2,43 · objet dominant (60/15/10/10/2/3) 2,43 · acheteur dominant (25/50/10/5/5/5) 2,44 · montant+récence (35/20/10/20/5/10) 2,43 ·
**objet seul 2,48**. Les jeux sont indiscernables ; l'objet seul est un peu moins bon. Je garde les poids par défaut. Voisins : 10 → 2,46, 20 → 2,43, 50 → 2,43.
Filtre de qualification : code 2,43, domaine 2,44, aucun 2,45 (le filtre change peu).

| par nombre d'offres retenues | n | médiane globale | similaires |
|---|---:|---:|---:|
| 1-2 | 2 584 | 4,10 | 3,35 |
| 3-4 | 1 239 | 2,57 | 2,48 |
| **5-7** | 924 | 1,92 | **1,97 (pas de gain)** |
| 8+ | 1 654 | 1,44 | 1,23 |
| acheteur connu (≥ 3 marchés) | 5 587 | 2,89 | 2,49 |
| acheteur peu ou pas connu | 814 | 2,21 | 2,04 |

Fuites : 0 candidat non antérieur sur 6 401 × 5 jeux. 2 marchés ont moins de 5 comparables (repli sur la médiane globale).

### Trois AO en exemple (+ un lot)

Marchés de 2026 rejoués par `api_similaires.analyser` avec coupure la veille de l'ouverture (index recalculé sur le passé seul). Erreur en points de taux.

| AO | acheteur reconnu | famille · qualifs | médiane 20 similaires | médiane globale | réel | erreur sim. / globale |
|---|---|---|---:|---:|---:|---:|
| piste n°05 — Commune Oulad Hassoune, 4,2 M (ouv. 14/09) | oui | route · B.1, B.3, B.5 cl. 4 | 1,0039 | 1,0184 | 1,0120 | 0,81 / 0,64 |
| AEP rurale — ARE Marrakech-Safi, 21,8 M (ouv. 10/09) | oui | eau potable · A.6 cl. 4, C.1 cl. 3 | 1,0140 | 1,0184 | 1,0159 | 0,19 / 0,25 |
| lycée collégial — DP Éducation Ifrane, 4,8 M (ouv. 10/09) | oui | bâtiment neuf · A.2 cl. 4 | 1,0255 | 1,0184 | 1,0524 | 2,69 / 3,40 |
| lot 3 : piste — DP Agriculture Safi, 8,9 M (ouv. 22/09) | oui | route · aucune qualification | 1,0207 | 1,0184 | 1,0072 | 1,35 / 1,13 |

Détail du 1ᵉʳ résultat de l'AEP (77,3/100) : objet 20,3/40 (texte 18 %, même famille, même nature) · acheteur 30/30 · qualification 10/10 (A.6 cl. 4/4, C.1 cl. 3/3) · montant 7,4/10 (×0,73) ·
région 5/5 · récence 4,6/5 (il y a 1 mois). Deux exemples sur quatre sont meilleurs que la médiane globale, deux sont moins bons : c'est l'ordre de grandeur du gain (0,37 point en moyenne).

## 6. Rapprochement des acheteurs

Quand `cle_acheteur()` ne trouve pas l'acheteur de l'AO, `acheteurs.rapprocher` compare les **noyaux** des noms (mots génériques ôtés : directeur, direction, provincial(e), régional(e),
délégué, gouverneur, commune, rurale, urbaine, ministère, province, préfecture, région, articles…, ordre des mots conservé) avec `rapidfuzz.fuzz.ratio`, sur les 1 946 acheteurs de l'historique.
Garde-fous : même type d'acheteur ; commune **rurale** ≠ **urbaine**, direction **provinciale** ≠ **régionale** ; un mot sans équivalent de l'autre côté (TAZA contre TANTAN) plafonne le score à 85.
Les propositions (≥ 80, 5 au plus) sont rangées dans `copilote_retours.db`, table `correspondances_acheteurs` (statut `proposee` / `validee` / `refusee`, auteur, date, commentaire) :
une proposition ne remplace **jamais** une décision humaine (testé). Usage dans le score : « validée » toujours, « refusée » jamais, « proposée » seulement si ≥ 95 et sans concurrente à moins de 3 points.
Modifiable par `POST /api/copilote/correspondance` (auteur = session) ou directement en SQL.

Auto-évaluation (`python -m copilote.acheteurs evaluer`, 300 acheteurs réécrits) :

| réécriture | n | bon acheteur en 1ʳᵉ proposition | utilisé automatiquement | …et juste |
|---|---:|---:|---:|---:|
| « directeur » ↔ « direction » | 22 | 22 | 22 | 22 |
| sans articles ni mots-outils | 259 | 256 | 253 | 253 |
| deux lettres fausses | 299 | 132 (44 %) | 24 | 24 |

Les fautes de frappe sur noms courts passent sous le seuil de 80 : choix de précision (une proposition fausse est rangée dans une table que des humains relisent).

**AO ouverts sans acheteur reconnu — `ao_ouverts.db` locale : 0 AO, donc 0 mesurable.** Mesure de remplacement sur 2026, en ne connaissant que les marchés d'avant 2026
(`acheteurs.mesurer_sur_historique`) : sur 6 250 marchés, 5 764 (92,2 %) ont un acheteur connu ; 486 (7,8 %, 262 acheteurs) n'en ont pas. Le rapprochement en rattache 12 automatiquement,
133 ont une proposition à valider, **341 (5,5 %) n'ont aucune proposition** (communes nouvelles dans l'historique). Reste sans acheteur utilisable : **7,6 %**.
Correction faite en cours de route : à 90, 3 rapprochements automatiques sur 6 étaient faux (« Commune SAADLA » → « Commune SAADA », « M'SAADA » → « SAADA », « TAGANTE » → « TARGANTE ») ; seuil relevé à 95, les 3 restants (Safi, Tarfaya, El Jadida → « Gouverneur de la province de… ») sont justes.

## FEU VERT

Seuils = ceux que je me suis fixés (tu n'en avais pas donné) ; à toi de les durcir.

| # | critère de réussite | valeur mesurée | |
|---|---|---|---|
| 1 | familles reconnues ≥ 90 % | 95,4 % (91,7 % avant ajout de mots-clés) | **OK** |
| 2 | nature reconnue ≥ 90 % | 91,8 % (**83,0 % avant : KO**, corrigé par l'ajout de mots-clés dans `nature_mots.json`, remesuré) | **OK** |
| 3 | les 100 objets non classés les plus fréquents livrés | 100 lignes, `rapports/copilote_non_classes.csv` | **OK** |
| 4 | index = TF-IDF scikit-learn | écart ≤ 1e-6 (test) | **OK** |
| 5 | index rangé dans `copilote_calcul.db`, rechargé sans recalcul | 0,29 s contre 4,7 s ; contenu identique (test) ; refait si le corpus change (test) | **OK** |
| 6 | poids par défaut dans `reglages.json` | 40/30/10/10/5/5, somme 100 (test) | **OK** |
| 7 | filtre respecté (montant 0,3×–3×, qualification par code, antériorité) | 0 violation sur 14 844 résultats (300 marchés, revérifié sur les données brutes) | **OK** |
| 8 | détail par critère = note, note ∈ [0 ; 100] | somme des points = note à ±0,06 (test) | **OK** |
| 9 | aucune fuite temporelle | 0 candidat non antérieur ; idf ajusté sur 12 312 marchés d'avant 2026 | **OK** |
| 10 | API : 50 meilleurs avec score, détail, gagnant, prix de réf. en taux, nb de concurrents ; `?lot=` ; `?poids=` ; accès protégé | 58 tests ; 0,04 s après chauffe ; 401 sans code d'accès | **OK** |
| 11 | médiane des 20 plus similaires bat la médiane globale (2026) | MAE 2,43 contre 2,80 ; −0,37 pt [−0,42 ; −0,32] ; 58 % de victoires ; MAE plus basse aussi que la base « famille » (2,83, écart testé) et « même acheteur » (2,66, écart non testé) | **OK** |
| 12 | 2-3 jeux de poids comparés | 5 jeux : 2,43 à 2,48 ; défaut conservé | **OK** |
| 13 | gain aussi sur les marchés à 5-7 offres | 1,97 contre 1,92 (aucun gain) | **KO** — voir ci-dessous |
| 14 | 3 AO ouverts **réels** en exemple | 0 AO dans `ao_ouverts.db` ; 3 marchés 2026 + 1 lot rejoués par le même code | **KO** — voir ci-dessous |
| 15 | table `correspondances_acheteurs`, modifiable à la main | table + API ; décisions jamais écrasées (tests) | **OK** |
| 16 | rapprochements automatiques justes | 253/253, 22/22, 24/24 sur réécritures ; 3/3 relus sur 2026 (**3/6 à 90 : KO**, seuil passé à 95, remesuré) | **OK** |
| 17 | AO ouverts sans acheteur reconnu, mesurés | `ao_ouverts.db` locale vide : non mesurable | **KO** — voir ci-dessous |
| 18 | proxy 2026 : acheteurs sans correspondance utilisable ≤ 10 % | 7,6 % des marchés (5,5 % sans aucune proposition) | **OK** |
| 19 | tests, `tester_ao_ouverts.py`, `pv_travaux.db` jamais écrite, `serveur.py` intact | 181 OK ; vert ; empreinte identique avant/après (test) ; `git diff` vide | **OK** |
| 20 | vocabulaire neutre (ni « entente », ni « collusion »…) | aucun dans les réponses de l'API (test) | **OK** |

**Les trois KO**
- **13 (5-7 offres)** : sur cette tranche le prix de référence dépend surtout des offres du jour, la similarité n'apporte rien (erreur +0,05 point). Correction possible, **non appliquée** : ne pas afficher la médiane des similaires seule pour 5-7 offres, mais l'écart-type et la médiane globale à côté. À décider à l'étape 6.
- **14 (AO réels)** : pas de correction possible ici. Sur ton serveur, ouvre `/api/copilote/similaires/<ref_annonce>` pour un AO de `/ao-ouverts` ; sinon `python -m copilote.rejouer_ao` fabrique 20 AO « REJEU- » à partir de 2026.
- **17 (mesure sur les AO réels)** : `python -m copilote.acheteurs mesurer` sur le serveur (lecture seule), puis `proposer` pour ranger les propositions.

## Limites

- Les gains sont modestes (0,37 point de taux) : la similarité resserre la fourchette, elle ne prédit pas le prix de référence. Surtout utile à 1-2 offres et à 8+.
- La région vient du nom de l'acheteur (77 % des marchés) ; « Commune OULAD HASSOUNE » n'en donne pas.
- Cosinus de texte bas (20 à 50 %) : les objets sont des formules longues et proches ; la famille et la nature font l'essentiel du travail de tri.
- Qualifications : seul le niveau marché existe en base, pas lot par lot ; 1 207 des 1 235 marchés allotis n'en ont aucune.
- Un AO rejoué calcule son propre index (4-5 s, 4 gardés en mémoire, jamais rangés) ; un vrai AO utilise l'index « courant ».
- Typos sur noms courts d'acheteurs : voir §6.
- Après un déploiement, la 1ʳᵉ requête attend le chargement du corpus (≈ 8 s) si le préchauffage de démarrage n'a pas fini.
- Je n'ai pas ajouté le calcul à `planificateur.py` : l'index se refait tout seul quand `pv_travaux.db` change.

## À tester par Hamza

1. Connecte-toi (`/copilote/entrer`), puis dans la barre d'adresse : `/api/copilote/similaires/<une ref_annonce de /ao-ouverts>`. Vérifie la famille, la nature, les qualifications lues (`B.1` + classe), `acheteur.statut`, et les 3 premiers résultats : sont-ils des marchés que tu aurais choisis ?
2. Un AO alloti sans `?lot=` doit répondre 400 avec la liste des lots ; avec `?lot=1`, l'estimation du lot est utilisée.
3. `…?poids=acheteur:60,objet_nature:20` : le classement doit changer, `poids` et `poids_effectif` sont renvoyés.
4. Ouvre `rapports/copilote_non_classes.csv` dans Excel, ajoute les mots-clés dans `copilote/nature_mots.json`, puis `python -m copilote.mesures_nature` : la couverture doit monter.
5. Si un AO a un acheteur « aucun » : `/api/copilote/correspondances` montre les propositions ; valide ou refuse depuis la console du navigateur :
   `fetch('/api/copilote/correspondance',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({cle_ao:'…',cle_historique:'…',statut:'validee',commentaire:'même commune'})}).then(r=>r.json())`
   (l'interface viendra avec l'étape 7). Recharge l'analyse : `cles_utilisees` doit contenir ta clé.
6. Sur le serveur : `python -m copilote.acheteurs mesurer` (combien d'AO ouverts sans acheteur reconnu) et, si tu veux, `python -m copilote.rejouer_ao` puis `/api/copilote/similaires/REJEU-01`.

Prochaine étape : 5 — concurrents probables et taux de chacun. Je m'arrête ici en attendant ta validation.
