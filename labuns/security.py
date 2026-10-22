"""Scénario S8 : contrôles de sécurité du conduit OT vers IT (inspirés de l'IEC 62443, niveau de base).

Chaque contrôle est une attaque ou une erreur simple jouée contre le LAB en fonctionnement.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import paho.mqtt.client as mqtt

from .lab import Lab


def _client(lab: Lab, user: str | None, password: str | None, cid: str):
    res = {"rc": None, "msgs": [], "suback": None}
    ev = threading.Event()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    if user:
        c.username_pw_set(user, password)

    def on_connect(cl, ud, flags, rc, props):
        res["rc"] = rc.value if hasattr(rc, "value") else int(rc)
        ev.set()

    def on_subscribe(cl, ud, mid, rcs, props):
        res["suback"] = [r.value if hasattr(r, "value") else int(r) for r in rcs]

    c.on_connect = on_connect
    c.on_subscribe = on_subscribe
    c.on_message = lambda cl, ud, m: res["msgs"].append((m.topic, m.payload))
    c.connect("127.0.0.1", lab.ports["broker"], keepalive=10)
    c.loop_start()
    ev.wait(3)
    return c, res


def _checks(lab: Lab) -> list[dict]:
    out = []
    root, P = lab.root, lab.passwords
    target = f"{root}/cellule-5axes/CU-01/charge-broche"

    # 1. Connexion anonyme refusée
    c, r = _client(lab, None, None, "intrus-anonyme")
    out.append({"controle": "Connexion anonyme au broker refusée", "ok": r["rc"] not in (None, 0),
                "detail": f"code retour {r['rc']}"})
    c.loop_stop()
    c.disconnect()

    # 2. Mauvais mot de passe refusé
    c, r = _client(lab, lab.users["gateway"], "mauvais-mot-de-passe", "intrus-mdp")
    out.append({"controle": "Mot de passe erroné refusé", "ok": r["rc"] not in (None, 0),
                "detail": f"code retour {r['rc']}"})
    c.loop_stop()
    c.disconnect()

    # 3. Un compte lecteur (cas d'usage) ne peut pas injecter une fausse mesure dans l'UNS
    spy, rs = _client(lab, lab.users["usecase"], P["usecase"], "observateur")
    spy.subscribe(target, qos=1)
    time.sleep(0.4)
    att, ra = _client(lab, lab.users["usecase"], P["usecase"], "cas-usage-compromis")
    fake = json.dumps({"v": 999.0, "ts": int(time.time() * 1000), "q": "GOOD", "u": "%", "src": "faux",
                       "gw": "edge-gw-01", "seq": 10**9}).encode()
    att.publish(target, fake, qos=1)
    time.sleep(0.8)
    injected = any(p == fake for _, p in rs["msgs"])
    out.append({"controle": "Écriture dans l'UNS interdite aux comptes lecteurs (ACL)", "ok": not injected,
                "detail": f"{len(rs['msgs'])} messages légitimes reçus, injection reçue : {injected}"})
    for c in (spy, att):
        c.loop_stop()
        c.disconnect()

    # 4. La passerelle ne peut pas s'abonner à l'UNS (moindre privilège : elle écrit, elle ne lit pas)
    c, r = _client(lab, lab.users["gateway"], P["gateway"], "passerelle-curieuse")
    c.subscribe(f"{root}/#", qos=1)
    time.sleep(0.6)
    refused = (r["suback"] and r["suback"][0] >= 0x80) or not r["msgs"]
    out.append({"controle": "Abonnement à l'UNS refusé au compte passerelle", "ok": bool(refused),
                "detail": f"SUBACK {r['suback']}, {len(r['msgs'])} messages reçus"})
    c.loop_stop()
    c.disconnect()

    # 5. Un message hors contrat (JSON invalide, champ manquant) est rejeté par l'historien
    before = lab.historian.counts["invalides"]
    c, r = _client(lab, lab.users["gateway"], P["gateway"], "passerelle-defectueuse")
    c.publish(target, b"{pas du json", qos=1)
    c.publish(target, json.dumps({"v": 1}).encode(), qos=1)
    time.sleep(0.8)
    c.loop_stop()
    c.disconnect()
    rejected = lab.historian.counts["invalides"] - before
    out.append({"controle": "Messages hors contrat rejetés par l'historien", "ok": rejected == 2,
                "detail": f"{rejected} messages rejetés sur 2"})

    # 6. Aucun mot de passe en clair : le broker ne stocke que des empreintes argon2
    lines = (lab.dir / "passwords.txt").read_text().splitlines()
    ok = all(line.split(":", 1)[1].startswith("$argon2") for line in lines) and \
        not any(p in "\n".join(lines) for p in P.values())
    out.append({"controle": "Mots de passe stockés hachés (argon2), jamais en clair", "ok": ok,
                "detail": f"{len(lines)} comptes"})
    return out


async def run_security(cfg: dict | None = None) -> dict:
    lab = Lab(cfg)
    await lab.start()
    try:
        await asyncio.sleep(1.0)
        checks = await asyncio.to_thread(_checks, lab)
    finally:
        await lab.stop()
        lab.cleanup()
    return {"id": "S8", "titre": "Contrôles de sécurité du conduit OT vers IT", "controles": checks,
            "ok": all(c["ok"] for c in checks)}
