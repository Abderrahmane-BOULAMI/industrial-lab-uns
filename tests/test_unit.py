import asyncio
import copy
import json
import random

import pytest

from labuns import uns
from labuns.config import validate_config
from labuns.faultproxy import FaultProxy
from labuns.lab import ROOT, free_port, load_config
from labuns.maturity import current_level, evaluate
from labuns.plant import Furnace, Plant, availability_from_transitions


def test_topic_isa95_and_rejects_bad_segments():
    t = uns.topic("entreprise/site/zone/ligne", "cellule", "CU-01", "charge-broche")
    assert t == "entreprise/site/zone/ligne/cellule/CU-01/charge-broche"
    for bad in ("a b", "a/b", "+", "#", ""):
        with pytest.raises(ValueError):
            uns.topic("r", "c", bad, "v")


def test_payload_contract():
    raw = uns.payload(54.2, 1_700_000_000_000, "GOOD", "%", "opcua:x", "gw", 3)
    m = uns.validate(raw)
    assert m["v"] == 54.2 and m["seq"] == 3
    with pytest.raises(ValueError):
        uns.validate(b"{pas du json")
    with pytest.raises(ValueError):
        uns.validate(json.dumps({"v": 1}))
    bad = json.loads(raw)
    bad["q"] = "BAD"
    with pytest.raises(ValueError):  # une valeur BAD doit être nulle
        uns.validate(json.dumps(bad))


def test_availability_from_transitions():
    tr = [(0, "PRODUCTION"), (10, "DEFAUT"), (15, "PRODUCTION"), (30, "ATTENTE")]
    assert availability_from_transitions(tr, 0, 40) == pytest.approx(25 / 40)
    assert availability_from_transitions(tr, 12, 20) == pytest.approx(5 / 8)


def test_plant_counts_parts_and_logs_transitions():
    p = Plant(load_config())
    for i in range(3000):
        p.step(0.2, now=i * 0.2)
    m = p.machines["CU-01"]
    assert m.part_count > 0 and len(m.transitions) > 2
    assert 0.3 < m.availability() < 1.0
    assert m.availability(0, 600) == pytest.approx(m.availability(), abs=0.01)


def test_furnace_goes_through_a_full_cycle():
    f = Furnace("F", 850, 15, 10, 20)
    rng, phases = random.Random(1), set()
    for _ in range(3000):
        f.step(0.2, rng)
        phases.add(f.phase)
    assert phases == {0, 1, 2, 3}
    regs = f.registers()
    assert regs[1] == 8500 and len(regs) == 5


def test_default_config_is_valid_and_errors_are_caught():
    cfg = load_config()
    assert validate_config(cfg) == []
    bad = copy.deepcopy(cfg)
    bad["sources"]["modbus"]["registers"][1]["address"] = 0
    bad["sources"]["modbus"]["registers"] = [r for r in bad["sources"]["modbus"]["registers"]
                                             if r["name"] != "battement"]
    bad["sources"]["opcua"]["endpoint"] = "http://x"
    bad["gateway"]["delivery"] = "rapide"
    errs = "\n".join(validate_config(bad))
    assert "en double" in errs and "battement" in errs and "OPC UA" in errs and "delivery" in errs


def test_fault_proxy_relay_latency_cut_and_blackhole():
    async def main():
        async def echo(r, w):
            while data := await r.read(100):
                w.write(data)
                await w.drain()
        srv = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        px = FaultProxy(free_port(), "127.0.0.1", port)
        await px.start()
        loop = asyncio.get_running_loop()
        r, w = await asyncio.open_connection("127.0.0.1", px.listen_port)
        w.write(b"ping")
        assert await asyncio.wait_for(r.read(4), 1) == b"ping"
        px.set_latency(0.2)
        t0 = loop.time()
        w.write(b"lent")
        assert await asyncio.wait_for(r.read(4), 2) == b"lent"
        assert loop.time() - t0 >= 0.35  # retard appliqué dans les deux sens
        px.set_latency(0)
        px.blackhole()
        w.write(b"perdu")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(r.read(5), 0.5)
        px.cut()
        assert await asyncio.wait_for(r.read(5), 1) == b""  # connexion fermée
        r2, w2 = await asyncio.open_connection("127.0.0.1", px.listen_port)
        assert await asyncio.wait_for(r2.read(5), 1) == b""  # nouvelles connexions refusées
        px.restore()
        r3, w3 = await asyncio.open_connection("127.0.0.1", px.listen_port)
        w3.write(b"retour")
        assert await asyncio.wait_for(r3.read(6), 1) == b"retour"
        w3.close()
        await px.stop()
        srv.close()
    asyncio.run(main())


def test_maturity_grid_stops_at_first_failed_level():
    ok = {k: True for k in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "API", "CFG")}
    grid = evaluate(ok, ROOT)
    assert current_level(grid) == 4  # le niveau 5 exige des essais réels
    ok["S4"] = False
    assert current_level(evaluate(ok, ROOT)) == 1
