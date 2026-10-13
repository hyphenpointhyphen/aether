"""Step 1: draft a WoT-style Thing Description from a datasheet.

Two drafters share one output format:

* HeuristicDrafter  - parses a Markdown register table, tags semantics from a
                      small keyword vocabulary. Offline, deterministic.
* LLMDrafter        - asks an LLM to produce the same property list from free
                      text (PDF excerpts, scanned tables, prose). Optional.

Either way the result is only a *draft*: every property carries
`ail:provenance` and starts as `ail:verification.status = "unverified"`.
Nothing downstream trusts a draft until `ail verify` has checked it against
the live device.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from . import vocab

HEADER_ALIASES = {
    "register": ["register", "reg", "address", "adresse"],
    "name": ["name", "bezeichnung"],
    "description": ["description", "beschreibung", "desc"],
    "type": ["type", "typ", "datatype", "datentyp"],
    "scale": ["scale", "faktor", "factor", "multiplier"],
    "unit": ["unit", "einheit"],
    "access": ["access", "zugriff", "r/w"],
    "range": ["range", "bereich"],
    "note": ["note", "hinweis", "notes", "bemerkung"],
}


def _norm_header(h: str) -> str | None:
    h = h.strip().lower()
    return next((k for k, al in HEADER_ALIASES.items() if h in al), None)


def parse_markdown_table(text: str) -> list[dict]:
    rows, headers = [], None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            headers = None  # a non-table line ends the current table
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if headers is None:
            headers = [_norm_header(c) for c in cells]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append({h: c for h, c in zip(headers, cells) if h})
    return rows


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _range(s: str) -> tuple[float, float] | None:
    m = re.match(r"\s*(-?[\d.]+)\s*(?:\.\.|–|-|to|bis)\s*(-?[\d.]+)\s*$", s or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


class HeuristicDrafter:
    name = "heuristic-v1"

    def properties(self, text: str) -> list[dict]:
        out = []
        for row in parse_markdown_table(text):
            if not row.get("register", "").isdigit():
                continue
            label = " ".join(row.get(k, "") for k in ("name", "description", "note"))
            qk, roles = vocab.classify(label)
            unit = vocab.UNITS.get(row.get("unit", "").strip().lower(), row.get("unit") or None)
            rng = _range(row.get("range", ""))
            confidence = 0.9 if qk and roles else 0.6 if qk else 0.3
            out.append({
                "key": _slug(row.get("name") or f"reg_{row['register']}"),
                "title": row.get("description") or row.get("name"),
                "register": int(row["register"]),
                "dtype": (row.get("type") or "uint16").lower(),
                "scale": float(row.get("scale") or 1),
                "unit": unit,
                "writable": "w" in row.get("access", "r").lower(),
                "range": rng,
                "quantityKind": qk,
                "roles": roles,
                "safetyCritical": vocab.is_safety_critical(label),
                "confidence": confidence,
            })
        return out


LLM_PROMPT = """You convert device documentation into a JSON list of Modbus holding-register properties.
Return ONLY a JSON array. Each element:
{"key": snake_case id, "title": str, "register": int, "dtype": "int16"|"uint16",
 "scale": float, "unit": "Cel"|"K"|"[degF]"|"%"|null, "writable": bool,
 "range": [min, max] | null, "quantityKind": "Temperature"|"RelativeHumidity"|"BatteryLevel"|null,
 "roles": subset of ["room","outdoor","flow","setpoint","input"],
 "safetyCritical": bool, "confidence": 0..1}
Copy values from the documentation; do not guess missing scale factors - set confidence low instead.

Documentation:
"""


class LLMDrafter:
    """Optional. Requires `pip install anthropic` and ANTHROPIC_API_KEY."""

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("AIL_LLM_MODEL", "claude-sonnet-5-5")
        self.name = f"llm:{self.model}"

    def properties(self, text: str) -> list[dict]:
        import anthropic  # imported lazily so the MVP runs without it

        msg = anthropic.Anthropic().messages.create(
            model=self.model,
            max_tokens=4000,
            messages=[{"role": "user", "content": LLM_PROMPT + text}],
        )
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        raw = raw[raw.find("["): raw.rfind("]") + 1]
        props = json.loads(raw)
        for p in props:  # LLM output is untrusted: normalise and clamp
            p["confidence"] = min(float(p.get("confidence", 0.5)), 0.8)
            p["range"] = tuple(p["range"]) if p.get("range") else None
        return props


def to_thing_description(title: str, endpoint: str, props: list[dict], source: str, drafter: str) -> dict:
    """Build a WoT Thing Description (subset) with `ail:` extension fields."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    endpoint = endpoint.rstrip("/")
    td = {
        "@context": ["https://www.w3.org/2022/wot/td/v1.1", {"ail": "https://example.org/ail#"}],
        "@type": "Thing",
        "title": title,
        "id": f"urn:ail:thing:{_slug(title)}",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": "nosec_sc",
        "properties": {},
    }
    for p in props:
        prop = {
            "title": p["title"],
            "type": "number",
            "readOnly": not p["writable"],
            "unit": p["unit"],
            "forms": [{
                "href": f"{endpoint}/{p['register']}?quantity=1",
                "op": ["readproperty", "writeproperty"] if p["writable"] else ["readproperty"],
                "modv:type": f"xsd:{'short' if p['dtype'] == 'int16' else 'unsignedShort'}",
            }],
            "ail:dtype": p["dtype"],
            "ail:scale": p["scale"],
            "ail:quantityKind": p["quantityKind"],
            "ail:roles": p["roles"],
            "ail:safetyCritical": p["safetyCritical"],
            "ail:provenance": {"source": source, "drafter": drafter, "confidence": p["confidence"], "at": now},
            "ail:verification": {"status": "unverified"},
        }
        if p.get("range"):
            prop["minimum"], prop["maximum"] = p["range"]
        td["properties"][p["key"]] = prop
    return td


def draft(datasheet_path: str, endpoint: str, title: str | None = None, use_llm: bool = False) -> dict:
    with open(datasheet_path, encoding="utf-8") as f:
        text = f.read()
    drafter = LLMDrafter() if use_llm else HeuristicDrafter()
    props = drafter.properties(text)
    if not props:
        raise ValueError(f"no register rows found in {datasheet_path}")
    if title is None:
        first = next((l for l in text.splitlines() if l.startswith("#")), "device")
        title = first.lstrip("# ").split("—")[0].split(" - ")[0].strip()
    return to_thing_description(title, endpoint, props, os.path.basename(datasheet_path), drafter.name)
