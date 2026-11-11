"""Where a run's time went, worked out from its events."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

SLOW_SUBAGENT_RUNS = 2


def summarise(events: list[dict[str, Any]], wall_seconds: float | None) -> dict[str, Any]:
    agents: dict[str, dict[str, float]] = defaultdict(lambda: {"turns": 0, "tokens": 0, "model_seconds": 0.0,
                                                               "tool_seconds": 0.0})
    slowest: list[dict[str, Any]] = []
    delegations: Counter[str] = Counter()
    failures: list[str] = []
    blocked = 0
    last_detail: dict[tuple[str, str], str] = {}

    for event in events:
        agent, kind = event.get("agent", "main"), event.get("event")
        if kind == "model":
            totals = agents[agent]
            totals["turns"] += 1
            totals["tokens"] += event.get("tokens", 0)
            totals["model_seconds"] += event.get("seconds", 0)
            slowest.append({"agent": agent, "label": "Thinking", "seconds": event.get("seconds", 0)})
        elif kind == "tool_start":
            last_detail[(agent, event.get("tool", ""))] = event.get("detail", "")
            if event.get("tool") == "task":
                delegations[event.get("detail", "").split(":")[0]] += 1
        elif kind == "tool_end":
            tool = event.get("tool", "")
            if tool != "task":  # a delegation's time is already counted in its subagent's steps
                agents[agent]["tool_seconds"] += event.get("seconds", 0)
                slowest.append({"agent": agent, "label": tool, "seconds": event.get("seconds", 0)})
            if not event.get("ok", True):
                failures.append(f"{tool}: {event.get('result') or last_detail.get((agent, tool), '')}"[:200])
        elif kind == "tool_blocked":
            blocked += 1

    model_seconds = sum(a["model_seconds"] for a in agents.values())
    tool_seconds = sum(a["tool_seconds"] for a in agents.values())
    turns = sum(a["turns"] for a in agents.values())
    tokens = sum(a["tokens"] for a in agents.values())
    repairs = max(delegations.get("verifier", 0) - 1, 0)
    return {
        "wall_seconds": wall_seconds, "model_seconds": round(model_seconds, 1), "tool_seconds": round(tool_seconds, 1),
        "turns": turns, "tokens": tokens, "repair_rounds": repairs, "delegations": dict(delegations),
        "agents": {name: {**totals, "model_seconds": round(totals["model_seconds"], 1),
                          "tool_seconds": round(totals["tool_seconds"], 1)} for name, totals in agents.items()},
        "slowest": sorted(slowest, key=lambda step: step["seconds"], reverse=True)[:5],
        "failures": failures[-5:],
        "explanation": explain(model_seconds, tool_seconds, turns, repairs, len(failures), blocked),
    }


def explain(model_seconds: float, tool_seconds: float, turns: int, repairs: int, failures: int, blocked: int) -> list[str]:
    """Why this run took as long as it did, in plain language."""
    if not turns:
        return []
    total = model_seconds + tool_seconds
    notes = []
    if total:
        average = model_seconds / turns
        notes.append(f"{model_seconds / total:.0%} of the working time was waiting for the model: {turns} calls, "
                     f"about {average:.0f}s each. Data and the semantic layer took {tool_seconds:.0f}s.")
    if repairs:
        notes.append(f"The verifier ran {repairs + 1} times, so {repairs} repair round{'s' if repairs > 1 else ''} "
                     "repeated earlier work. This is usually the biggest avoidable cost.")
    if failures:
        notes.append(f"{failures} tool call{'s' if failures > 1 else ''} failed and had to be retried.")
    if blocked:
        notes.append(f"The run limit stopped {blocked} further repair{'s' if blocked > 1 else ''}.")
    if not repairs and not failures:
        notes.append("It went straight through, with no repairs.")
    return notes
