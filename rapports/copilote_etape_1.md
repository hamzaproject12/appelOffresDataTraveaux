# Copilote — étape 1 : le socle

Branche `copilote`, rien de commité. Tests : `python -m unittest discover -s tests -t .` → 59 OK.
`python tester_ao_ouverts.py` → tous les contrôles passent.

## Ce qui est en place

| Fichier | Rôle |
|---|---|
| `copilote/config.py` | `COPILOTE_CALCUL`, `COPILOTE_RETOURS` (défaut `/data/…`, sinon à côté du script) |
| `copilote/bases.py` | `historique()` et `ao_ouverts()` en lecture seule (`mode=ro` + `query_only`), `calcul()` et `retours()` en lecture-écriture |
| `copilote/acces.py` | `COPILOTE_ACCES="nom:code,…"`, cookie signé `pvc` (clé dérivée de SECRET), 8 essais ratés / 10 min / adresse |
| `copilote/routes.py` | `creer_routeur(SECRET, BASE, AO_BASE)` ; `/copilote/entrer`, `/copilote`, `/api/copilote/etat` |
| `copilote/donnees.py` | unités (marché ou lot) en taux, `rejouer_attribution()`, `concordance()` |
| `copilote/references.py` | bases simples (offre fictive à taux fixe) |
| `copilote/doublons.py` | doublons de sociétés, rapport seul |
| `copilote/mesures.py` | `python -m copilote.mesures` |

**serveur.py** : 2 imports + 1 ligne `app.include_router(creer_routeur(SECRET, BASE, AO_BASE))`, plus
l'extension de `verifier_separation_ao` (chaque base du copilote est refusée si elle désigne la base
servie, son `-wal/-shm/.tmp/.neuf/.gz/.amorce`, le journal, `ao_ouverts.db`, ou l'autre base du copilote).
Les routes protégées sont déclarées sur un sous-routeur dont la garde s'applique d'office ; un test
parcourt le schéma OpenAPI pour vérifier qu'aucune route `/copilote*` ou `/api/copilote*` n'est ouverte.

**Accès** : il faut le cookie visiteur `pv` ET le cookie `pvc`. Sans `pv` : retour à `/`. Sans `pvc` :
`/copilote/entrer` (pages) ou 401 (API). Un compte retiré de `COPILOTE_ACCES` perd l'accès tout de suite.
Sans `COPILOTE_ACCES`, `/copilote/entrer` répond 503 et rien ne passe ; le reste du site est inchangé.

**Fichiers hors copilote/ que j'ai dû toucher** (non demandés explicitement) :
- `Dockerfile` : `COPY copilote ./copilote`. Sans cette ligne l'image (`COPY *.py` seulement) planterait
  à l'import de serveur.py au prochain déploiement.
- `.gitignore`, `.dockerignore` : `copilote_calcul.db`, `copilote_retours.db` (et `ao_ouverts.db` côté Docker).
- `requirements.txt` : numpy, pandas, scipy, scikit-learn, networkx, rapidfuzz, sans version.

## Rejeu de l'attribution

La logique est importée de `construire_base.py` (`prix_de_reference`, `classement_commission`,
`offres_du_marche`, `dans_la_fourchette`, `offre_de`) ; un test vérifie qu'elle n'est pas recopiée.
21 082 unités : 16 850 marchés ordinaires + 4 232 lots (`principale = 1`, lots lus un par un).

Le prix de référence rejoué est identique à celui stocké dans la base pour **100 %** des unités
comparables (0 écart > 0,01).

| période | unités | non attribuées | sans réf. | comparables | concordants | taux |
|---|---:|---:|---:|---:|---:|---:|
| global | 21 082 | 2 574 | 90 | 18 418 | 16 859 | **91,5 %** |
| 2024 | 2 955 | 315 | 15 | 2 625 | 2 406 | 91,7 % |
| 2025 | 10 854 | 1 313 | 49 | 9 492 | 8 649 | 91,1 % |
| 2026 | 7 273 | 946 | 26 | 6 301 | 5 804 | 92,1 % |

Par nombre d'offres retenues : 1–2 offres 96,6 % · 3–4 offres 86,3 % · 5–7 offres 86,5 % · 8+ offres 91,5 %.

## Bases simples 2026 (offre fictive admise, taux fixe, dans chaque marché attribué)

n = 6 301 unités (2025 : 9 492). Taux de victoire, extraits de la grille de 41 taux :

