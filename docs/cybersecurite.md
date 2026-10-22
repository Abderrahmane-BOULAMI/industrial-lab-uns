# Cybersécurité du LAB

Approche par **zones et conduits** (IEC 62443-3-2), au niveau de base. Le LAB ne remplace pas une analyse de
risque par l'équipe cybersécurité industrielle du site ; il fournit des contrôles testables.

## Zones et conduits

| Zone | Contenu | Conduit sortant | Règle |
|---|---|---|---|
| OT cellule | automates, CN, serveurs OPC UA | vers l'edge uniquement | aucun accès direct depuis l'IT |
| Edge (DMZ industrielle) | passerelle | vers le broker, MQTT sortant | la passerelle initie les connexions, rien n'entre |
| Données | broker, historien, cas d'usage | API REST en lecture | comptes nominatifs, droits par topic |

## Contrôles testés automatiquement (scénario S8)

1. Connexion anonyme au broker refusée.
2. Mot de passe erroné refusé.
3. Un compte lecteur ne peut pas injecter de fausse mesure dans l'UNS (ACL en publication).
4. La passerelle ne peut pas lire l'UNS (moindre privilège).
5. Un message hors contrat est rejeté par l'historien.
6. Les mots de passe ne sont stockés que sous forme d'empreinte argon2, et sont générés à chaque lancement
   (aucun secret dans le dépôt).

## Écarts connus dans le LAB logiciel (à traiter avant tout déploiement)

| Écart | Cible |
|---|---|
| MQTT sans TLS | TLS 1.2+ sur le port 8883, certificats émis par la PKI du site ; `deploy/mosquitto.conf` contient la configuration à activer |
| OPC UA sans sécurité | politique `Basic256Sha256`, mode `SignAndEncrypt`, certificats d'application approuvés |
| Modbus TCP sans authentification (limite du protocole) | Modbus confiné à la zone OT, filtré par pare-feu ; Modbus/TCP Security si l'automate le permet |
| Pas de journalisation centralisée | événements de connexion et refus d'ACL envoyés au SIEM |
| Pas de gestion des correctifs | inventaire des versions (SBOM) et veille sur les composants |
