"""Passerelle edge : OPC UA et Modbus TCP vers l'UNS MQTT, avec qualité, horodatage et stockage local.

Trois modes de livraison, comparés dans les scénarios :
- "aucun"   : QoS 0, rien n'est gardé si le lien montant est coupé ;
- "memoire" : QoS 1, file d'attente en mémoire du client MQTT (perdue si la passerelle redémarre) ;
- "disque"  : QoS 1, tampon SQLite ; un message n'est effacé qu'après accusé de réception du broker.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections import deque
from datetime import timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from asyncua import Client as UaClient
from pymodbus.client import AsyncModbusTcpClient

from . import uns

log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


class Buffer:
    """Tampon SQLite : file FIFO des messages et compteur de séquence persistant."""

    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, "
                        "payload BLOB)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER)")
        self.db.commit()

    def get_seq(self) -> int:
        row = self.db.execute("SELECT v FROM meta WHERE k='seq'").fetchone()
        return row[0] if row else 0

    def set_seq(self, seq: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('seq', ?)", (seq,))

    def push(self, topic: str, payload: bytes) -> None:
        self.db.execute("INSERT INTO queue (topic, payload) VALUES (?, ?)", (topic, payload))

    def next_after(self, last_id: int, n: int) -> list[tuple[int, str, bytes]]:
        return self.db.execute("SELECT id, topic, payload FROM queue WHERE id > ? ORDER BY id LIMIT ?",
                               (last_id, n)).fetchall()

    def delete(self, ids: list[int]) -> None:
        self.db.executemany("DELETE FROM queue WHERE id = ?", [(i,) for i in ids])

    def depth(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()


class EdgeGateway:
    def __init__(self, cfg: dict, *, broker: tuple[str, int], password: str, opcua_endpoint: str,
                 modbus: tuple[str, int], buffer_path: str | Path, delivery: str | None = None):
        self.cfg = cfg
        g = cfg["gateway"]
        self.delivery = delivery or g["delivery"]
        if self.delivery not in ("aucun", "memoire", "disque"):
            raise ValueError(self.delivery)
        self.gw_id = g["client_id"]
        self.root = cfg["uns"]["root"]
        self.broker, self.password = broker, password
        self.opcua_endpoint, self.modbus_addr = opcua_endpoint, modbus
        self.buffer = Buffer(buffer_path)
        self.seq = self.buffer.get_seq()
        self.max_inflight = g.get("max_inflight", 200)
        self.deadband = g.get("deadband", {})
        self.bad_after = g.get("bad_after_failures", 2)
        self.health_period = g.get("health_period_s", 1.0)
        self.metrics = {"produits": 0, "jetes": 0, "acquittes": 0, "connexions_mqtt": 0, "deconnexions_mqtt": 0}
        self.buffer_series: list[tuple[float, int]] = []
        self.health = {"opcua": "INIT", "modbus": "INIT"}
        self._last_sent: dict[str, object] = {}
        self._connected = threading.Event()
        self._mid_to_row: dict[int, int] = {}
        self._acked_mids: deque[int] = deque()
        self._last_row_sent = 0
        self._resend_from_start = False
        self._tasks: list[asyncio.Task] = []
        self._mqtt: mqtt.Client | None = None

    # ------------------------------------------------------------------ MQTT
    def _make_mqtt(self) -> mqtt.Client:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.gw_id, clean_session=True)
        c.username_pw_set(self.cfg["gateway"]["username"], self.password)
        c.max_inflight_messages_set(self.max_inflight)
        c.max_queued_messages_set(0)
        c.reconnect_delay_set(0.2, 1.0)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_publish = self._on_publish
        return c

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self.metrics["connexions_mqtt"] += 1
            # Les messages non acquittés avant la coupure seront renvoyés depuis le tampon (au moins une fois)
            self._resend_from_start = True
            self._connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self._connected.is_set():
            self.metrics["deconnexions_mqtt"] += 1
        self._connected.clear()

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        # Thread du client MQTT : on ne fait que noter l'accusé (aucun verrou, pas d'inversion de verrous
        # avec le client) ; la correspondance avec le tampon est faite par la tâche d'envoi.
        self.metrics["acquittes"] += 1
        self._acked_mids.append(mid)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ------------------------------------------------------------------ émission
    def emit(self, cell: str, equipment: str, variable: str, value, ts_ms: int, quality: str, unit: str,
             src: str) -> None:
        t = uns.topic(self.root, cell, equipment, variable)
        db = self.deadband.get(variable)
        prev = self._last_sent.get(t)
        if quality == "GOOD" and prev is not None and prev[1] == "GOOD":
            if value == prev[0]:
                return  # publication sur changement uniquement
            if db and isinstance(value, (int, float)) and isinstance(prev[0], (int, float)) \
                    and abs(value - prev[0]) <= db:
                return  # variation dans la bande morte
        self._last_sent[t] = (value, quality)
        self.seq += 1
        self.metrics["produits"] += 1
        body = uns.payload(value if quality != "BAD" else None, ts_ms, quality, unit, src, self.gw_id, self.seq)
        if self.delivery == "aucun":
            if self.connected:
                self._mqtt.publish(t, body, qos=0)
            else:
                self.metrics["jetes"] += 1
        elif self.delivery == "memoire":
            self._mqtt.publish(t, body, qos=1)
        else:
            self.buffer.push(t, body)

    def _apply_acks(self) -> None:
        ids = []
        while self._acked_mids:
            row = self._mid_to_row.pop(self._acked_mids.popleft(), None)
            if row is not None:
                ids.append(row)
        if ids:
            self.buffer.delete(ids)

    async def _drain(self) -> None:
        """Mode disque : envoie le tampon dans l'ordre, efface les lignes acquittées."""
        while True:
            self._apply_acks()
            if self._resend_from_start:
                self._resend_from_start = False
                self._mid_to_row.clear()
                self._last_row_sent = 0
            if self.connected:
                room = self.max_inflight - len(self._mid_to_row)
                if room > 0:
                    for row_id, t, body in self.buffer.next_after(self._last_row_sent, room):
                        info = self._mqtt.publish(t, body, qos=1)
                        self._mid_to_row[info.mid] = row_id
                        self._last_row_sent = row_id
            self.buffer.set_seq(self.seq)
            self.buffer.commit()
            await asyncio.sleep(0.02)

    async def _sample(self) -> None:
        while True:
            self.buffer_series.append((time.time(), self.buffer_depth()))
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------ OPC UA
    async def _opcua_loop(self) -> None:
        o = self.cfg["sources"]["opcua"]
        mapping = {}
        for eq in o["equipments"]:
            for node_name, tag in eq["tags"].items():
                mapping[f"{eq['id']}.{node_name}"] = (eq["cell"], eq["id"], tag["name"], tag["unit"])
        gw = self

        class Handler:
            def status_change_notification(self, status):
                pass  # la perte de lien est traitée par la boucle de surveillance ci-dessous

            def datachange_notification(self, node, val, data):
                ident = node.nodeid.Identifier
                cell, eq, name, unit = mapping[ident]
                dv = data.monitored_item.Value
                st = dv.SourceTimestamp
                ts = int(st.replace(tzinfo=timezone.utc).timestamp() * 1000) if st else now_ms()
                quality = "GOOD" if dv.StatusCode is None or dv.StatusCode.is_good() else "BAD"
                gw.emit(cell, eq, name, val, ts, quality, unit, f"opcua:ns={node.nodeid.NamespaceIndex};s={ident}")

        while True:
            client = UaClient(self.opcua_endpoint, timeout=2)
            try:
                await client.connect()
                idx = await client.get_namespace_index(o["namespace"])
                nodes = [client.get_node(f"ns={idx};s={ident}") for ident in mapping]
                sub = await client.create_subscription(o.get("publishing_interval_ms", 100), Handler())
                await sub.subscribe_data_change(nodes)
                self._set_health("opcua", "UP")
                while True:
                    await asyncio.sleep(1.0)
                    await asyncio.wait_for(client.check_connection(), 2.0)
                    await asyncio.wait_for(client.get_node("i=2259").read_value(), 2.0)  # état du serveur
            except asyncio.CancelledError:
                try:
                    await asyncio.wait_for(client.disconnect(), 1.0)
                except Exception:
                    pass
                raise
            except Exception as exc:  # perte de lien, serveur arrêté, délai dépassé
                log.info("OPC UA indisponible : %s", exc)
                if self.health["opcua"] != "DOWN":
                    self._set_health("opcua", "DOWN")
                    for ident, (cell, eq, name, unit) in mapping.items():
                        self.emit(cell, eq, name, None, now_ms(), "BAD", unit, f"opcua:s={ident}")
                try:
                    await asyncio.wait_for(client.disconnect(), 1.0)
                except Exception:
                    pass
                await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ Modbus
    async def _modbus_loop(self) -> None:
        m = self.cfg["sources"]["modbus"]
        regs = m["registers"]
        count = max(r["address"] for r in regs) + 1
        host, port = self.modbus_addr
        client = AsyncModbusTcpClient(host, port=port, timeout=m.get("timeout_s", 1.0), retries=0,
                                      reconnect_delay=0, reconnect_delay_max=0)
        failures, last_hb, hb_same = 0, None, 0
        try:
            while True:
                t_poll = now_ms()
                try:
                    if not client.connected:
                        await asyncio.wait_for(client.connect(), m.get("timeout_s", 1.0))
                        if not client.connected:
                            raise ConnectionError("connexion Modbus refusée")
                    rr = await asyncio.wait_for(
                        client.read_holding_registers(0, count=count, device_id=m["device_id"]),
                        m.get("timeout_s", 1.0) + 0.2)
                    if rr.isError():
                        raise ConnectionError(str(rr))
                    values = rr.registers
                    failures = 0
                    if self.health["modbus"] != "UP":
                        self._set_health("modbus", "UP")
                    for r in regs:
                        raw = values[r["address"]]
                        if r["name"] == "battement":
                            hb_same = hb_same + 1 if raw == last_hb else 0
                            last_hb = raw
                            continue
                        val = round(raw * r["scale"], 3) if r["scale"] != 1 else raw
                        if "enum" in r:
                            val = r["enum"][raw] if raw < len(r["enum"]) else f"inconnu-{raw}"
                        # Battement figé depuis 4 lectures : automate bloqué, valeurs douteuses
                        q = "UNCERTAIN" if hb_same >= 4 else "GOOD"
                        self.emit(m["cell"], m["equipment"], r["name"], val, t_poll, q, r["unit"],
                                  f"modbus:{m['device_id']}/hr{r['address']}")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    client.close()
                    if failures == self.bad_after:
                        log.info("Modbus indisponible : %s", exc)
                        self._set_health("modbus", "DOWN")
                        for r in regs:
                            if r["name"] != "battement":
                                self.emit(m["cell"], m["equipment"], r["name"], None, now_ms(), "BAD",
                                          r["unit"], f"modbus:{m['device_id']}/hr{r['address']}")
                await asyncio.sleep(m.get("poll_s", 0.5))
        finally:
            client.close()

    # ------------------------------------------------------------------ santé
    def _health_targets(self, source: str):
        if source == "modbus":
            m = self.cfg["sources"]["modbus"]
            return [(m["cell"], m["equipment"])]
        return [(eq["cell"], eq["id"]) for eq in self.cfg["sources"]["opcua"]["equipments"]]

    def _set_health(self, source: str, state: str) -> None:
        self.health[source] = state
        for cell, eq in self._health_targets(source):
            self._last_sent.pop(uns.topic(self.root, cell, eq, "_sante"), None)
            self.emit(cell, eq, "_sante", state, now_ms(), "GOOD", "", f"gateway:{source}")

    async def _health_loop(self) -> None:
        # Message de vie de la passerelle, publié même sans changement (retain côté broker non utilisé)
        while True:
            await asyncio.sleep(self.health_period)
            for source, state in self.health.items():
                for cell, eq in self._health_targets(source):
                    t = uns.topic(self.root, cell, eq, "_sante")
                    self._last_sent.pop(t, None)
                    self.emit(cell, eq, "_sante", state, now_ms(), "GOOD", "", f"gateway:{source}")

    # ------------------------------------------------------------------ cycle de vie
    async def start(self) -> None:
        self._mqtt = self._make_mqtt()
        self._mqtt.connect_async(*self.broker, keepalive=self.cfg["broker"].get("keepalive_s", 5))
        self._mqtt.loop_start()
        for _ in range(50):
            if self.connected:
                break
            await asyncio.sleep(0.05)
        self._tasks = [asyncio.create_task(c) for c in (self._opcua_loop(), self._modbus_loop(),
                                                         self._health_loop(), self._sample())]
        if self.delivery == "disque":
            self._tasks.append(asyncio.create_task(self._drain()))

    def buffer_depth(self) -> int:
        if self.delivery == "disque":
            return self.buffer.depth()
        return len(self._mqtt._out_messages) if self._mqtt else 0

    async def wait_drained(self, timeout_s: float = 20.0) -> float | None:
        """Attend que tout soit acquitté ; renvoie la durée d'attente ou None si le délai est dépassé."""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self.buffer_depth() == 0 and self.connected:
                return time.time() - t0
            await asyncio.sleep(0.05)
        return None

    async def stop(self, graceful: bool = True) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._apply_acks()
        self.buffer.set_seq(self.seq)
        self.buffer.close()
        if self._mqtt:
            if graceful:
                self._mqtt.disconnect()
            self._mqtt.loop_stop()
