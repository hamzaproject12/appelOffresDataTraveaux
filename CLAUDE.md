# Copilote de soumission — site Travaux

Ce fichier est relu à chaque session. Il donne le contexte et les règles du chantier « copilote ».
Le brief historique du site reste BRIEF_TRAVAUX.md (chantiers 1 à 6) ; le copilote est le chantier 7.

## Objectif
Depuis la page /ao-ouverts, un bouton « Analyser » ouvre une page qui déroule tout le processus
pour un appel d'offres (AO), étape par étape, de façon visible et modifiable :
1. marchés similaires (score détaillé) ;
2. sociétés probables, regroupées par groupe détecté ;
3. sous-historique et taux de chaque société ;
4. simulation de l'ouverture des plis → chance de gagner selon notre prix ;
5. décision GO / À ÉTUDIER / NO-GO avec notre coût et notre marge.
Une page /copilote/groupes permet de chercher une société, voir ses groupes et donner un avis.

## Architecture décidée
- Le code du copilote vit dans le paquet `copilote/` (un module par étape). Il est branché sur
  `serveur.py` par un `APIRouter` : on ajoute UNE ligne d'inclusion dans serveur.py, rien d'autre.
- Deux nouvelles bases SQLite, à part, sur le volume (/data), jamais dans l'image :
  - `copilote_calcul.db` : résultats recalculables (groupes, paires, index de similarité…).
    Peut être supprimée et reconstruite à tout moment par `python -m copilote.recalculer`.
  - `copilote_retours.db` : saisies humaines à conserver (avis sur les groupes, taux modifiés,
    profil de l'entreprise, nos soumissions). Jamais supprimée ni reconstruite.
  Les deux chemins (variables COPILOTE_CALCUL, COPILOTE_RETOURS) sont ajoutés au contrôle de
  séparation existant (même logique que verifier_separation_ao).
- `pv_travaux.db` est TOUJOURS ouverte en lecture seule (mode=ro). Elle est remplacée à chaque
  déploiement : rien ne doit jamais y être écrit.
- `ao_ouverts.db` est lue en lecture seule par le copilote (c'est serveur.py qui l'alimente).
- Accès : toutes les routes /copilote/* et /api/copilote/* exigent une authentification dédiée
  (variable COPILOTE_ACCES = "nom:code,nom:code"), en plus du cookie visiteur. Un visiteur qui
  n'a donné que son nom n'y accède pas. Chaque avis enregistré garde le nom de son auteur.
- Les calculs lourds et réguliers sont en Python : groupes (chaque semaine, via planificateur.py),
  similarité, sous-historiques. Le serveur renvoie un « dossier d'analyse » JSON complet.
- Les calculs interactifs sont faits dans le navigateur, en JavaScript natif : taux final pondéré,
  prix de référence, simulation, décision. Ils sont recalculés instantanément à chaque modification
  (marché similaire décoché, société retirée, taux modifié, coût saisi).
- Le simulateur existe en deux copies identiques : JavaScript (interface) et Python (backtest).
  Un fichier de cas de test commun (tests/cas_simulation.json) garantit qu'elles donnent le même
  résultat.

## Règles métier
- Taux d'une offre : r = montant / estimation. Tous les calculs se font en taux.
- Prix de référence P = (moyenne des offres admises + estimation) / 2. Le gagnant est l'offre la plus
  proche de P par défaut, sinon la plus proche par excès. Reprendre exactement la logique de
  construire_base.py (prix_de_reference, classement_commission, offres_utilisables) en l'important,
  jamais en la recopiant.
- Pièges connus (voir BRIEF_TRAVAUX.md, chantier 6) : `m.principale = 1` obligatoire ; un marché
  alloti se lit lot par lot (table lots) ; la clé d'acheteur passe par cle_acheteur(). Un AO alloti
  s'analyse lot par lot.
- Qualifications : l'AO donne {"libelle": "B.1", "classe": "3"} alors que l'historique donne
  « B.1- Travaux de terrassements routiers courants ». La comparaison se fait sur le code (B.1).
- Backtest temporel strict : pour un marché du jour d, n'utiliser que des données antérieures à d,
  y compris pour les groupes et les profils. Aucune fuite.
- Une méthode n'est gardée que si elle bat une base simple au backtest.
- Peu d'historique ⇒ rapprocher le taux de celui des sociétés comparables.
- Chaque chiffre affiché peut être expliqué : d'où il vient, sur combien de marchés.
- Pas de LLM ni d'API externe à ce stade.

## Règles juridiques (non négociables)
- Le système ne propose jamais de coordonner des prix avec d'autres soumissionnaires : ni offres de
  couverture, ni répartition de marchés, ni prix suggérés à des tiers. Un partenaire n'entre dans le
  calcul que comme groupement déclaré (une seule offre commune), sous-traitant ou fournisseur.
- Les groupes sont des faits statistiques de présence commune, pas des accusations. Vocabulaire
  neutre : « groupe de présence commune », « signal ». Jamais « entente », « collusion », « fraude ».
- Les avis demandés sont neutres : « Ces sociétés sont-elles liées (partenaires habituels, même
  dirigeant, groupement) ? Oui / Non / Je ne sais pas » + commentaire.

## Interface
- Même charte que dashboard.html et ao_ouverts.html : jetons de couleur, thème sombre, clé
  localStorage `pv-theme`, typographie. HTML + JavaScript natif, pas de framework, pas de build,
  pas de CDN. Graphiques en SVG dessinés à la main.
- Chaque étape de la page d'analyse montre : le résultat, comment il est calculé, et ce qu'on peut
  modifier. Une modification est visible (valeur d'origine barrée ou rappelée) et réversible.
- Lisible à 390 px et à 1 440 px.

## Économie de contexte (important)
- Ne jamais ouvrir pv_travaux.db, ao_ouverts.db, journal.db, *.gz, amorce.tar.gz, ni les dossiers
  extraits/, consultations/, rattrapage/, sondage*/ : interroger les bases par de petites requêtes
  SQL, lister les dossiers sans les lire.
- dashboard.html et serveur.py sont gros : chercher (grep) la partie utile et ne lire qu'elle.
- Rapports courts : chiffres, tableau, limites, prochaine étape. Pas de récit.

## Méthode de travail
- On travaille sur la branche git `copilote`. Pas de commit ni de push sans demande explicite.
- Une étape à la fois. Chaque étape se termine par :
  - les tests (tests/) qui passent, et `python tester_ao_ouverts.py` toujours vert ;
  - rapports/copilote_etape_N.md ;
  - une liste « À tester par Hamza » : ce qu'il doit ouvrir et vérifier dans l'interface.
  Ensuite, s'arrêter et attendre la validation.

## Avancement
- [ ] 1 Socle   - [ ] 2 Groupes (calcul)   - [ ] 3 Page groupes   - [ ] 4 Similarité
- [ ] 5 Concurrents + taux   - [ ] 6 Simulateur   - [ ] 7 Page d'analyse   - [ ] 8 Décision
- [ ] 9 Finitions et déploiement
(Cocher ici à la fin de chaque étape validée.)
