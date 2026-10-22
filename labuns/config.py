"""Validation de la configuration du LAB : une erreur de saisie doit être refusée avant le déploiement."""
from __future__ import annotations

import re

from . import uns

_ENDPOINT = re.compile(r"^opc\.tcp://[^/:]+:\d+(/.*)?$")


def validate_config(cfg: dict) -> list[str]:
    """Renvoie la liste des erreurs (vide si la configuration est utilisable)."""
    errors = []
    for key in ("uns", "broker", "sources", "gateway", "historian"):
        if key not in cfg:
            errors.append(f"section manquante : {key}")
    if errors:
        return errors
    root = cfg["uns"].get("root", "")
    try:
        uns.topic(root, "c", "e", "v")
    except ValueError as exc:
        errors.append(f"racine UNS invalide : {exc}")
    if cfg["gateway"].get("delivery") not in ("aucun", "memoire", "disque"):
        errors.append("gateway.delivery doit valoir aucun, memoire ou disque")
    o = cfg["sources"].get("opcua", {})
    if o and not _ENDPOINT.match(o.get("endpoint", "")):
        errors.append(f"point d'accès OPC UA invalide : {o.get('endpoint')}")
    topics = set()
    for eq in o.get("equipments", []):
        for node, tag in eq.get("tags", {}).items():
            try:
                t = uns.topic(root, eq["cell"], eq["id"], tag["name"])
            except (KeyError, ValueError) as exc:
                errors.append(f"OPC UA {eq.get('id')}.{node} : {exc}")
                continue
            if t in topics:
                errors.append(f"topic en double : {t}")
            topics.add(t)
    m = cfg["sources"].get("modbus", {})
    addrs = set()
    for r in m.get("registers", []):
        if not 0 <= r.get("address", -1) <= 65535:
            errors.append(f"adresse Modbus hors plage : {r}")
        if r["address"] in addrs:
            errors.append(f"adresse Modbus en double : {r['address']}")
        addrs.add(r["address"])
        if r.get("name") != "battement":
            try:
                t = uns.topic(root, m["cell"], m["equipment"], r["name"])
                if t in topics:
                    errors.append(f"topic en double : {t}")
                topics.add(t)
            except ValueError as exc:
                errors.append(str(exc))
    if m and not any(r.get("name") == "battement" for r in m.get("registers", [])):
        errors.append("aucun registre de battement : un automate figé ne serait pas détecté")
    return errors
