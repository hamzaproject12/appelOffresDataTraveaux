"""Copilote de soumission du site Travaux (chantier 7, voir CLAUDE.md).

Un module par étape. Le paquet se branche sur serveur.py par un APIRouter (routes.creer_routeur)
et ne touche jamais à pv_travaux.db ni à ao_ouverts.db autrement qu'en lecture seule.
"""
