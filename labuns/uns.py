"""Unified Namespace : construction des topics ISA-95 et format des messages.

Topic : <racine>/<cellule>/<équipement>/<variable>, par exemple
    entreprise/site-lab/usinage/ligne-1/cellule-5axes/CU-01/charge-broche
Santé d'une source : <racine>/<cellule>/<équipement>/_sante

Message (JSON) :
    {"v": 54.2, "ts": 1761040000123, "q": "GOOD", "u": "%", "src": "opcua:ns=2;s=CU-01.SpindleLoad",
     "gw": "edge-gw-01", "seq": 1842}
    v   valeur (nombre, texte ou null si la qualité est BAD)
    ts  horodatage source en millisecondes Unix (OPC UA : SourceTimestamp ; Modbus : instant de lecture)
    q   qualité : GOOD, UNCERTAIN ou BAD
    u   unité
    src adresse d'origine, pour remonter à l'équipement
    gw  passerelle émettrice ; seq numéro de séquence de la passerelle (détection des pertes et doublons)
"""
from __future__ import annotations

import json
import re

QUALITIES = ("GOOD", "UNCERTAIN", "BAD")
_SEGMENT = re.compile(r"^[A-Za-z0-9_\-]+$")
REQUIRED = {"v", "ts", "q", "u", "src", "gw", "seq"}


def topic(root: str, cell: str, equipment: str, variable: str) -> str:
    parts = [*root.split("/"), cell, equipment, variable]
    for p in parts:
        if not _SEGMENT.match(p):
            raise ValueError(f"segment de topic invalide : {p!r}")
    return "/".join(parts)


def payload(value, ts_ms: int, quality: str, unit: str, src: str, gw: str, seq: int) -> bytes:
    if quality not in QUALITIES:
        raise ValueError(quality)
    return json.dumps({"v": value, "ts": int(ts_ms), "q": quality, "u": unit, "src": src, "gw": gw,
                       "seq": int(seq)}, ensure_ascii=False, separators=(",", ":")).encode()


def validate(raw: bytes | str) -> dict:
    """Décode et vérifie un message UNS ; lève ValueError s'il ne respecte pas le contrat."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("JSON invalide") from exc
    if not isinstance(msg, dict):
        raise ValueError("objet JSON attendu")
    missing = REQUIRED - msg.keys()
    if missing:
        raise ValueError(f"champs manquants : {sorted(missing)}")
    if msg["q"] not in QUALITIES:
        raise ValueError(f"qualité inconnue : {msg['q']}")
    if not isinstance(msg["ts"], int) or not isinstance(msg["seq"], int):
        raise ValueError("ts et seq doivent être des entiers")
    if msg["q"] == "BAD" and msg["v"] is not None:
        raise ValueError("une valeur BAD doit être nulle")
    return msg
