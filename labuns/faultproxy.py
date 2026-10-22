"""Proxy TCP d'injection de défauts réseau, placé entre un client et un serveur.

Modes :
- normal : relais transparent, avec un retard optionnel (latence fixe + gigue) ;
- coupure : toutes les connexions sont fermées et les nouvelles refusées (câble débranché, serveur arrêté) ;
- trou noir : les connexions restent ouvertes mais plus rien ne passe (lien radio perdu, pare-feu qui jette).
"""
from __future__ import annotations

import asyncio
import random


class FaultProxy:
    def __init__(self, listen_port: int, target_host: str, target_port: int, host: str = "127.0.0.1",
                 seed: int = 0):
        self.host, self.listen_port = host, listen_port
        self.target_host, self.target_port = target_host, target_port
        self.mode = "normal"
        self.delay_s, self.jitter_s = 0.0, 0.0
        self._rng = random.Random(seed)
        self._server: asyncio.base_events.Server | None = None
        self._conns: set[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = set()
        self._tasks: set[asyncio.Task] = set()
        self.bytes_relayed = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.listen_port)

    async def stop(self) -> None:
        self.cut()
        self.mode = "normal"
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for t in list(self._tasks):
            t.cancel()

    # --- commandes de défaut ---
    def set_latency(self, delay_s: float, jitter_s: float = 0.0) -> None:
        self.delay_s, self.jitter_s = delay_s, jitter_s

    def cut(self) -> None:
        self.mode = "coupure"
        for a, b in list(self._conns):
            for w in (a, b):
                w.close()
        self._conns.clear()

    def blackhole(self) -> None:
        self.mode = "trou_noir"

    def restore(self) -> None:
        self.mode = "normal"

    # --- relais ---
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.mode == "coupure":
            writer.close()
            return
        try:
            t_reader, t_writer = await asyncio.open_connection(self.target_host, self.target_port)
        except OSError:
            writer.close()
            return
        pair = (writer, t_writer)
        self._conns.add(pair)
        for src, dst in ((reader, t_writer), (t_reader, writer)):
            task = asyncio.create_task(self._pipe(src, dst, pair))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _pipe(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, pair) -> None:
        # Le retard est appliqué dans l'ordre d'arrivée : une file par sens garde l'ordre des octets.
        queue: asyncio.Queue = asyncio.Queue()

        async def sender():
            while True:
                due, data = await queue.get()
                wait = due - asyncio.get_running_loop().time()
                if wait > 0:
                    await asyncio.sleep(wait)
                if self.mode == "normal":
                    dst.write(data)
                    await dst.drain()
                    self.bytes_relayed += len(data)

        send_task = asyncio.create_task(sender())
        last_due = 0.0
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                if self.mode != "normal":
                    continue  # trou noir : les octets sont perdus
                now = asyncio.get_running_loop().time()
                delay = self.delay_s + (self._rng.uniform(0, self.jitter_s) if self.jitter_s else 0.0)
                last_due = max(last_due, now + delay)
                await queue.put((last_due, data))
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            await asyncio.sleep(0)
            send_task.cancel()
            for w in pair:
                w.close()
            self._conns.discard(pair)
