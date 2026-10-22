"""Grille de maturité du LAB, inspirée des niveaux TRL, alimentée par les résultats des scénarios.

Un niveau n'est atteint que si tous ses critères sont validés et que le niveau précédent l'est aussi.
Le niveau 5 demande des essais sur équipements réels : il ne peut pas être validé par la simulation.
"""
from __future__ import annotations

from pathlib import Path

LEVELS = [
    (1, "Démonstrateur", "Le flux de bout en bout fonctionne et un indicateur utile est juste.",
     [("S1", "flux nominal sans perte, latence p95 sous 500 ms, disponibilité juste à 1 point près")]),
    (2, "Robuste aux défauts réseau", "Aucune perte de donnée sur les défauts réseau et équipement joués.",
     [("S2", "coupure franche du lien montant"), ("S3", "coupure et redémarrage de la passerelle"),
      ("S4", "lien silencieux"), ("S5", "réseau lent"), ("S6", "perte de l'automate Modbus"),
      ("S7", "perte du serveur OPC UA")]),
    (3, "Sécurisé (base)", "Accès authentifiés, droits minimaux, messages contrôlés.",
     [("S8", "six contrôles de sécurité")]),
    (4, "Exploitable et reproductible", "Configuration réutilisable, documentation et API disponibles.",
     [("DOC", "architecture, spécifications d'interfaces, sécurité, industrialisation"),
      ("CFG", "passerelle décrite entièrement par un fichier de configuration validé"),
      ("API", "API de l'historien testée")]),
    (5, "Validé sur équipements réels", "Essais sur automates et machines du LAB physique, en charge réelle.",
     [("REEL", "non réalisable en simulation")]),
]

DOCS = ("docs/architecture.md", "docs/specifications_interfaces.md", "docs/cybersecurite.md",
        "docs/industrialisation.md", "docs/catalogue_cas_usage.md")


def evaluate(results: dict, root: Path) -> list[dict]:
    """results : {"S1": bool, ..., "S8": bool, "API": bool, "CFG": bool}."""
    status = dict(results)
    status["DOC"] = all((root / d).exists() for d in DOCS)
    status["REEL"] = False
    out, reached = [], True
    for level, name, desc, crit in LEVELS:
        ok = all(status.get(k, False) for k, _ in crit)
        reached = reached and ok
        out.append({"niveau": level, "nom": name, "description": desc, "atteint": reached,
                    "criteres": [{"id": k, "libelle": lab, "ok": bool(status.get(k, False))} for k, lab in crit]})
    return out


def current_level(grid: list[dict]) -> int:
    return max((g["niveau"] for g in grid if g["atteint"]), default=0)
