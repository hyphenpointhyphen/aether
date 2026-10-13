"""Minimal Modbus TCP simulator for two fictional devices.

Implements function codes 3 (read holding registers), 6 (write single
register) and 16 (write multiple registers). Deliberately tiny and
dependency-free so the demo does not depend on a server library's API.

Devices
-------
RS-200 room sensor   (port 5020): room temperature is sent as raw*10,
                                  but its datasheet wrongly says scale 1.
HC-10 heating ctrl   (port 5021): German datasheet, external room
                                  temperature input in 0.5 °C steps,
                                  safety-critical flow setpoint.
"""
from __future__ import annotations

import asyncio
import math
import random
import struct
import time


def _s16(v: int) -> int:
    """Encode a signed value as an unsigned 16-bit register."""
    return v & 0xFFFF


class Device:
    def __init__(self, name: str, writable: set[int]):
        self.name = name
        self.regs: dict[int, int] = {}
        self.writable = writable

    def tick(self, t: float) -> None:  # pragma: no cover - overridden
        pass


class RoomSensor(Device):
    def __init__(self):
        super().__init__("RS-200", writable=set())

    def tick(self, t):
        temp = 21.5 + 0.4 * math.sin(t / 30) + random.uniform(-0.05, 0.05)
        hum = 45.0 + 3 * math.sin(t / 50) + random.uniform(-0.2, 0.2)
        self.regs[0] = _s16(round(temp * 10))   # datasheet claims scale 1 (error)
        self.regs[1] = round(hum * 10)          # scale 0.1, correct
        self.regs[2] = 87                       # battery %


class HeatingController(Device):
    def __init__(self):
        super().__init__("HC-10", writable={110, 120})
        self.regs[110] = 40   # external room temp input, 0.5 °C steps -> 20.0 °C
        self.regs[120] = 450  # flow setpoint, 0.1 °C -> 45.0 °C

    def tick(self, t):
        outdoor = -3.0 + 2 * math.sin(t / 60) + random.uniform(-0.1, 0.1)
        setpoint = self.regs[120] / 10
        flow = setpoint - 1.5 + random.uniform(-0.3, 0.3)
        self.regs[100] = _s16(round(flow * 10))
        self.regs[101] = _s16(round(outdoor * 10))


DEVICES = {5020: RoomSensor, 5021: HeatingController}


async def _handle(dev: Device, reader, writer):
    try:
        while True:
            header = await reader.readexactly(7)
            tid, pid, length, unit = struct.unpack(">HHHB", header)
            pdu = await reader.readexactly(length - 1)
            fc = pdu[0]
            dev.tick(time.monotonic())
            resp: bytes
            if fc == 3:
                addr, count = struct.unpack(">HH", pdu[1:5])
                if any(a not in dev.regs for a in range(addr, addr + count)):
                    resp = bytes([fc | 0x80, 2])
                else:
                    vals = [dev.regs[a] for a in range(addr, addr + count)]
                    resp = struct.pack(">BB", fc, 2 * count) + struct.pack(f">{count}H", *vals)
            elif fc == 6:
                addr, val = struct.unpack(">HH", pdu[1:5])
                if addr not in dev.writable:
                    resp = bytes([fc | 0x80, 2])
                else:
                    dev.regs[addr] = val
                    resp = pdu[:5]
            elif fc == 16:
                addr, count, _ = struct.unpack(">HHB", pdu[1:6])
                vals = struct.unpack(f">{count}H", pdu[6:6 + 2 * count])
                if any(a not in dev.writable for a in range(addr, addr + count)):
                    resp = bytes([fc | 0x80, 2])
                else:
                    for i, v in enumerate(vals):
                        dev.regs[addr + i] = v
                    resp = struct.pack(">BHH", fc, addr, count)
            else:
                resp = bytes([fc | 0x80, 1])
            writer.write(struct.pack(">HHHB", tid, pid, len(resp) + 1, unit) + resp)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()


async def serve(host: str = "127.0.0.1", ports=(5020, 5021)) -> list[asyncio.base_events.Server]:
    servers = []
    for port in ports:
        dev = DEVICES[port]()
        dev.tick(time.monotonic())
        srv = await asyncio.start_server(lambda r, w, d=dev: _handle(d, r, w), host, port)
        servers.append(srv)
    return servers


def run_forever(host: str = "127.0.0.1") -> None:
    async def main():
        servers = await serve(host)
        for s in servers:
            print(f"simulator listening on {s.sockets[0].getsockname()}")
        await asyncio.gather(*(s.serve_forever() for s in servers))

    asyncio.run(main())


def start_in_thread(host: str = "127.0.0.1") -> None:
    """Start the simulator in a daemon thread (used by `ail demo` and tests)."""
    import threading

    ready = threading.Event()

    def target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(serve(host))
        ready.set()
        loop.run_forever()

    threading.Thread(target=target, daemon=True).start()
    ready.wait(5)
