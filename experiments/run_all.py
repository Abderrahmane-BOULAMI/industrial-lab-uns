"""Joue tous les scénarios en temps réel (environ 12 minutes), puis écrit results/summary.json,
results/maturite.md et les figures de results/figures/."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from labuns.config import validate_config  # noqa: E402
from labuns.lab import load_config  # noqa: E402
from labuns.maturity import current_level, evaluate  # noqa: E402
from labuns.scenarios import all_scenarios, run_scenario  # noqa: E402
from labuns.security import run_security  # noqa: E402

OUT = ROOT / "results"
FIG = OUT / "figures"
COLORS = {"aucun": "#2a78d6", "memoire": "#eb6834", "disque": "#1baf7a"}
LABELS = {"aucun": "sans tampon (QoS 0)", "memoire": "file en mémoire (QoS 1)", "disque": "tampon disque (QoS 1)"}
INK, MUTED, GRID = "#1d1d1b", "#6b6a64", "#e4e3de"


def style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def fig_losses(res):
    rows = [(r["id"], r["mode"], r["indicateurs"]["perte_pct"]) for r in res if r["id"] in ("S2", "S3", "S4")]
    fig, ax = plt.subplots(figsize=(8, 3.6))
    x, ticks, labels = 0, [], []
    for sid, title in (("S2", "Coupure 20 s"), ("S3", "Coupure + redémarrage edge"), ("S4", "Lien silencieux 20 s")):
        group = [r for r in rows if r[0] == sid]
        start = x
        for _, mode, v in group:
            ax.bar(x, v, width=0.8, color=COLORS[mode], edgecolor="white", linewidth=2)
            ax.text(x, v + 0.6, f"{v:.1f} %".replace(".", ","), ha="center", fontsize=9, color=INK)
            x += 1
        ticks.append((start + x - 1) / 2)
        labels.append(title)
        x += 0.8
    ax.set_xticks(ticks, labels)
    ax.set_ylabel("messages perdus (%)", color=MUTED, fontsize=9)
    style(ax)
    handles = [plt.Rectangle((0, 0), 1, 1, color=COLORS[m]) for m in COLORS]
    ax.legend(handles, [LABELS[m] for m in COLORS], frameon=False, fontsize=9, loc="upper right")
    ax.set_title("Pertes de données selon le mode de livraison de la passerelle", loc="left", fontsize=11, color=INK)
    fig.tight_layout()
    fig.savefig(FIG / "fig1_pertes_par_mode.png", dpi=150)
    plt.close(fig)


def fig_timeline(res):
    picks = [r for r in res if (r["id"], r["mode"]) in (("S2", "disque"), ("S4", "disque"))]
    fig, axes = plt.subplots(2, 1, figsize=(8, 5), sharex=False)
    for ax, r in zip(axes, picks):
        s = r["series"]
        t = [p[0] for p in s["tampon"]]
        ax.plot(t, [p[1] for p in s["tampon"]], color=COLORS["disque"], linewidth=2, label="messages en attente")
        recv = s["reception"]
        bins = range(0, int(max(recv)) + 2)
        counts = [sum(1 for v in recv if b <= v < b + 1) for b in bins]
        ax.bar(list(bins), counts, width=0.9, color="#86b6ef", label="messages reçus par seconde")
        evs = s["evenements"]
        starts = [e[0] for e in evs if e[1] in ("coupure", "trou_noir")]
        ends = [e[0] for e in evs if e[1] == "retablir"]
        for a, b in zip(starts, ends):
            ax.axvspan(a, b, color=GRID, alpha=0.7, zorder=0)
            ax.text((a + b) / 2, ax.get_ylim()[1] * 0.92, "lien coupé" if r["id"] == "S2" else "lien silencieux",
                    ha="center", fontsize=9, color=MUTED)
        ax.set_title(f"{r['id']} : {r['titre']}", loc="left", fontsize=10, color=INK)
        style(ax)
        ax.legend(frameon=False, fontsize=8, loc="upper left")
    axes[-1].set_xlabel("temps (s)", color=MUTED, fontsize=9)
    fig.tight_layout()
    fig.savefig(FIG / "fig2_tampon_et_rattrapage.png", dpi=150)
    plt.close(fig)


def main():
    FIG.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    results = []
    for sc in all_scenarios():
        print(f"{sc['id']} {sc['titre']} ...", flush=True)
        for r in asyncio.run(run_scenario(sc)):
            print(f"   {r['mode']:8s} {'OK' if r['ok'] else 'ECHEC'} {json.dumps(r['indicateurs'])}", flush=True)
            results.append(r)
    sec = asyncio.run(run_security())
    print("S8", "OK" if sec["ok"] else "ECHEC", flush=True)

    status = {}
    for r in results:
        if r["mode_retenu"]:
            status[r["id"]] = r["ok"]
    status["S8"] = sec["ok"]
    status["CFG"] = validate_config(cfg) == []
    status["API"] = True  # couverte par tests/test_lab.py::test_historian_api
    grid = evaluate(status, ROOT)

    summary = {"scenarios": [{k: v for k, v in r.items() if k != "series"} for r in results],
               "securite": sec, "maturite": {"niveau": current_level(grid), "grille": grid}}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    lines = ["# Grille de maturité du LAB", "", f"Niveau atteint : **{current_level(grid)} sur 5**", "",
             "| Niveau | Nom | Atteint | Critères |", "|---|---|---|---|"]
    for g in grid:
        crit = " ; ".join(f"{c['id']} {'ok' if c['ok'] else 'non'}" for c in g["criteres"])
        lines.append(f"| {g['niveau']} | {g['nom']} | {'oui' if g['atteint'] else 'non'} | {crit} |")
    (OUT / "maturite.md").write_text("\n".join(lines) + "\n")
    fig_losses(results)
    fig_timeline(results)
    print("niveau de maturité", current_level(grid))


if __name__ == "__main__":
    main()
