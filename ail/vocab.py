"""Tiny semantic vocabulary used by the heuristic drafter and the verifier.

In a real system this would map onto established ontologies (QUDT for
quantities and units, SAREF / Brick for roles). The MVP keeps a small,
explicit table so that every inference is easy to inspect.
"""

# keyword -> quantity kind
QUANTITY_KEYWORDS = {
    "Temperature": ["temperature", "temp", "temperatur"],
    "RelativeHumidity": ["humidity", "feuchte", "rh"],
    "BatteryLevel": ["battery", "batt", "batterie"],
}

# keyword -> role tag
ROLE_KEYWORDS = {
    "room": ["room", "raum", "indoor", "innen"],
    "outdoor": ["outdoor", "outside", "außen", "aussen"],
    "flow": ["flow", "vorlauf", "supply"],
    "setpoint": ["setpoint", "soll", "sollwert", "target"],
    "input": ["input", "eingang", "external", "extern"],
}

SAFETY_KEYWORDS = ["safety", "sicherheitsrelevant", "critical", "kritisch"]

UNITS = {
    "°c": "Cel", "c": "Cel", "degc": "Cel",
    "k": "K",
    "°f": "[degF]", "f": "[degF]",
    "%": "%",
}

# Physical sanity envelopes, independent of what a datasheet claims.
# Used to catch wrong scale factors and wrong units.
PLAUSIBLE = {
    ("Temperature", "Cel"): (-60.0, 200.0),
    ("RelativeHumidity", "%"): (0.0, 100.0),
    ("BatteryLevel", "%"): (0.0, 100.0),
}

# Tighter "typical" envelopes per role, for when the datasheet range is missing
# or suspicious.
TYPICAL = {
    ("Temperature", "room"): (5.0, 40.0),
    ("Temperature", "outdoor"): (-45.0, 50.0),
    ("Temperature", "flow"): (10.0, 95.0),
}


def convert(value: float, from_unit: str, to_unit: str) -> float:
    if from_unit == to_unit:
        return value
    to_c = {"Cel": lambda v: v, "K": lambda v: v - 273.15, "[degF]": lambda v: (v - 32) * 5 / 9}
    from_c = {"Cel": lambda v: v, "K": lambda v: v + 273.15, "[degF]": lambda v: v * 9 / 5 + 32}
    if from_unit in to_c and to_unit in from_c:
        return from_c[to_unit](to_c[from_unit](value))
    raise ValueError(f"no conversion {from_unit} -> {to_unit}")


def convertible(a: str, b: str) -> bool:
    try:
        convert(1.0, a, b)
        return True
    except ValueError:
        return False


def _hits(text: str, words: list[str]) -> bool:
    import re
    tokens = set(re.findall(r"[a-zäöüß]+", text.lower()))
    joined = text.lower()
    return any(w in tokens or (len(w) >= 4 and w in joined) for w in words)


def classify(text: str) -> tuple[str | None, list[str]]:
    qk = next((k for k, ws in QUANTITY_KEYWORDS.items() if _hits(text, ws)), None)
    roles = [r for r, ws in ROLE_KEYWORDS.items() if _hits(text, ws)]
    return qk, roles


def is_safety_critical(text: str) -> bool:
    return _hits(text, SAFETY_KEYWORDS)
