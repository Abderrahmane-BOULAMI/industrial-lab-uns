# Catalogue de cas d'usage du LAB

Chaque fiche décrit un cas d'usage, les données UNS qu'il consomme et la façon de le valider dans le LAB.

## CU-1 : Disponibilité et production des centres d'usinage (implémenté)

- **Besoin** : suivre en continu la disponibilité et la production de chaque machine, sans saisie manuelle.
- **Données** : `.../CU-0x/etat`, `.../CU-0x/compteur-pieces`.
- **Calcul** : part du temps en PRODUCTION sur la fenêtre, à partir des horodatages source des changements d'état.
- **Validation** : comparaison avec la vérité du simulateur (scénario S1) ; critère : écart inférieur à 1 point de
  disponibilité et compteur de pièces identique.

## CU-2 : Traçabilité des cycles de traitement thermique (à construire)

- **Besoin** : prouver pour chaque lot que la température est restée dans la tolérance pendant le palier.
- **Données** : `.../FOUR-01/temperature`, `consigne`, `phase`, `lot`.
- **Calcul** : par lot, écart maximal à la consigne pendant la phase `palier`, durée hors tolérance.
- **Validation** : injecter une dérive de température dans le simulateur et vérifier que le lot est signalé ;
  vérifier qu'une valeur `BAD` pendant le palier rend le lot « non prouvé » plutôt que conforme.

## CU-3 : Détection de dérive de charge broche (à construire)

- **Besoin** : repérer une usure d'outil ou une anomalie de coupe avant le défaut.
- **Données** : `.../CU-0x/charge-broche`, `programme`.
- **Calcul** : enveloppe de charge par programme, alerte sur dépassement persistant.
- **Validation** : dérive injectée dans le simulateur ; taux de fausses alertes sur un fonctionnement nominal.

## CU-4 : Supervision de la chaîne de données elle-même (implémenté)

- **Besoin** : savoir à tout moment si les données d'un équipement sont fraîches et fiables.
- **Données** : `.../_sante`, champ `q` de chaque message, API `/qualite`.
- **Validation** : scénarios S6 et S7 (détection de perte d'équipement en moins de 3 et 5 secondes).
