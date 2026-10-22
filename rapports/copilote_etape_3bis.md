# Copilote — étape 3 bis : jeu de test d'AO rejoués

Branche `copilote`, rien de commité. Tests : `python -m unittest discover -s tests -t .` → 123 OK (99 + 24 nouveaux).
`python tester_ao_ouverts.py` → tous les contrôles passent. `serveur.py` n'est pas modifié.
Essayé de bout en bout sur un vrai serveur local avec des bases temporaires : les vraies `copilote_calcul.db` et
`ao_ouverts.db` n'ont reçu aucun AO « REJEU- » ni aucune table `rejeu_*` (c'est à toi de lancer la commande).

## Ce qui est en place

| Fichier | Rôle |
|---|---|
| `copilote/rejeu.py` | ce que le **moteur** sait d'un AO rejoué : sa date de coupure. `jusqua_pour`, `calcul_groupes_pour`, `unites_pour`. Un « REJEU- » sans coupure lève `RejeuSansCoupure` : jamais « tout l'historique » par défaut |
| `copilote/rejouer_ao.py` | choix des 20 marchés, annonce, vérité, envoi. Seul module qui lit `rejeu_verite` |
| `tests/test_rejeu.py`, `tests/test_serveur.py` | 23 + 1 tests ; le second poste les annonces au vrai `serveur.py` |
| `tests/outils.py` | colonnes ajoutées à la base de poche (`reference`, `caution_provisoire`, `intitule`, `classes`) |

Deux tables dans `copilote_calcul.db` :
- `rejeu_ao` (lisible par le moteur) : `ref_annonce`, `date_coupure`, l'annonce, `envoye_le` ;
- `rejeu_verite` (jamais lue par le moteur) : une ligne par marché ou lot, avec la **référence réelle**, la date d'ouverture, les concurrents (nom, clé, statut, montant, taux, rang), le prix de référence (stocké et rejoué), la moyenne des offres, le gagnant réel et celui que la règle de la commission désigne.

## Commandes

```
python -m copilote.rejouer_ao                 choisir (si pas déjà fait) puis envoyer
python -m copilote.rejouer_ao choisir [--n 20] [--allotis 3] [--graine 0] [--renouveler]
python -m copilote.rejouer_ao envoyer [--url http://127.0.0.1:8000]
python -m copilote.rejouer_ao groupes         calcule les groupes « jusqua-… » de chaque AO (≈ 1 min pour 20)
python -m copilote.rejouer_ao etat
```
Le jeton se lit dans `JETON_SERVICE` (jamais affiché ni rangé). L'envoi refuse une adresse non locale sans `--distant`.

## Comment le choix est fait

