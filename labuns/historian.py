"""Historien : abonné à l'UNS, il valide chaque message, élimine les doublons et l'enregistre (SQLite).

Une API REST en lecture seule (FastAPI) expose les dernières valeurs, l'historique et la qualité des flux.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

from . import uns

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    gw TEXT, seq INTEGER, topic TEXT, v TEXT, ts INTEGER, q TEXT, u TEXT, src TEXT, recv INTEGER,
    PRIMARY KEY (gw, seq));
CREATE INDEX IF NOT EXISTS idx_topic_ts ON samples (topic, ts);
CREATE TABLE IF NOT EXISTS stats (k TEXT PRIMARY KEY, n INTEGER);
"""


def open_db(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


class Historian:
    def __init__(self, cfg: dict, *, broker: tuple[str, int], password: str, db_path: str | Path):
        h = cfg["historian"]
        self.root = cfg["uns"]["root"]
        self.db_path = db_path
        self.db = open_db(db_path)
        self.lock = threading.Lock()
        self.counts = {"recus": 0, "doublons": 0, "invalides": 0}
        self.ready = threading.Event()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=h["client_id"], clean_session=True)
        self.c.username_pw_set(h["username"], password)
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message
        self.c.on_subscribe = lambda *a: self.ready.set()
        self.broker = broker
        self._last_commit = time.time()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(self.root + "/#", qos=1)

    def _on_message(self, client, userdata, msg):
        recv = int(time.time() * 1000)
        with self.lock:
            self.counts["recus"] += 1
            try:
                m = uns.validate(msg.payload)
            except ValueError:
                self.counts["invalides"] += 1
                return
            cur = self.db.execute("INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?,?,?)",
                                  (m["gw"], m["seq"], msg.topic, json.dumps(m["v"], ensure_ascii=False), m["ts"],
                                   m["q"], m["u"], m["src"], recv))
            if cur.rowcount == 0:
                self.counts["doublons"] += 1
            if time.time() - self._last_commit > 0.2:
                self.flush()

    def flush(self) -> None:
        for k, n in self.counts.items():
            self.db.execute("INSERT OR REPLACE INTO stats VALUES (?, ?)", (k, n))
        self.db.commit()
        self._last_commit = time.time()

    def start(self, timeout_s: float = 5.0) -> None:
        self.c.connect(*self.broker, keepalive=10)
        self.c.loop_start()
        if not self.ready.wait(timeout_s):
            raise TimeoutError("l'historien n'a pas pu s'abonner à l'UNS")

    def stop(self) -> None:
        self.c.disconnect()
        self.c.loop_stop()
        with self.lock:
            self.flush()
            self.db.close()


# ---------------------------------------------------------------------- lecture
def load_samples(db_path: str | Path, topic_like: str = "%") -> list[dict]:
    db = open_db(db_path)
    rows = db.execute("SELECT gw, seq, topic, v, ts, q, u, src, recv FROM samples WHERE topic LIKE ? "
                      "ORDER BY ts, seq", (topic_like,)).fetchall()
    db.close()
    keys = ("gw", "seq", "topic", "v", "ts", "q", "u", "src", "recv")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["v"] = json.loads(d["v"])
        out.append(d)
    return out


def create_app(db_path: str | Path):
    """API REST en lecture seule sur l'historien."""
    from fastapi import FastAPI, HTTPException, Query

    app = FastAPI(title="LAB UNS, historien", version="1.0")

    def q(sql, args=()):
        db = open_db(db_path)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    @app.get("/sante")
    def sante():
        n = q("SELECT COUNT(*), MAX(recv) FROM samples")[0]
        return {"messages": n[0], "dernier_message_ms": n[1]}

    @app.get("/variables")
    def variables():
        rows = q("SELECT topic, v, ts, q, u FROM samples s WHERE seq = (SELECT MAX(seq) FROM samples t "
                 "WHERE t.topic = s.topic AND t.gw = s.gw) ORDER BY topic")
        return [{"topic": t, "v": json.loads(v), "ts": ts, "q": qq, "u": u} for t, v, ts, qq, u in rows]

    @app.get("/historique/{topic:path}")
    def historique(topic: str, depuis_ms: int = 0, limite: int = Query(1000, le=10000)):
        rows = q("SELECT v, ts, q FROM samples WHERE topic = ? AND ts >= ? ORDER BY ts LIMIT ?",
                 (topic, depuis_ms, limite))
        if not rows:
            raise HTTPException(404, "variable inconnue ou sans données")
        return [{"v": json.loads(v), "ts": ts, "q": qq} for v, ts, qq in rows]

    @app.get("/qualite")
    def qualite():
        stats = dict(q("SELECT k, n FROM stats"))
        by_q = dict(q("SELECT q, COUNT(*) FROM samples GROUP BY q"))
        return {"compteurs": stats, "par_qualite": by_q}

    return app
