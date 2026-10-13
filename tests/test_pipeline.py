import os

import pytest

from ail import draft, planner, run, sim, verify

EX = os.path.join(os.path.dirname(__file__), "..", "examples")


@pytest.fixture(scope="module")
def things():
    sim.start_in_thread()
    rs = draft.draft(os.path.join(EX, "datasheets", "rs200.md"), "modbus+tcp://127.0.0.1:5020/1")
    hc = draft.draft(os.path.join(EX, "datasheets", "hc10.md"), "modbus+tcp://127.0.0.1:5021/1")
    return rs, hc


def intent(name):
    import json
    with open(os.path.join(EX, "intents", name + ".json")) as f:
        return json.load(f)


def test_drafter_tags_german_datasheet(things):
    _, hc = things
    p = hc["properties"]
    assert p["t_aussen"]["ail:roles"] == ["outdoor"]
    assert set(p["t_raum_ext"]["ail:roles"]) == {"room", "input"}
    assert p["t_vl_soll"]["ail:safetyCritical"] is True
    assert p["t_raum_ext"]["readOnly"] is False


def test_verifier_catches_wrong_scale(things):
    rs, _ = things
    v = verify.verify(rs, samples=2, interval=0.05)
    rt = v["properties"]["room_temp"]["ail:verification"]
    assert rt["status"] == "conditional"
    assert rt["suggestion"]["ail:scale"] == 0.1
    fixed = verify.verify(rs, samples=2, interval=0.05, accept_suggestions=True)
    assert fixed["properties"]["room_temp"]["ail:verification"]["status"] == "verified"
    assert fixed["properties"]["room_temp"]["ail:scale"] == 0.1


def test_three_verdicts(things):
    rs, hc = things
    rs = verify.verify(rs, samples=2, interval=0.05, accept_suggestions=True)
    hc = verify.verify(hc, samples=2, interval=0.05)
    assert planner.plan([rs, hc], intent("log-humidity"))["verdict"] == "compatible"
    p = planner.plan([rs, hc], intent("room-temp-to-controller"))
    assert p["verdict"] == "conditional"
    assert p["sink"]["property"] == "t_raum_ext"
    assert planner.plan([rs, hc], intent("room-temp-to-flow-setpoint"))["verdict"] == "incompatible"


def test_runner_guards(things):
    rs, hc = things
    rs = verify.verify(rs, samples=1, accept_suggestions=True)
    hc = verify.verify(hc, samples=1)
    p = planner.plan([rs, hc], intent("room-temp-to-controller"))
    with pytest.raises(PermissionError):
        run.run(p, iterations=1, apply=True)  # not approved
    bad = planner.plan([rs, hc], intent("room-temp-to-flow-setpoint"))
    with pytest.raises(PermissionError):
        run.approve(bad, "someone")
    forged = {**bad, "verdict": "conditional", "approved": True}
    with pytest.raises(PermissionError):
        run.run(forged, iterations=1, apply=True, things=[rs, hc])  # safety flag re-checked
