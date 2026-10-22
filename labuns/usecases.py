"""Cas d'usage construit uniquement sur l'UNS : disponibilité et production des centres d'usinage.

Il ne parle à aucune machine : il lit les topics etat et compteur-pieces. On le compare à la vérité du
simulateur pour démontrer que la chaîne de données suffit à calculer un indicateur de pilotage fiable.
"""
from __future__ import annotations

from .plant import availability_from_transitions


def availability_from_uns(samples: list[dict], machine: str, t0: float, t1: float) -> float | None:
    states = sorted((s["ts"] / 1000, s["v"]) for s in samples
                    if s["topic"].endswith(f"/{machine}/etat") and s["q"] == "GOOD")
    if not states:
        return None
    return availability_from_transitions(states, t0, t1)


def parts_from_uns(samples: list[dict], machine: str) -> int | None:
    counts = [s for s in samples if s["topic"].endswith(f"/{machine}/compteur-pieces") and s["q"] == "GOOD"]
    return max(counts, key=lambda s: s["ts"])["v"] if counts else None


def oee_check(lab, samples: list[dict], t0: float, t1: float) -> dict:
    out, gaps = {}, []
    for mid, machine in lab.shop.plant.machines.items():
        a_uns = availability_from_uns(samples, mid, t0, t1)
        a_true = machine.availability(t0, t1)
        p_uns, p_true = parts_from_uns(samples, mid), machine.part_count
        out[f"dispo_{mid}_uns_pct"] = None if a_uns is None else round(100 * a_uns, 2)
        out[f"dispo_{mid}_reelle_pct"] = round(100 * a_true, 2)
        out[f"pieces_{mid}_uns"] = p_uns
        out[f"pieces_{mid}_reelles"] = p_true
        if a_uns is not None:
            gaps.append(abs(a_uns - a_true) * 100)
        out[f"changements_etat_{mid}"] = sum(1 for t, _ in machine.transitions if t0 <= t <= t1)
    out["ecart_dispo_max_points"] = round(max(gaps), 3) if gaps else None
    out["ecart_pieces_max"] = max(abs((out[f"pieces_{m}_uns"] or 0) - out[f"pieces_{m}_reelles"])
                                  for m in lab.shop.plant.machines)
    return out
