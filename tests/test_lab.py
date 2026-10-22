"""Tests d'intégration : le LAB complet tourne dans le processus de test (temps raccourcis)."""
import asyncio

from fastapi.testclient import TestClient

from labuns.historian import create_app
from labuns.lab import Lab
from labuns.scenarios import all_scenarios, run_one, run_scenario
from labuns.security import run_security

SC = {s["id"]: s for s in all_scenarios()}


def test_all_scenarios_load():
    assert set(SC) == {"S1", "S2", "S3", "S4", "S5", "S6", "S7"}


def test_nominal_flow_and_usecase():
    r = asyncio.run(run_one(SC["S1"], "disque", scale=0.12))
    m = r["indicateurs"]
    assert m["perdus"] == 0 and m["invalides"] == 0 and m["produits"] > 50
    assert m["latence_p95_ms"] < 500
    assert m["ecart_pieces_max"] == 0
    assert m["ecart_dispo_max_points"] < 1.0


def test_uplink_outage_loses_data_without_buffer_but_not_with_disk_buffer():
    res = {r["mode"]: r["indicateurs"] for r in asyncio.run(run_scenario(SC["S2"], scale=0.25,
                                                                          modes=["aucun", "disque"]))}
    assert res["aucun"]["perdus"] > 0
    assert res["disque"]["perdus"] == 0 and res["disque"]["tampon_max"] > 0


def test_disk_buffer_survives_gateway_restart():
    res = {r["mode"]: r["indicateurs"] for r in asyncio.run(run_scenario(SC["S3"], scale=0.3))}
    assert res["memoire"]["perdus"] > 0
    assert res["disque"]["perdus"] == 0


def test_modbus_loss_is_detected_and_recovered():
    r = asyncio.run(run_one(SC["S6"], "disque", scale=0.4))
    m = r["indicateurs"]
    assert m["valeurs_bad_publiees"] >= 4 and m["detection_s"] < 3 and m["retablissement_s"] < 3


def test_opcua_loss_is_detected_and_recovered():
    r = asyncio.run(run_one(SC["S7"], "disque", scale=0.4))
    m = r["indicateurs"]
    assert m["detection_s"] < 5 and m["retablissement_s"] < 10 and m["perdus"] == 0


def test_security_controls():
    r = asyncio.run(run_security())
    assert r["ok"], [c for c in r["controles"] if not c["ok"]]


def test_historian_api(tmp_path):
    async def main():
        lab = Lab(workdir=tmp_path)
        await lab.start()
        await asyncio.sleep(3)
        await lab.gateway.wait_drained()
        await asyncio.sleep(0.5)
        await lab.stop()
        return lab
    lab = asyncio.run(main())
    c = TestClient(create_app(tmp_path / "historian.sqlite"))
    assert c.get("/sante").json()["messages"] > 10
    variables = {v["topic"]: v for v in c.get("/variables").json()}
    t = f"{lab.root}/traitement-thermique/FOUR-01/temperature"
    assert variables[t]["u"] == "degC" and variables[t]["q"] == "GOOD"
    hist = c.get(f"/historique/{t}").json()
    assert len(hist) >= 3 and all(h["q"] == "GOOD" for h in hist)
    assert c.get(f"/historique/{lab.root}/inconnu").status_code == 404
    assert c.get("/qualite").json()["compteurs"]["invalides"] == 0
