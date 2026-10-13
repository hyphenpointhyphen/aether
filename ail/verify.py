"""Step 2: verify a drafted Thing Description against the live device.

Read-only by design. For every property the verifier:

1. reads the register several times (reachability, stability),
2. decodes the samples with the drafted dtype and scale,
3. checks them against the datasheet range AND an independent physical
   plausibility envelope for the quantity kind,
4. if the values are implausible, searches alternative interpretations
   (other scale factors, signed/unsigned) and proposes a correction.

It never writes to a device. Writable properties are marked
`write: "untested"` - write paths are only exercised later, by a plan a
human has approved.

Status per property:
  verified     samples fit the description
  conditional  samples only fit with a correction (needs human acceptance)
  failed       unreachable, or no interpretation fits
"""
from __future__ import annotations

import copy
import math
import time
from datetime import datetime, timezone

from . import vocab
from .bus import Bus, decode

ALT_SCALES = [1, 0.1, 0.01, 0.5, 10]
ALT_DTYPES = ["int16", "uint16"]


def _envelope(prop: dict) -> tuple[float, float] | None:
    """Tightest sanity range we can justify for this property."""
    qk, unit = prop.get("ail:quantityKind"), prop.get("unit")
    env = vocab.PLAUSIBLE.get((qk, unit))
    for role in prop.get("ail:roles", []):
        typ = vocab.TYPICAL.get((qk, role))
        if typ and env:
            env = (max(env[0], typ[0]), min(env[1], typ[1]))
    return env


def _fits(values: list[float], prop: dict) -> tuple[bool, list[str]]:
    problems = []
    lo, hi = prop.get("minimum"), prop.get("maximum")
    if lo is not None and any(v < lo or v > hi for v in values):
        problems.append(f"outside datasheet range {lo}..{hi}")
    env = _envelope(prop)
    if env and any(v < env[0] or v > env[1] for v in values):
        problems.append(f"physically implausible for {prop.get('ail:quantityKind')} "
                        f"{'/'.join(prop.get('ail:roles', []))} (expected {env[0]}..{env[1]} {prop.get('unit')})")
    return not problems, problems


def verify_property(bus: Bus, prop: dict, samples: int, interval: float) -> dict:
    href = prop["forms"][0]["href"]
    raws: list[int] = []
    try:
        for i in range(samples):
            raws.append(bus.read_raw(href)[0])
            if i < samples - 1:
                time.sleep(interval)
    except Exception as e:  # noqa: BLE001 - surface any transport error as evidence
        return {"status": "failed", "problems": [f"read error: {e}"]}

    dtype, scale = prop["ail:dtype"], prop["ail:scale"]
    values = [decode([r], dtype, scale) for r in raws]
    ok, problems = _fits(values, prop)
    result = {
        "raw_samples": raws,
        "decoded": values,
        "write": "untested" if not prop.get("readOnly", True) else "n/a",
    }
    if ok:
        return {"status": "verified", **result}

    # Search alternative interpretations that make the data plausible.
    candidates = []
    for d in ALT_DTYPES:
        for s in ALT_SCALES:
            if (d, s) == (dtype, scale):
                continue
            alt = [decode([r], d, s) for r in raws]
            if _fits(alt, prop)[0]:
                # prefer: same dtype, then scale closest to the documented one
                rank = (d != dtype, abs(math.log10(s / scale)))
                candidates.append((rank, d, s, alt))
    if candidates:
        candidates.sort(key=lambda c: c[0])
        _, d, s, alt = candidates[0]
        return {
            "status": "conditional",
            "problems": problems,
            "suggestion": {"ail:dtype": d, "ail:scale": s, "decoded_with_suggestion": alt,
                           "alternatives": len(candidates)},
            **result,
        }
    return {"status": "failed", "problems": problems, **result}


def verify(td: dict, bus: Bus | None = None, samples: int = 3, interval: float = 0.3,
           accept_suggestions: bool = False) -> dict:
    bus = bus or Bus()
    out = copy.deepcopy(td)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key, prop in out["properties"].items():
        res = verify_property(bus, prop, samples, interval)
        res["at"] = now
        if res["status"] == "conditional" and accept_suggestions:
            sug = res["suggestion"]
            corrected_from = {"ail:dtype": prop["ail:dtype"], "ail:scale": prop["ail:scale"]}
            prop["ail:dtype"], prop["ail:scale"] = sug["ail:dtype"], sug["ail:scale"]
            prop["forms"][0]["modv:type"] = "xsd:short" if sug["ail:dtype"] == "int16" else "xsd:unsignedShort"
            # Re-verify with the corrected description so the evidence is fresh.
            res = verify_property(bus, prop, samples, interval)
            res.update(at=now, corrected_from=corrected_from, accepted_by="human (--accept-suggestions)")
        prop["ail:verification"] = res
    return out


def summary(td: dict) -> list[str]:
    lines = [f"{td['title']}:"]
    for key, p in td["properties"].items():
        v = p["ail:verification"]
        tag = {"verified": "OK  ", "conditional": "FIX?", "failed": "FAIL"}.get(v["status"], "??  ")
        detail = ""
        if v.get("decoded"):
            detail = f"samples={v['decoded']} {p.get('unit') or ''}"
        if v["status"] == "conditional":
            s = v["suggestion"]
            detail += (f"\n           -> {'; '.join(v['problems'])}"
                       f"\n           -> suggestion: dtype={s['ail:dtype']} scale={s['ail:scale']}"
                       f" gives {s['decoded_with_suggestion']}")
        elif v["status"] == "failed":
            detail += f"\n           -> {'; '.join(v.get('problems', []))}"
        if v.get("corrected_from"):
            detail += f"  (corrected from scale {v['corrected_from']['ail:scale']}, {v['accepted_by']})"
        if v.get("write") == "untested":
            detail += "  [write path untested]"
        lines.append(f"  [{tag}] {key:<12} {detail}")
    return lines
