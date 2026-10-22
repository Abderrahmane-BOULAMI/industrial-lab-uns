# Spécifications des interfaces

Ce document formalise les accès aux équipements, les flux et le contrat des messages. Il est la référence pour
brancher un nouvel équipement ou un nouveau consommateur.

## 1. Arborescence UNS (ISA-95)

```
<entreprise>/<site>/<zone>/<ligne>/<cellule>/<équipement>/<variable>
entreprise/site-lab/usinage/ligne-1/cellule-5axes/CU-01/charge-broche
entreprise/site-lab/usinage/ligne-1/traitement-thermique/FOUR-01/temperature
entreprise/site-lab/usinage/ligne-1/cellule-5axes/CU-01/_sante
```

Règles :

- segments en minuscules, chiffres, `-` et `_` uniquement (pas d'espace, pas de `+` ni de `#`) ;
- un topic par variable ; les variables techniques commencent par `_` (`_sante`) ;
- la racine est définie une seule fois dans `config/lab.yaml`.

## 2. Contrat des messages

```json
{"v": 54.2, "ts": 1761040000123, "q": "GOOD", "u": "%",
 "src": "opcua:ns=2;s=CU-01.SpindleLoad", "gw": "edge-gw-01", "seq": 1842}
```

| Champ | Type | Règle |
|---|---|---|
| `v` | nombre, texte ou `null` | `null` obligatoire si `q` vaut `BAD` |
| `ts` | entier | millisecondes Unix, horodatage **source** |
| `q` | texte | `GOOD`, `UNCERTAIN` (automate figé : battement inchangé) ou `BAD` (source perdue) |
| `u` | texte | unité (`%`, `degC`, `pieces`, vide si sans unité) |
| `src` | texte | adresse d'origine (`opcua:ns=..;s=..` ou `modbus:<esclave>/hr<adresse>`) |
| `gw` | texte | identifiant de la passerelle émettrice |
| `seq` | entier | numéro de séquence croissant de la passerelle, persistant entre redémarrages |

Un message qui ne respecte pas ce contrat est rejeté et compté par l'historien (`labuns/uns.py: validate`).

Garantie de livraison : **au moins une fois** (QoS 1 + tampon). Les consommateurs dédoublonnent sur `(gw, seq)`.

## 3. Équipements

### Centres d'usinage CU-01 et CU-02 (OPC UA)

| Nœud | Type | Variable UNS | Unité |
|---|---|---|---|
| `ns=2;s=CU-0x.State` | String | `etat` (PRODUCTION, ATTENTE, ARRET, DEFAUT) | |
| `ns=2;s=CU-0x.SpindleLoad` | Double | `charge-broche` | % |
| `ns=2;s=CU-0x.PartCount` | UInt32 | `compteur-pieces` | pièces |
| `ns=2;s=CU-0x.Program` | String | `programme` | |
| `ns=2;s=CU-0x.Alarm` | String | `alarme` | |

Point d'accès `opc.tcp://<hôte>:4840/lab/`, espace de noms `urn:lab:machines`, abonnement à 100 ms.
Dans le LAB logiciel, sécurité OPC UA désactivée ; voir `cybersecurite.md` pour la cible.

### Four FOUR-01 (Modbus TCP, esclave 1, registres de maintien)

| Adresse | Variable UNS | Échelle | Unité |
|---|---|---|---|
| 0 | `temperature` | x 0,1 | degC |
| 1 | `consigne` | x 0,1 | degC |
| 2 | `phase` (0 attente, 1 montée, 2 palier, 3 refroidissement) | énumération | |
| 3 | `lot` | 1 | |
| 4 | battement (non publié, sert à détecter un automate figé) | 1 | |

Scrutation toutes les 500 ms, délai de réponse 1 s ; source déclarée `BAD` après 2 échecs consécutifs.

## 4. Comptes et droits sur le broker

| Compte | Publie sur | S'abonne à |
|---|---|---|
| `gateway` | `<racine>/#` | rien de l'UNS (moindre privilège) |
| `historian` | `lab/historien/#` | `<racine>/#` |
| `cas-usage` | `lab/cas-usage/#` | `<racine>/#` |
| anonyme | refusé | refusé |

## 5. API de l'historien (lecture seule)

| Méthode | Chemin | Réponse |
|---|---|---|
| GET | `/sante` | nombre de messages, instant du dernier message |
| GET | `/variables` | dernière valeur, qualité et unité de chaque topic |
| GET | `/historique/{topic}?depuis_ms=&limite=` | série d'une variable (404 si inconnue) |
| GET | `/qualite` | messages reçus, doublons, invalides, répartition par qualité |

Documentation interactive OpenAPI générée automatiquement sur `/docs`.