- **Candidats** : marchés de 2026, attribués, date non douteuse, non infructueux, dont **chaque** marché ou lot a une estimation, un gagnant, au moins 3 offres chiffrées et un prix de référence calculable. Un marché simple doit avoir une classe ; un alloti a 2 à 6 lots. Sont écartés ceux dont l'objet nomme un concurrent. Reste 1 683 candidats sur 6 250 marchés de 2026, dont 145 allotis.
- **Tirage déterministe** (même graine, même choix) : 3 allotis (2 lots, 3 à 4 lots, 5 à 6 lots), puis 17 marchés simples répartis sur les classes 1 à 5 et sur trois tranches de montant, un acheteur une seule fois, les mois les moins représentés d'abord.
- **Résultat** : classes 1 à 5 présentes (3, 3, 4, 4, 4 ; les 2 allotis restants n'en ont pas), estimations de 0,47 à 42,4 M, 20 acheteurs distincts, 9 mois de janvier à septembre.

## L'annonce envoyée

Format du POST : `ref_annonce` et `reference` = `REJEU-01` … `REJEU-20`, `objet`, `acheteur`, `estimation`, `caution_provisoire`, `qualifications` (`{"libelle": "B.1", "classe": "3"}` : le code, pas « B.1- Travaux… »), `lots`, `clients: ["Test"]`, `source: "rejeu"`. La date de publication (hier) et la date limite (dans 20 à 49 jours) sont posées **à l'envoi**.
Elle ne contient ni la référence réelle, ni un résultat. Pour un alloti, `estimation` est la somme des lots (le marché n'a pas d'estimation globale) et les qualifications viennent des classes des lots.

## Coupure temporelle

Pour un AO « REJEU- », la date de coupure est la **veille** de l'ouverture réelle ; elle est stricte : le marché lui-même et tout ce qui est daté du même jour ou après sont invisibles, comme les dates douteuses. Elle se branche sur ce qui existe : `groupes.charger(jusqua=…)`, `recalculer --jusqua`, calcul de groupes `jusqua-AAAA-MM-JJ`. Exemple : REJEU-04 s'ouvre le 06/05/2026, la coupure est le 05/05 et le dernier marché vu par ses groupes est du 05/05.

## Limites

- **Colonne « chez cet acheteur » de /ao-ouverts** : pour un AO rejoué, elle compte tout l'historique, le marché lui-même et les suivants compris. Ce n'est que de l'affichage (le moteur passe par `rejeu.py`), mais ne la prends pas pour ce que le moteur voit.
- **Pas de suppression côté serveur** : aucune route ne supprime un AO. Après `--renouveler`, ou si `copilote_calcul.db` est supprimée puis que `pv_travaux.db` change, d'anciens « REJEU- » restent dans `ao_ouverts.db` sans vérité. Le moteur les refuse (`RejeuSansCoupure`) ; le même choix se retrouve tant que la base d'historique et la graine ne changent pas.
- **Vérité et calculs dans le même fichier** : la séparation tient au code (un seul module lit `rejeu_verite`, test à l'appui), pas à un fichier distinct. Si tu veux une barrière physique, on la déplace dans sa propre base.
- **Allotis sans code de qualification** : l'annonce ne donne que des classes (`libelle: null`), l'historique des allotis n'ayant pas de qualification au niveau du marché. La comparaison par code (B.1) ne marchera pas pour ces 3 AO.
- **Échantillon favorable** : seuls les marchés bien documentés (3 offres chiffrées, prix de référence calculable) sont retenus, soit 27 % de 2026.
- **Dates** : une annonce expire 20 à 49 jours après l'envoi et quitte la page « En cours » ; renvoyer prolonge (idempotent).
- **Fusions** : les clés de la vérité sont celles après les 25 fusions validées ; les noms sont ceux du procès-verbal.
- Le moteur (étapes 4 et suivantes) n'existe pas encore : `rejeu.py` est le passage obligé, mais rien ne l'oblige à l'utiliser. Le jour venu, chaque module devra appeler `unites_pour` ou `jusqua_pour`.

## À tester par Hamza

1. Lancer le site en local avec un jeton, par exemple (PowerShell) : `$env:JETON_SERVICE="un-jeton-d-essai"; python -m uvicorn serveur:app`.
   Dans un autre terminal, avec le même jeton : `python -m copilote.rejouer_ao`. Attendu : « 20 reçus, 20 créés, 0 rejetés ».
2. Ouvrir `/ao-ouverts`, choisir le client **Test** : 20 AO `REJEU-01` à `REJEU-20`, dont 3 avec des lots (2, 3 et 5). Aucune référence réelle ne doit apparaître.
3. Relancer `python -m copilote.rejouer_ao envoyer` : « 0 créés, 20 mis à jour ».
4. `python -m copilote.rejouer_ao etat` : les 20 coupures. Vérifier au hasard 3 AO : la coupure est bien la veille de l'ouverture réelle.
5. Dans DB Browser, ouvrir `copilote_calcul.db`, table `rejeu_verite` : pour 3 AO que tu connais, comparer concurrents, taux, gagnant et prix de référence au procès-verbal.
6. `python -m copilote.rejouer_ao groupes` (≈ 1 min) : crée les calculs `jusqua-…` utilisés plus tard.
7. Dire si l'échantillon te convient (20 marchés, 3 allotis, classes et montants variés) ou s'il faut changer de graine (`choisir --graine 1 --renouveler`).
8. Dire si « la veille de l'ouverture » est bien la coupure voulue : un marché ouvert le même jour est exclu.
9. Tes AO de test sont réels dans `ao_ouverts.db` : ils resteront après l'essai. À ne pas faire sur Railway avec le vrai jeton sans `--distant`, qui est refusé exprès.

## FEU VERT

Critères fixés avant la mesure, à partir de la demande.

| critère | valeur mesurée | |
|---|---|---|
| 20 marchés attribués de 2026, dont 3 allotis | 20 ; 3 allotis (2, 3, 5 lots) ; tous de 2026 | **OK** |
| Variété : acheteurs tous distincts, classes 1 à 5 présentes, rapport de montants ≥ 10, ≥ 6 mois | 20 acheteurs ; classes 1 à 5 ; 0,47 à 42,4 M (× 90) ; 9 mois | **OK** |
| Annonces acceptées par le vrai POST, avec préfixe, client Test, qualifications `B.1` / classe, lots, date limite future | 20 reçus, 20 créés, 0 rejeté ; 20 / 20 préfixées, client Test, 20 / 20 dans le futur ; 10 lots ; qualifications `B.1`–`3` | **OK** |
| Aucune fuite dans l'annonce : ni référence réelle, ni nom de concurrent, ni gagnant | 0 sur les 20 AO réels (script) ; 0 aussi pour les montants, les clés et le gagnant dans le test automatique | **OK** |
| Vérité complète et cohérente : une ligne par marché ou lot, prix de référence stocké = rejoué | 27 lignes pour 20 AO (17 + 10 lots) ; 0 écart de prix de référence ; 26 / 27 gagnants retrouvés par la règle | **OK** |
| Coupure stricte : le marché lui-même et le jour même exclus, la veille vue, un « REJEU- » sans coupure refusé, groupes et profils sur la même coupure | tests verts ; REJEU-04 : dernier marché vu 05/05 pour une coupure du 05/05 ; `RejeuSansCoupure` levée | **OK** |
| La vérité est invisible au moteur | aucun autre module du copilote (ni `serveur.py`) ne cite `rejeu_verite` ni `lire_verite` ; aucun n'importe `rejouer_ao` (test) | **OK** |
| Envoi sûr : refus sans jeton, avec un mauvais jeton, vers une adresse distante ; jeton absent des messages | 3 refus (code 1) ; jeton absent de tous les messages (test) | **OK** |
| Idempotent | 2ᵉ envoi : 0 créé, 20 mis à jour | **OK** |
| Vraies bases et `serveur.py` intacts | aucune table `rejeu_*` dans la vraie `copilote_calcul.db`, 0 « REJEU- » dans la vraie `ao_ouverts.db`, 0 ligne de diff dans `serveur.py` | **OK** |
| Tests verts, `tester_ao_ouverts.py` vert | 123 / 123 ; « Tous les contrôles passent » | **OK** |

Aucun critère KO, donc aucune correction. Prochaine étape après validation : 4 — similarité des marchés.
