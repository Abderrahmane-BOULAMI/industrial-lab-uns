"""Modèle de l'atelier simulé : deux centres d'usinage et un four de traitement thermique.

Le modèle garde la « vérité terrain » (temps passé dans chaque état, pièces produites) pour vérifier
ensuite ce que les cas d'usage recalculent à partir des seules données de l'UNS.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

STATES = ("PRODUCTION", "ATTENTE", "ARRET", "DEFAUT")

# Taux de transition par seconde (chaîne de Markov à temps continu), choisis pour donner des
# disponibilités réalistes sur quelques minutes de simulation.
RATES = {
    "PRODUCTION": {"ATTENTE": 1 / 60, "DEFAUT": 1 / 150, "ARRET": 1 / 300},
    "ATTENTE": {"PRODUCTION": 1 / 8},
    "ARRET": {"PRODUCTION": 1 / 15},
    "DEFAUT": {"PRODUCTION": 1 / 12},
}
ALARMS = ("", "E101 surcharge broche", "E214 défaut lubrification", "E330 porte ouverte")
PROGRAMS = ("O1001-disque-ebauche", "O1002-disque-finition", "O2001-carter-ebauche")


@dataclass
class Machine:
    id: str
    cycle_s: float
    load_mean_pct: float
    state: str = "PRODUCTION"
    spindle_load: float = 0.0
    part_count: int = 0
    program: str = PROGRAMS[0]
    alarm: str = ""
    _progress: float = 0.0
    time_in_state: dict = field(default_factory=lambda: {s: 0.0 for s in STATES})
    transitions: list = field(default_factory=list)  # (horodatage, nouvel état), vérité terrain

    def step(self, dt: float, rng: random.Random, now: float = 0.0) -> None:
        if not self.transitions:
            self.transitions.append((now, self.state))
        self.time_in_state[self.state] += dt
        for nxt, rate in RATES[self.state].items():
            if rng.random() < rate * dt:
                self.state = nxt
                self.transitions.append((now, nxt))
                self.alarm = rng.choice(ALARMS[1:]) if nxt == "DEFAUT" else ""
                break
        if self.state == "PRODUCTION":
            self._progress += dt
            if self._progress >= self.cycle_s:
                self._progress -= self.cycle_s
                self.part_count += 1
                if rng.random() < 0.1:
                    self.program = rng.choice(PROGRAMS)
            self.spindle_load = max(0.0, rng.gauss(self.load_mean_pct, 6.0))
        else:
            self.spindle_load = 0.0

    def availability(self, t0: float | None = None, t1: float | None = None) -> float:
        """Disponibilité (part du temps en PRODUCTION) entre t0 et t1, d'après les transitions."""
        if t0 is None:
            total = sum(self.time_in_state.values())
            return self.time_in_state["PRODUCTION"] / total if total else 0.0
        return availability_from_transitions(self.transitions, t0, t1)


def availability_from_transitions(transitions: list, t0: float, t1: float) -> float:
    """Part du temps passé en PRODUCTION sur [t0, t1] pour une suite de (horodatage, état) triée."""
    run, state, last = 0.0, None, t0
    for ts, st in transitions:
        if ts <= t0:
            state = st
            continue
        if ts >= t1:
            break
        if state == "PRODUCTION":
            run += ts - last
        state, last = st, ts
    if state == "PRODUCTION":
        run += t1 - last
    return run / (t1 - t0) if t1 > t0 else 0.0


@dataclass
class Furnace:
    id: str
    setpoint_c: float
    ramp_c_per_s: float
    soak_s: float
    cool_c_per_s: float
    ambient_c: float = 25.0
    temperature: float = 25.0
    target: float = 25.0
    phase: int = 0  # 0 attente, 1 montée, 2 palier, 3 refroidissement
    batch: int = 1000
    heartbeat: int = 0
    _t_phase: float = 0.0

    def step(self, dt: float, rng: random.Random) -> None:
        self._t_phase += dt
        self.heartbeat = (self.heartbeat + 1) % 65536
        if self.phase == 0 and self._t_phase > 5:
            self.phase, self._t_phase, self.batch = 1, 0.0, self.batch + 1
        elif self.phase == 1:
            self.target = min(self.setpoint_c, self.target + self.ramp_c_per_s * dt)
            if self.temperature >= self.setpoint_c - 3:
                self.phase, self._t_phase = 2, 0.0
        elif self.phase == 2:
            self.target = self.setpoint_c
            if self._t_phase >= self.soak_s:
                self.phase, self._t_phase = 3, 0.0
        elif self.phase == 3:
            self.target = max(self.ambient_c, self.target - self.cool_c_per_s * dt)
            if self.temperature <= 60:
                self.phase, self._t_phase = 0, 0.0
        # Réponse du premier ordre (constante de temps 4 s) et bruit de mesure
        self.temperature += (self.target - self.temperature) * min(1.0, dt / 4.0) + rng.gauss(0, 0.4)

    def registers(self) -> list[int]:
        return [int(round(self.temperature * 10)) & 0xFFFF, int(round(self.setpoint_c * 10)) & 0xFFFF,
                self.phase, self.batch & 0xFFFF, self.heartbeat]


class Plant:
    def __init__(self, cfg: dict):
        sim = cfg["simulation"]
        self.rng = random.Random(sim.get("seed", 0))
        self.tick_s = sim.get("tick_s", 0.2)
        self.machines = {m["id"]: Machine(m["id"], m["cycle_s"], m["load_mean_pct"]) for m in sim["machines"]}
        f = sim["furnace"]
        self.furnace = Furnace(f["id"], f["setpoint_c"], f["ramp_c_per_s"], f["soak_s"], f["cool_c_per_s"])
        self.t = 0.0

    def step(self, dt: float | None = None, now: float | None = None) -> None:
        dt = dt or self.tick_s
        self.t += dt
        for m in self.machines.values():
            m.step(dt, self.rng, self.t if now is None else now)
        self.furnace.step(dt, self.rng)
