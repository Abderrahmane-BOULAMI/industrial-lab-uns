"""Assemblage du LAB dans un seul processus : broker MQTT sécurisé, équipements simulés, proxys de défauts,
passerelle edge et historien. Tout tourne sur un portable ou dans la CI, sans Docker ni matériel.

    zone OT (cellule)              conduit                      zone IT / données
    OPC UA  (CU-01, CU-02) --proxy--+                       +--> historien + API
                                    +--> passerelle --proxy-+--> broker MQTT (UNS)
    Modbus  (FOUR-01)      --proxy--+                       +--> cas d'usage (TRS)
"""
from __future__ import annotations

import asyncio
import copy
import logging
import secrets
import socket
import tempfile
import time
from pathlib import Path

import yaml
from amqtt.broker import Broker
from pwdlib.hashers.argon2 import Argon2Hasher

from .faultproxy import FaultProxy
from .gateway import EdgeGateway
from .historian import Historian
from .simulators import SimulatedShop

ROOT = Path(__file__).resolve().parent.parent
logging.getLogger("amqtt").setLevel(logging.CRITICAL)
logging.getLogger("asyncua").setLevel(logging.CRITICAL)
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)


def load_config(path: str | Path | None = None) -> dict:
    with open(path or ROOT / "config" / "lab.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def broker_config(port: int, password_file: Path, root: str, users: dict) -> dict:
    """Broker : pas d'anonyme, mots de passe hachés (argon2), droits par topic (ACL)."""
    return {
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": False},
            "amqtt.plugins.authentication.FileAuthPlugin": {"password_file": str(password_file)},
            "amqtt.plugins.topic_checking.TopicAccessControlListPlugin": {
                # Seule la passerelle écrit dans l'UNS ; l'historien et les cas d'usage lisent.
                "publish_acl": {users["gateway"]: [f"{root}/#"], users["historian"]: ["lab/historien/#"],
                                users["usecase"]: ["lab/cas-usage/#"]},
                "acl": {users["gateway"]: ["lab/passerelle/commandes/#"], users["historian"]: [f"{root}/#"],
                        users["usecase"]: [f"{root}/#"]},
            },
        },
    }


class Lab:
    def __init__(self, cfg: dict | None = None, *, delivery: str | None = None, workdir: str | Path | None = None):
        self.cfg = copy.deepcopy(cfg or load_config())
        if delivery:
            self.cfg["gateway"]["delivery"] = delivery
        self._tmp = None
        if workdir is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="lab_")
            workdir = self._tmp.name
        self.dir = Path(workdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        # Mots de passe générés à chaque lancement : rien de secret dans le dépôt
        self.users = {"gateway": self.cfg["gateway"]["username"], "historian": self.cfg["historian"]["username"],
                      "usecase": "cas-usage"}
        self.passwords = {k: secrets.token_urlsafe(12) for k in self.users}
        self.ports = {k: free_port() for k in ("broker", "opcua", "modbus", "p_mqtt", "p_opcua", "p_modbus")}
        self.broker: Broker | None = None
        self.shop: SimulatedShop | None = None
        self.proxies: dict[str, FaultProxy] = {}
        self.gateway: EdgeGateway | None = None
        self.historian: Historian | None = None
        self.restarts: list[tuple[float, int]] = []  # (instant, dernier numéro de séquence avant redémarrage)

    @property
    def root(self) -> str:
        return self.cfg["uns"]["root"]

    def opcua_endpoint(self, port: int) -> str:
        return f"opc.tcp://127.0.0.1:{port}/lab/"

    async def start_broker(self) -> None:
        pw = self.dir / "passwords.txt"
        h = Argon2Hasher()
        pw.write_text("".join(f"{self.users[k]}:{h.hash(self.passwords[k])}\n" for k in self.users))
        self.broker = Broker(broker_config(self.ports["broker"], pw, self.root, self.users))
        await self.broker.start()

    def new_gateway(self) -> EdgeGateway:
        return EdgeGateway(self.cfg, broker=("127.0.0.1", self.ports["p_mqtt"]), password=self.passwords["gateway"],
                           opcua_endpoint=self.opcua_endpoint(self.ports["p_opcua"]),
                           modbus=("127.0.0.1", self.ports["p_modbus"]), buffer_path=self.dir / "gateway_buffer.sqlite")

    async def start(self) -> "Lab":
        await self.start_broker()
        self.shop = SimulatedShop(self.cfg, opcua_endpoint=self.opcua_endpoint(self.ports["opcua"]),
                                  modbus_port=self.ports["modbus"])
        await self.shop.start()
        for name, target in (("mqtt", "broker"), ("opcua", "opcua"), ("modbus", "modbus")):
            p = FaultProxy(self.ports[f"p_{name}"], "127.0.0.1", self.ports[target])
            await p.start()
            self.proxies[name] = p
        self.historian = Historian(self.cfg, broker=("127.0.0.1", self.ports["broker"]),
                                   password=self.passwords["historian"], db_path=self.dir / "historian.sqlite")
        await asyncio.to_thread(self.historian.start)
        self.gateway = self.new_gateway()
        await self.gateway.start()
        return self

    async def restart_gateway(self, graceful: bool = False) -> None:
        """Redémarrage brutal de la passerelle (coupure d'alimentation de l'edge)."""
        self.restarts.append((time.time(), self.gateway.seq))
        await self.gateway.stop(graceful=graceful)
        old = self.gateway
        self.gateway = self.new_gateway()
        self.gateway.metrics = old.metrics  # cumul des compteurs sur les deux vies
        self.gateway.buffer_series = old.buffer_series
        await self.gateway.start()

    async def stop(self) -> None:
        if self.gateway:
            await self.gateway.stop()
        if self.historian:
            await asyncio.to_thread(self.historian.stop)
        for p in self.proxies.values():
            await p.stop()
        if self.shop:
            await self.shop.stop()
        if self.broker:
            await self.broker.shutdown()

    def cleanup(self) -> None:
        if self._tmp:
            self._tmp.cleanup()

    async def __aenter__(self) -> "Lab":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()
