"""Command line interface: python -m ail <command> ..."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import draft as draft_mod, planner, run as run_mod, sim, verify as verify_mod


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(obj, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    print(f"  wrote {path}")


def cmd_sim(a):
    sim.run_forever(a.host)


def cmd_draft(a):
    td = draft_mod.draft(a.datasheet, a.endpoint, a.title, use_llm=a.llm)
    for k, p in td["properties"].items():
        print(f"  {k:<12} {p['ail:quantityKind'] or '?':<17} roles={p['ail:roles']} "
              f"scale={p['ail:scale']} unit={p['unit']} "
              f"{'RW' if not p['readOnly'] else 'R '}{' SAFETY' if p['ail:safetyCritical'] else ''} "
              f"conf={p['ail:provenance']['confidence']}")
    _save(td, a.out)


def cmd_verify(a):
    td = verify_mod.verify(_load(a.td), samples=a.samples, accept_suggestions=a.accept_suggestions)
    print("\n".join(verify_mod.summary(td)))
    _save(td, a.out)


def cmd_plan(a):
    p = planner.plan([_load(t) for t in a.things], _load(a.intent))
    print("\n".join(planner.summary(p)))
    _save(p, a.out)


def cmd_approve(a):
    p = run_mod.approve(_load(a.plan), a.by)
    for c in p["approval"]["acknowledged_conditions"]:
        print(f"  acknowledged: {c}")
    _save(p, a.plan)


def cmd_run(a):
    things = [_load(t) for t in a.things] if a.things else None
    run_mod.run(_load(a.plan), iterations=a.iterations, apply=a.apply, period=a.period, things=things)


def cmd_demo(a):
    out = a.out
    root = os.path.join(os.path.dirname(__file__), "..", "examples")
    print("== starting simulated devices (RS-200 :5020, HC-10 :5021)")
    sim.start_in_thread()

    print("\n== 1. draft descriptions from datasheets")
    tds = {}
    for name, port in (("rs200", 5020), ("hc10", 5021)):
        td = draft_mod.draft(os.path.join(root, "datasheets", f"{name}.md"), f"modbus+tcp://127.0.0.1:{port}/1")
        print(f"  {td['title']}: " + ", ".join(
            f"{k} ({p['ail:quantityKind']}/{'+'.join(p['ail:roles']) or '-'}"
            f"{', SAFETY' if p['ail:safetyCritical'] else ''})" for k, p in td["properties"].items()))
        _save(td, os.path.join(out, f"{name}.draft.td.json"))
        tds[name] = td

    print("\n== 2. verify against the live devices (read-only)")
    for name, td in tds.items():
        v = verify_mod.verify(td, samples=3, interval=0.2)
        print("\n".join(verify_mod.summary(v)))
        tds[name] = v

    print("\n== 3. plan before accepting corrections")
    intent = _load(os.path.join(root, "intents", "room-temp-to-controller.json"))
    print("\n".join(planner.summary(planner.plan(list(tds.values()), intent))))

    print("\n== 4. human accepts the proposed correction, re-verify")
    tds["rs200"] = verify_mod.verify(tds["rs200"], samples=3, interval=0.2, accept_suggestions=True)
    print("\n".join(verify_mod.summary(tds["rs200"])))
    for name, td in tds.items():
        _save(td, os.path.join(out, f"{name}.td.json"))

    print("\n== 5. plan all three intents")
    plans = {}
    for f in ("room-temp-to-controller", "log-humidity", "room-temp-to-flow-setpoint"):
        p = planner.plan(list(tds.values()), _load(os.path.join(root, "intents", f + ".json")))
        print("\n".join(planner.summary(p)) + "\n")
        _save(p, os.path.join(out, f"{f}.plan.json"))
        plans[f] = p

    print("\n== 6. run: humidity log (compatible), then room temp dry-run, approval, apply")
    run_mod.run(plans["log-humidity"], iterations=2, period=0.3)
    run_mod.run(plans["room-temp-to-controller"], iterations=2, period=0.3)
    approved = run_mod.approve(plans["room-temp-to-controller"], "demo-operator")
    run_mod.run(approved, iterations=2, period=0.3, apply=True, things=list(tds.values()))
    try:
        run_mod.run(plans["room-temp-to-flow-setpoint"], iterations=1)
    except PermissionError as e:
        print(f"  refused: {e}")
    print(f"\nDone. Artifacts in {out}/")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ail", description="Adaptive Interoperability Layer - MVP")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sim", help="run the simulated Modbus devices")
    s.add_argument("--host", default="127.0.0.1")
    s.set_defaults(fn=cmd_sim)

    s = sub.add_parser("draft", help="draft a Thing Description from a datasheet")
    s.add_argument("datasheet")
    s.add_argument("--endpoint", required=True, help="e.g. modbus+tcp://127.0.0.1:5020/1")
    s.add_argument("--title")
    s.add_argument("--llm", action="store_true", help="use the LLM drafter (needs ANTHROPIC_API_KEY)")
    s.add_argument("-o", "--out", required=True)
    s.set_defaults(fn=cmd_draft)

    s = sub.add_parser("verify", help="verify a draft against the live device (read-only)")
    s.add_argument("td")
    s.add_argument("--samples", type=int, default=5)
    s.add_argument("--accept-suggestions", action="store_true",
                   help="apply proposed corrections (a human decision, recorded in the TD)")
    s.add_argument("-o", "--out", required=True)
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("plan", help="plan a connection for an intent")
    s.add_argument("--intent", required=True)
    s.add_argument("--things", nargs="+", required=True)
    s.add_argument("-o", "--out", required=True)
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("approve", help="approve a conditional plan (records who and which conditions)")
    s.add_argument("plan")
    s.add_argument("--by", required=True)
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("run", help="execute a plan (dry-run unless --apply)")
    s.add_argument("plan")
    s.add_argument("--things", nargs="*")
    s.add_argument("--iterations", type=int, default=3)
    s.add_argument("--period", type=float)
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("demo", help="run the whole pipeline against the simulator")
    s.add_argument("--out", default="out")
    s.set_defaults(fn=cmd_demo)

    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (PermissionError, ConnectionError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
