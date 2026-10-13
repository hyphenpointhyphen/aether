"""Step 3: turn an *intent* into a connection plan with a three-way verdict.

The planner does not connect "everything that is type-compatible". It starts
from an intent ("feed the room temperature into the controller's external
room-temperature input") and only considers properties whose quantity kind
and role tags match it. Type-compatible near-misses (e.g. the outdoor
temperature) are listed as rejected candidates with a reason.

Verdicts
  compatible    every check passes; can be activated without further input
  conditional   works, but needs a human decision (approval, clamping, an
                unaccepted correction, an untested write path)
  incompatible  a blocking check failed (safety-critical sink, failed
                verification, unconvertible units, no matching source/sink)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import vocab

OK, COND, BLOCK = "ok", "condition", "block"


@dataclass
class Check:
    level: str
    message: str


@dataclass
class Ref:
    thing: str
    key: str
    prop: dict

    def describe(self) -> dict:
        p = self.prop
        return {
            "thing": self.thing, "property": self.key, "title": p.get("title"),
            "href": p["forms"][0]["href"], "dtype": p["ail:dtype"], "scale": p["ail:scale"],
            "unit": p.get("unit"), "range": [p.get("minimum"), p.get("maximum")],
        }


@dataclass
class Option:
    source: Ref
    sink: Ref | None
    checks: list[Check] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        levels = {c.level for c in self.checks}
        return "incompatible" if BLOCK in levels else "conditional" if COND in levels else "compatible"


# Properties with these roles are commands to a device, not measurements of the
# world, so they are never used as a data source unless an intent asks for it.
COMMAND_ROLES = {"input", "setpoint"}


def _matches(prop: dict, want: dict, as_source: bool = False) -> bool:
    roles = set(prop.get("ail:roles", []))
    if as_source and roles & COMMAND_ROLES and not set(want.get("roles", [])) & COMMAND_ROLES:
        return False
    return (prop.get("ail:quantityKind") == want.get("quantityKind")
            and set(want.get("roles", [])) <= roles)


def _all_props(things: list[dict]):
    for td in things:
        for key, p in td["properties"].items():
            yield Ref(td["title"], key, p)


def _verification_checks(ref: Ref, side: str) -> list[Check]:
    v = ref.prop.get("ail:verification", {}).get("status", "unverified")
    name = f"{side} {ref.thing}.{ref.key}"
    if v == "verified":
        return [Check(OK, f"{name} verified against live device")]
    if v == "conditional":
        return [Check(COND, f"{name} only plausible with a proposed correction that nobody accepted yet")]
    if v == "failed":
        return [Check(BLOCK, f"{name} failed verification")]
    return [Check(COND, f"{name} has not been verified")]


def _evaluate(src: Ref, sink: Ref | None, intent: dict) -> Option:
    opt = Option(src, sink)
    opt.checks += _verification_checks(src, "source")
    if sink is None:  # built-in read-only sink, e.g. a log
        opt.checks.append(Check(OK, f"sink is built-in '{intent['sink']['builtin']}' (read-only data flow)"))
        return opt

    sp, kp = src.prop, sink.prop
    if kp.get("ail:safetyCritical"):
        opt.checks.append(Check(BLOCK, f"sink {sink.thing}.{sink.key} is safety-critical; the AIL never writes it "
                                        "(keep this in the local controller or a certified safety function)"))
    opt.checks += _verification_checks(sink, "sink")
    if kp.get("ail:verification", {}).get("write") == "untested":
        opt.checks.append(Check(COND, "write path is untested: first activation needs human approval"))

    su, ku = sp.get("unit"), kp.get("unit")
    if su and ku and not vocab.convertible(su, ku):
        opt.checks.append(Check(BLOCK, f"units not convertible: {su} -> {ku}"))
    elif su != ku:
        opt.checks.append(Check(OK, f"unit conversion {su} -> {ku}"))
    else:
        opt.checks.append(Check(OK, f"same unit ({su})"))

    s_lo, s_hi = sp.get("minimum"), sp.get("maximum")
    k_lo, k_hi = kp.get("minimum"), kp.get("maximum")
    if None not in (s_lo, s_hi, k_lo, k_hi):
        if s_hi < k_lo or s_lo > k_hi:
            opt.checks.append(Check(BLOCK, f"ranges do not overlap: {s_lo}..{s_hi} vs {k_lo}..{k_hi}"))
        elif s_lo < k_lo or s_hi > k_hi:
            opt.checks.append(Check(COND, f"source range {s_lo}..{s_hi} exceeds sink range {k_lo}..{k_hi}: "
                                          f"values will be clamped"))
        else:
            opt.checks.append(Check(OK, "source range fits sink range"))

    if kp["ail:scale"] > sp["ail:scale"]:
        opt.checks.append(Check(OK, f"precision reduced to {kp['ail:scale']} {ku} steps at the sink"))
    return opt


def plan(things: list[dict], intent: dict) -> dict:
    props = list(_all_props(things))
    want_src, want_sink = intent["source"], intent["sink"]

    sources = [r for r in props if _matches(r.prop, want_src, as_source=True)]
    rejected = [
        {"candidate": f"{r.thing}.{r.key}", "reason":
            f"roles {r.prop.get('ail:roles')} do not match source roles {want_src.get('roles')}"
            if not set(r.prop.get("ail:roles", [])) & COMMAND_ROLES
            else "is a command input/setpoint, not a measurement"}
        for r in props
        if r.prop.get("ail:quantityKind") == want_src.get("quantityKind") and r not in sources
    ]

    if "builtin" in want_sink:
        sinks: list[Ref | None] = [None]
    else:
        sinks = [r for r in props if _matches(r.prop, want_sink) and not r.prop.get("readOnly", True)]
        rejected += [
            {"candidate": f"{r.thing}.{r.key}", "reason":
                f"as sink: roles {r.prop.get('ail:roles')} do not match sink roles {want_sink.get('roles')}"}
            for r in props
            if r.prop.get("ail:quantityKind") == want_sink.get("quantityKind")
            and not r.prop.get("readOnly", True) and r not in sinks
        ]

    merged: dict[str, list[str]] = {}
    for r in rejected:
        merged.setdefault(r["candidate"], []).append(r["reason"])
    rejected = [{"candidate": c, "reason": "; ".join(rs)} for c, rs in merged.items()]

    result = {"intent": intent, "approved": False}
    if not sources or not sinks:
        missing = "source" if not sources else "sink"
        return {**result, "verdict": "incompatible",
                "checks": [{"level": BLOCK, "message": f"no {missing} matches the intent"}],
                "rejected": rejected}

    options = [_evaluate(s, k, intent) for s in sources for k in sinks]
    order = {"compatible": 0, "conditional": 1, "incompatible": 2}
    options.sort(key=lambda o: (order[o.verdict], sum(c.level != OK for c in o.checks)))
    best = options[0]

    transform = [f"read {best.source.prop['ail:dtype']} * {best.source.prop['ail:scale']} "
                 f"-> {best.source.prop.get('unit')}"]
    if best.sink:
        kp = best.sink.prop
        if best.source.prop.get("unit") != kp.get("unit"):
            transform.append(f"convert {best.source.prop.get('unit')} -> {kp.get('unit')}")
        if kp.get("minimum") is not None:
            transform.append(f"clamp to {kp['minimum']}..{kp['maximum']}")
        transform.append(f"encode / {kp['ail:scale']} as {kp['ail:dtype']} -> write")
    else:
        transform.append(f"emit to {intent['sink']['builtin']}")

    return {
        **result,
        "verdict": best.verdict,
        "source": best.source.describe(),
        "sink": best.sink.describe() if best.sink else {"builtin": intent["sink"]["builtin"]},
        "periodSeconds": intent.get("periodSeconds", 30),
        "transform": transform,
        "checks": [{"level": c.level, "message": c.message} for c in best.checks],
        "rejected": rejected,
        "alternatives": len(options) - 1,
    }


def summary(p: dict) -> list[str]:
    icon = {"compatible": "COMPATIBLE", "conditional": "CONDITIONAL", "incompatible": "INCOMPATIBLE"}
    lines = [f"intent '{p['intent']['name']}': {icon[p['verdict']]}"]
    if "source" in p:
        s, k = p["source"], p["sink"]
        lines.append(f"  source: {s['thing']}.{s['property']}")
        lines.append(f"  sink:   {k.get('builtin') or k['thing'] + '.' + k['property']}")
        lines.append(f"  transform: {' | '.join(p['transform'])}")
    mark = {OK: "+", COND: "?", BLOCK: "x"}
    for c in p["checks"]:
        lines.append(f"   {mark[c['level']]} {c['message']}")
    for r in p.get("rejected", []):
        lines.append(f"   - rejected {r['candidate']}: {r['reason']}")
    return lines
