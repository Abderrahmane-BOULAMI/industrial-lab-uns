"""Lancement d'un composant du LAB (utilisé par docker-compose) ou du LAB complet en local.

    python -m labuns lab                  # tout dans un processus, broker intégré (démonstration)
    python -m labuns sim                  # équipements simulés seuls (OPC UA :4840, Modbus :5020)
    python -m labuns gateway              # passerelle edge, broker défini par MQTT_HOST / MQTT_PORT
    python -m labuns historian            # historien + API REST sur :8000

Mots de passe : variables d'environnement GATEWAY_PASSWORD et HISTORIAN_PASSWORD (jamais dans le dépôt).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .config import validate_config
from .lab import Lab, load_config


async def _forever():
    while True:
        await asyncio.sleep(3600)


async def run_lab(cfg):
    async with Lab(cfg) as lab:
        print(f"LAB démarré. Broker 127.0.0.1:{lab.ports['broker']}, racine UNS {lab.root}")
        print(f"Compte lecteur : {lab.users['usecase']} / {lab.passwords['usecase']}  (Ctrl+C pour arrêter)")
        await _forever()


async def run_sim(cfg):
    from .simulators import SimulatedShop
    host = os.environ.get("SIM_HOST", "0.0.0.0")
    cfg["sources"]["modbus"]["host"] = host
    shop = SimulatedShop(cfg, opcua_endpoint=f"opc.tcp://{host}:4840/lab/", modbus_port=5020)
    await shop.start()
    print("Équipements simulés : OPC UA :4840, Modbus TCP :5020")
    await _forever()


async def run_gateway(cfg):
    from .gateway import EdgeGateway
    sim = os.environ.get("SIM_HOST", "127.0.0.1")
    gw = EdgeGateway(cfg, broker=(os.environ.get("MQTT_HOST", "127.0.0.1"), int(os.environ.get("MQTT_PORT", 1883))),
                     password=os.environ["GATEWAY_PASSWORD"], opcua_endpoint=f"opc.tcp://{sim}:4840/lab/",
                     modbus=(sim, 5020), buffer_path=os.environ.get("BUFFER_PATH", "gateway_buffer.sqlite"))
    await gw.start()
    print(f"Passerelle {gw.gw_id} démarrée, mode {gw.delivery}")
    await _forever()


def run_historian(cfg):
    import uvicorn

    from .historian import Historian, create_app
    db = os.environ.get("DB_PATH", "historian.sqlite")
    h = Historian(cfg, broker=(os.environ.get("MQTT_HOST", "127.0.0.1"), int(os.environ.get("MQTT_PORT", 1883))),
                  password=os.environ["HISTORIAN_PASSWORD"], db_path=db)
    h.start(timeout_s=30)
    uvicorn.run(create_app(db), host="0.0.0.0", port=int(os.environ.get("API_PORT", 8000)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="labuns")
    ap.add_argument("role", choices=["lab", "sim", "gateway", "historian", "check"])
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    errors = validate_config(cfg)
    if errors:
        print("Configuration invalide :\n- " + "\n- ".join(errors))
        sys.exit(2)
    if a.role == "check":
        print("Configuration valide.")
    elif a.role == "historian":
        run_historian(cfg)
    else:
        asyncio.run({"lab": run_lab, "sim": run_sim, "gateway": run_gateway}[a.role](cfg))


if __name__ == "__main__":
    main()