| taux | 0,95 | 0,97 | 1,00 | 1,02 | 1,05 |
|---|---:|---:|---:|---:|---:|
| 2026 | **47,9 %** | 45,7 % | 42,2 % | 41,1 % | 26,6 % |
| 2025 | 42,2 % | — | — | — | — |

Ces pourcentages sont tirés vers le haut par les unités à peu d'offres. Par tranche (2026) :

| offres reçues avant la nôtre | n | 0,95 | 1,00 | 1,05 |
|---|---:|---:|---:|---:|
| 1–2 | 2 490 | 88,8 % | 76,6 % | 52,7 % |
| 3–4 | 1 236 | 45,4 % | 42,9 % | 21,2 % |
| 5–7 | 922 | 19,2 % | 15,9 % | 8,1 % |
| 8+ | 1 653 | 4,0 % | 4,5 % | 1,5 % |

**Référence à battre = la tranche, pas le chiffre global.** 1 470 des 6 301 unités n'ont qu'une seule
offre : l'offre fictive y gagne d'office. Le tableau complet (41 taux × 2 années) est produit par
`python -m copilote.mesures 2026`.

## Doublons de sociétés

11 631 sociétés ; **489 paires** de clés très proches (ratio rapidfuzz ≥ 90, clés ≥ 6 caractères) jamais
présentes sur un même marché → `rapports/copilote_doublons.csv` (séparateur `;`, ouvrable dans Excel).
Aucune fusion. 48 paires à ≥ 95 ; 192 paires où l'une des deux a ≥ 20 marchés.

Vrais doublons probables : `HAJ DOUIRI SOHADU` / `HAJ DUIRI SOHADU`, `BENTAHA HOLDING SERVICE` /
`…SERVICES`, `ADEME MULISERVICES` / `…MULTISERVICES`. Faux positifs probables : `ORTIS MAROC` / `OTIS MAROC`,
`AB CONSTRUCTION` / `MAB CONSTRUCTION`, `WADA` / `WAMDA` / `WADAM TRAVAUX`. Le seuil seul ne les sépare pas :
une fusion automatique serait dangereuse.

## Limites

- Le meilleur taux fixe est au bord de la grille (0,95) : l'optimum réel est peut-être plus bas.
- L'offre fictive est supposée admise ; à égalité exacte de montant elle passe après l'offre déjà déposée.
- « Vrai gagnant » = `cle_attributaire` du marché ou du lot. Les marchés non attribués sont exclus des deux mesures.
- Les 375 concurrents multi-lots (`lots = "1,2"`) ont un seul montant, appliqué à chaque lot, comme dans construire_base.py.
- `--seuil` de construire_base n'est pas pris en compte (0 par défaut, comme la base livrée).
- La page `/copilote` est un simple écran d'état, non vérifiée dans un navigateur (testée par TestClient seulement).
- `networkx` est dans requirements.txt mais n'est pas installé sur ce poste et pas encore utilisé.

## À tester par Hamza

1. Définir `COPILOTE_ACCES` (ex. PowerShell : `$env:COPILOTE_ACCES="Hamza:un-code-long"`), lancer
   `uvicorn serveur:app`, ouvrir `/`, donner un nom.
2. Ouvrir `/copilote` : tu dois arriver sur `/copilote/entrer`. Un mauvais code est refusé ; le bon ouvre une
   page « Socle en place » avec ton nom et un tableau de 4 bases (pv_travaux.db et ao_ouverts.db « lecture
   seule », les deux bases du copilote « lecture-écriture »). Vérifier clair et sombre, à 390 px et 1 440 px.
3. Dans une fenêtre privée, ouvrir `/copilote/entrer` sans avoir donné de nom : retour à `/`.
4. Ouvrir `rapports/copilote_doublons.csv` : parcourir une vingtaine de paires et dire à partir de quel
   score tu juges les doublons fiables.
5. Relire les tableaux ci-dessus : l'offre fictive « admise, qui gagne si la commission la désigne » est-elle bien
   la base voulue, et la grille s'arrête-t-elle assez bas (0,95) ?
6. Avant le déploiement Railway : définir `COPILOTE_ACCES`. Sans lui le site fonctionne, le copilote reste fermé.
   La prochaine image est plus lourde (numpy, pandas, scipy, scikit-learn).

Prochaine étape après validation : 2 — groupes de présence commune (calcul, planificateur hebdomadaire).
