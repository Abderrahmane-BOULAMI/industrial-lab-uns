# Préparation à l'industrialisation

Ce qui doit être vrai avant de passer une solution validée dans le LAB en atelier.

## Liste de contrôle

| Domaine | Point de contrôle | État dans ce dépôt |
|---|---|---|
| Configuration | Équipements, variables et racine UNS décrits dans un fichier unique, validé avant démarrage (`python -m labuns check`) | fait |
| Configuration | Aucune adresse ni mot de passe dans le code | fait |
| Données | Contrat de message versionné et vérifié à la réception | fait |
| Données | Pertes et doublons mesurés par numéro de séquence | fait |
| Robustesse | Coupure réseau, lien silencieux, redémarrage edge, perte d'équipement testés | fait (simulation) |
| Robustesse | Taille maximale du tampon et comportement à saturation définis | à faire : dimensionner selon la durée de coupure à couvrir |
| Sécurité | Authentification, ACL, rejet des messages hors contrat | fait |
| Sécurité | TLS MQTT, sécurité OPC UA, revue par l'équipe cyber du site | à faire |
| Exploitation | Topic de santé par équipement, compteurs de qualité exposés par API | fait |
| Exploitation | Supervision de la passerelle (CPU, disque, profondeur du tampon) et alertes | à faire |
| Exploitation | Procédure de mise à jour de la passerelle sans perte (rejouée par S3) | fait (simulation) |
| Déploiement | Images conteneur et `docker-compose` pour un poste edge | fourni, non exécuté dans la CI |
| Validation | Scénarios S2 à S7 rejoués sur le LAB physique avec de vrais équipements | à faire |
| Documentation | Architecture, interfaces, cybersécurité, catalogue de cas d'usage | fait |

## Démarche proposée pour un nouveau cas d'usage

1. **Cadrer** : besoin métier, indicateur attendu, équipements et variables concernés (fiche du catalogue).
2. **Brancher dans le LAB** : ajouter les équipements à `config/lab.yaml`, valider la configuration.
3. **Tester** : rejouer les scénarios S1 à S8, ajouter un scénario propre au cas d'usage si besoin.
4. **Évaluer** : grille de maturité, valeur démontrée (indicateur juste, gain attendu chiffré).
5. **Industrialiser** : traiter la colonne « à faire » ci-dessus avec les équipes IT, OT et cybersécurité.
