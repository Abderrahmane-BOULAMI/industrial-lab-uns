"""Équipements simulés : un serveur OPC UA (centres d'usinage) et un automate Modbus TCP (four).

Les deux serveurs lisent le même modèle d'atelier (plant.Plant), avancé par une boucle asyncio.
"""
from __future__ import annotations

import asyncio
import logging
import time

from asyncua import Server, ua
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from .plant import Plant

log = logging.getLogger(__name__)

OPCUA_TYPES = {"State": ua.VariantType.String, "SpindleLoad": ua.VariantType.Double,
               "PartCount": ua.VariantType.UInt32, "Program": ua.VariantType.String,
               "Alarm": ua.VariantType.String}


def _machine_values(m) -> dict:
    return {"State": m.state, "SpindleLoad": round(m.spindle_load, 2), "PartCount": m.part_count,
            "Program": m.program, "Alarm": m.alarm}


class OpcUaSimulator:
    """Serveur OPC UA : un objet par machine, une variable par grandeur (NodeId lisibles ns=2;s=CU-01.State)."""

    def __init__(self, plant: Plant, endpoint: str, namespace: str):
        self.plant, self.endpoint, self.namespace = plant, endpoint, namespace
        self.server: Server | None = None
        self.nodes: dict[tuple[str, str], object] = {}
        self._last: dict[tuple[str, str], object] = {}

    async def start(self) -> None:
        self.server = Server()
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("LAB simulateur de cellule d'usinage")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        idx = await self.server.register_namespace(self.namespace)
        objects = self.server.nodes.objects
        for mid, m in self.plant.machines.items():
            obj = await objects.add_object(ua.NodeId(mid, idx), mid)
            for name, value in _machine_values(m).items():
                node = await obj.add_variable(ua.NodeId(f"{mid}.{name}", idx), name,
                                              ua.Variant(value, OPCUA_TYPES[name]))
                self.nodes[(mid, name)] = node
                self._last[(mid, name)] = value
        await self.server.start()

    async def publish_values(self) -> None:
        """Écrit dans l'espace d'adressage les valeurs qui ont changé (horodatage source = maintenant)."""
        for mid, m in self.plant.machines.items():
            for name, value in _machine_values(m).items():
                if self._last[(mid, name)] != value:
                    self._last[(mid, name)] = value
                    await self.nodes[(mid, name)].write_value(ua.Variant(value, OPCUA_TYPES[name]))

    async def stop(self) -> None:
        if self.server:
            await self.server.stop()


class ModbusSimulator:
    """Automate du four : registres de maintien 0 à 4 (température x10, consigne x10, phase, lot, battement)."""

    def __init__(self, plant: Plant, host: str, port: int, device_id: int = 1):
        self.plant, self.host, self.port, self.device_id = plant, host, port, device_id
        self.device = SimDevice(device_id, simdata=[SimData(0, count=16, values=0, datatype=DataType.REGISTERS)])
        self.server: ModbusTcpServer | None = None

    async def start(self) -> None:
        self.server = ModbusTcpServer(self.device, address=(self.host, self.port))
        await self.server.serve_forever(background=True)
        await self.update_registers()

    async def update_registers(self) -> None:
        await self.server.async_setValues(self.device_id, 3, 0, self.plant.furnace.registers())

    async def stop(self) -> None:
        if self.server:
            await self.server.shutdown()


class SimulatedShop:
    """Atelier complet : modèle + serveurs OPC UA et Modbus, avancés toutes les tick_s secondes."""

    def __init__(self, cfg: dict, opcua_endpoint: str | None = None, modbus_port: int | None = None):
        src = cfg["sources"]
        self.plant = Plant(cfg)
        self.opcua = OpcUaSimulator(self.plant, opcua_endpoint or src["opcua"]["endpoint"],
                                    src["opcua"]["namespace"])
        mb = src["modbus"]
        self.modbus = ModbusSimulator(self.plant, mb["host"], modbus_port or mb["port"], mb["device_id"])
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.opcua.start()
        await self.modbus.start()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        tick = self.plant.tick_s
        while True:
            self.plant.step(tick, now=time.time())
            await self.modbus.update_registers()
            try:
                await self.opcua.publish_values()
            except Exception:  # pragma: no cover - ne doit pas arrêter la simulation
                log.exception("écriture OPC UA")
            await asyncio.sleep(tick)

    async def freeze(self) -> None:
        """Arrête l'évolution du modèle (les serveurs restent joignables) : la vérité terrain est figée."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.modbus.stop()
        await self.opcua.stop()
