"""Exécution des scénarios de test du LAB et calcul des indicateurs de validation.

Un scénario (fichier YAML de scenarios/) décrit : la durée, les modes de livraison à comparer, une suite
d'événements datés (coupure, trou noir, latence, redémarrage...) et des critères d'acceptation.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import yaml

from .historian import load_samples
from .lab import ROOT, Lab, load_config
from .usecases import oee_check

FLOW_TAGS = ("/charge-broche", "/temperature")
ACTIONS = ("coupure", "trou_noir", "retablir", "latence", "redemarrer_passerelle")


def load_scenario(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        sc = yaml.safe_load(f)
    for ev in sc.get("evenements", []):
        if ev["action"] not in ACTIONS:
            raise ValueError(f"action inconnue : {ev['action']}")
    return sc


def all_scenarios() -> list[dict]:
    return [load_scenario(p) for p in sorted((ROOT / "scenarios").glob("S*.yaml"))]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = min(len(values) - 1, max(0, int(round(q / 100 * (len(values) - 1)))))
    return values[k]


async def _apply(lab: Lab, ev: dict) -> None:
    link = ev.get("lien", "mqtt")
    proxy = lab.proxies.get(link)
    if ev["action"] == "coupure":
        proxy.cut()
    elif ev["action"] == "trou_noir":
        proxy.blackhole()
    elif ev["action"] == "retablir":
        proxy.restore()
        proxy.set_latency(0, 0)
    elif ev["action"] == "latence":
        proxy.set_latency(ev["retard_ms"] / 1000, ev.get("gigue_ms", 0) / 1000)
    elif ev["action"] == "redemarrer_passerelle":
        await lab.restart_gateway(graceful=False)


def compute_metrics(lab: Lab, samples: list[dict], t_start: float, events: list[tuple[float, dict]],
                    drain_s: float | None) -> dict:
    gw = lab.gateway
    produced = gw.metrics["produits"]
    unique = len(samples)
    lost = max(0, produced - unique)
    m = {"produits": produced, "recus_uniques": unique, "perdus": lost,
         "perte_pct": round(100 * lost / produced, 2) if produced else None,
         "doublons": lab.historian.counts["doublons"], "invalides": lab.historian.counts["invalides"],
         "jetes_passerelle": gw.metrics["jetes"], "deconnexions_mqtt": gw.metrics["deconnexions_mqtt"],
         "tampon_max": max((d for _, d in gw.buffer_series), default=0),
         "vidage_final_s": None if drain_s is None else round(drain_s, 2)}

    # Fenêtres de défaut (du premier défaut au rétablissement) : la latence nominale est mesurée hors fenêtres
    windows, open_t = [], None
    for t, ev in events:
        if ev["action"] in ("coupure", "trou_noir", "latence", "redemarrer_passerelle") and open_t is None:
            open_t = t
        if ev["action"] == "retablir" and open_t is not None:
            windows.append((open_t, t))
            open_t = None
    if open_t is not None:
        windows.append((open_t, float("inf")))

    def in_window(ts_s):
        return any(a - 0.5 <= ts_s <= b + 0.5 for a, b in windows)

    # La première valeur de chaque variable est l'état initial lu à l'abonnement (horodatage ancien) : exclue
    seen, good = set(), []
    for s in sorted(samples, key=lambda x: x["ts"]):
        if not s["topic"].endswith(FLOW_TAGS):
            continue  # latence mesurée sur les variables qui changent à chaque cycle
        # Après un redémarrage, la nouvelle passerelle relit l'état initial (horodatage antérieur) : exclu aussi
        snapshot = any(s["seq"] > sq and s["ts"] / 1000 < tr for tr, sq in lab.restarts)
        if s["topic"] in seen and not snapshot:
            good.append(s)
        seen.add(s["topic"])
    lat_all = [s["recv"] - s["ts"] for s in good]  # variables à flux continu, hors état initial
    lat_nom = [s["recv"] - s["ts"] for s in good if not in_window(s["ts"] / 1000)]
    m["latence_p50_ms"] = _pct(lat_nom, 50)
    m["latence_p95_ms"] = _pct(lat_nom, 95)
    m["latence_max_ms"] = max(lat_all) if lat_all else None  # inclut les messages mis en attente

    # Rattrapage : dernier message produit pendant la fenêtre et reçu après le rétablissement
    catchups = []
    for a, b in windows:
        if b == float("inf"):
            continue
        recv = [s["recv"] / 1000 for s in samples if a <= s["ts"] / 1000 <= b]
        if recv:
            catchups.append(max(recv) - b)
    m["rattrapage_s"] = round(max(catchups), 2) if catchups else None

    # Détection et rétablissement d'une source (OPC UA ou Modbus) : première valeur BAD après la coupure,
    # première valeur GOOD après le rétablissement
    src_events = [(t, ev) for t, ev in events if ev.get("lien") in ("opcua", "modbus")]
    if src_events:
        link = src_events[0][1]["lien"]
        prefix = "opcua:" if link == "opcua" else "modbus:"
        cut_t = next(t for t, ev in src_events if ev["action"] in ("coupure", "trou_noir"))
        rest_t = next((t for t, ev in src_events if ev["action"] == "retablir"), None)
        mine = [s for s in samples if s["src"].startswith(prefix)]
        bad = [s["recv"] / 1000 for s in mine if s["q"] == "BAD" and s["recv"] / 1000 >= cut_t]
        m["detection_s"] = round(min(bad) - cut_t, 2) if bad else None
        m["valeurs_bad_publiees"] = len(bad)
        if rest_t is not None:
            back = [s["recv"] / 1000 for s in mine if s["q"] == "GOOD" and s["recv"] / 1000 >= rest_t]
            m["retablissement_s"] = round(min(back) - rest_t, 2) if back else None
    return m


def evaluate(metrics: dict, criteria: dict) -> dict:
    out = {}
    for key, rule in (criteria or {}).items():
        v = metrics.get(key)
        ok = v is not None
        if ok and "max" in rule:
            ok = v <= rule["max"]
        if ok and "min" in rule:
            ok = v >= rule["min"]
        out[key] = {"valeur": v, **rule, "ok": bool(ok)}
    return out


async def run_one(sc: dict, delivery: str, *, scale: float = 1.0, cfg: dict | None = None,
                  workdir: str | Path | None = None) -> dict:
    """Joue un scénario pour un mode de livraison. scale < 1 raccourcit tous les temps (tests rapides)."""
    lab = Lab(cfg or load_config(), delivery=delivery, workdir=workdir)
    await lab.start()
    events: list[tuple[float, dict]] = []
    t_start = time.time()
    try:
        await asyncio.sleep(1.0)  # abonnements OPC UA en place
        t_start = time.time()
        for ev in sorted(sc.get("evenements", []), key=lambda e: e["t"]):
            wait = t_start + ev["t"] * scale - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            events.append((time.time(), ev))
            await _apply(lab, ev)
        wait = t_start + sc["duree_s"] * scale - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        t_end = time.time()
        await lab.shop.freeze()
        drain_s = await lab.gateway.wait_drained(timeout_s=30)
        await asyncio.sleep(0.5)
        await lab.gateway.stop()
        await asyncio.to_thread(lab.historian.flush)
        samples = load_samples(lab.dir / "historian.sqlite")
        metrics = compute_metrics(lab, samples, t_start, events, drain_s)
        if sc.get("cas_usage_trs"):
            metrics.update(oee_check(lab, samples, t_start, t_end))
        series = {"tampon": [(round(t - t_start, 2), d) for t, d in lab.gateway.buffer_series],
                  "reception": [round(s["recv"] / 1000 - t_start, 3) for s in samples],
                  "evenements": [(round(t - t_start, 2), ev["action"]) for t, ev in events]}
        lab.gateway = None  # déjà arrêtée
        return {"id": sc["id"], "titre": sc["titre"], "mode": delivery, "duree_s": sc["duree_s"] * scale,
                "indicateurs": metrics, "criteres": evaluate(metrics, sc.get("criteres")),
                "series": series}
    finally:
        await lab.stop()
        lab.cleanup()


async def run_scenario(sc: dict, *, scale: float = 1.0, modes: list[str] | None = None) -> list[dict]:
    results = []
    for mode in modes or sc.get("modes", ["disque"]):
        r = await run_one(sc, mode, scale=scale)
        r["ok"] = all(c["ok"] for c in r["criteres"].values())
        r["mode_retenu"] = mode == sc.get("mode_retenu", "disque")
        results.append(r)
    return results


def summarize(results: list[dict]) -> dict:
    return {f"{r['id']}[{r['mode']}]": {"ok": r["ok"], **{k: v for k, v in r["indicateurs"].items()
                                                         if v is not None}} for r in results}


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    sc = load_scenario(sys.argv[1])
    res = asyncio.run(run_scenario(sc, scale=float(sys.argv[2]) if len(sys.argv) > 2 else 1.0))
    print(json.dumps([{k: v for k, v in r.items() if k != "series"} for r in res], indent=1, ensure_ascii=False))
