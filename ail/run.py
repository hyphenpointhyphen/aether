"""Step 4: execute a plan. Dry-run unless the plan was approved.

Guard rails (enforced here, not just in the planner):
  * incompatible plans never run
  * writes need `approved: true` in the plan file AND --apply on the command line
  * safety-critical sinks are refused even if someone edits the plan file
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from . import vocab
from .bus import Bus, decode, encode


def approve(plan: dict, who: str) -> dict:
    if plan["verdict"] == "incompatible":
        raise PermissionError("incompatible plans cannot be approved")
    open_items = [c["message"] for c in plan["checks"] if c["level"] == "condition"]
    return {**plan, "approved": True,
            "approval": {"by": who, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "acknowledged_conditions": open_items}}


def step(bus: Bus, plan: dict, apply: bool) -> str:
    s = plan["source"]
    value = decode(bus.read_raw(s["href"]), s["dtype"], s["scale"])
    line = f"{s['thing']}.{s['property']} = {value} {s['unit']}"
    k = plan["sink"]
    if "builtin" in k:
        print(json.dumps({"t": time.time(), "source": f"{s['thing']}.{s['property']}", "value": value,
                          "unit": s["unit"]}))
        return line + " -> logged"
    out = vocab.convert(value, s["unit"], k["unit"]) if s["unit"] and k["unit"] else value
    lo, hi = k["range"]
    if lo is not None:
        out = min(max(out, lo), hi)
    raw = encode(out, k["dtype"], k["scale"])
    target = f"{k['thing']}.{k['property']}"
    if apply:
        bus.write_raw(k["href"], raw)
        echo = decode(bus.read_raw(k["href"]), k["dtype"], k["scale"])
        return line + f" -> wrote raw {raw} ({round(raw * k['scale'], 3)} {k['unit']}) to {target}; read back {echo}"
    return line + f" -> [dry-run] would write raw {raw} ({round(raw * k['scale'], 3)} {k['unit']}) to {target}"


def run(plan: dict, iterations: int = 3, apply: bool = False, period: float | None = None,
        things: list[dict] | None = None) -> None:
    if plan["verdict"] == "incompatible":
        raise PermissionError("plan is incompatible; refusing to run")
    if apply and not plan.get("approved"):
        raise PermissionError("plan is not approved; run `ail approve` first or omit --apply")
    if apply and things:  # re-check safety flag against the current descriptions
        k = plan["sink"]
        for td in things:
            p = td["properties"].get(k.get("property")) if td["title"] == k.get("thing") else None
            if p and p.get("ail:safetyCritical"):
                raise PermissionError("sink is safety-critical; refusing to write")
    bus = Bus()
    period = plan.get("periodSeconds", 30) if period is None else period
    try:
        for i in range(iterations):
            print(f"  [{i + 1}/{iterations}] {step(bus, plan, apply)}")
            if i < iterations - 1:
                time.sleep(period)
    finally:
        bus.close()
