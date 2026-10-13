"""Thin protocol access layer. MVP: Modbus TCP only (via pymodbus).

Forms follow the shape of the W3C WoT Modbus binding:
    href = "modbus+tcp://<host>:<port>/<unit_id>/<address>?quantity=<n>"
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

from pymodbus.client import ModbusTcpClient


@dataclass(frozen=True)
class ModbusRef:
    host: str
    port: int
    unit: int
    address: int
    quantity: int = 1

    @classmethod
    def parse(cls, href: str) -> "ModbusRef":
        u = urlparse(href)
        if u.scheme != "modbus+tcp":
            raise ValueError(f"unsupported scheme in {href!r} (MVP supports modbus+tcp)")
        parts = [p for p in u.path.split("/") if p]
        unit, address = int(parts[0]), int(parts[1])
        qty = int(parse_qs(u.query).get("quantity", ["1"])[0])
        return cls(u.hostname or "127.0.0.1", u.port or 502, unit, address, qty)


class Bus:
    """Caches one client per endpoint."""

    def __init__(self, timeout: float = 2.0):
        self._clients: dict[tuple[str, int], ModbusTcpClient] = {}
        self.timeout = timeout

    def _client(self, ref: ModbusRef) -> ModbusTcpClient:
        key = (ref.host, ref.port)
        c = self._clients.get(key)
        if c is None or not c.connected:
            c = ModbusTcpClient(ref.host, port=ref.port, timeout=self.timeout, retries=1)
            if not c.connect():
                raise ConnectionError(f"cannot connect to {ref.host}:{ref.port}")
            self._clients[key] = c
        return c

    def read_raw(self, href: str) -> list[int]:
        ref = ModbusRef.parse(href)
        rr = self._client(ref).read_holding_registers(ref.address, count=ref.quantity, device_id=ref.unit)
        if rr.isError():
            raise IOError(f"read failed at {href}: {rr}")
        return list(rr.registers)

    def write_raw(self, href: str, value: int) -> None:
        ref = ModbusRef.parse(href)
        rr = self._client(ref).write_register(ref.address, value & 0xFFFF, device_id=ref.unit)
        if rr.isError():
            raise IOError(f"write failed at {href}: {rr}")

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
        self._clients.clear()


def decode(raw: list[int], dtype: str, scale: float) -> float:
    v = raw[0]
    if dtype == "int16" and v >= 0x8000:
        v -= 0x10000
    return round(v * scale, 6)


def encode(value: float, dtype: str, scale: float) -> int:
    raw = round(value / scale)
    lo, hi = (-0x8000, 0x7FFF) if dtype == "int16" else (0, 0xFFFF)
    if not lo <= raw <= hi:
        raise ValueError(f"value {value} does not fit {dtype} at scale {scale}")
    return raw & 0xFFFF
