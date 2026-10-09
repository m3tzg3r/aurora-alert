"""
Govee LAN streaming over the razer/DreamView protocol (UDP multicast).

Razer mode drops out after ~60s without packets, so stream continuously.
The enable packet is re-sent every ENABLE_EVERY seconds because multicast
can drop it.
"""
import base64
import json
import os
import socket
import time

GROUP_ADDR = "239.255.255.250"
GROUP_PORT = 4001
TTL = 2
ENABLE_PT = "uwABsQEK"  # bb 00 01 b1 01 0a
ENABLE_EVERY = 3.0      # seconds


def build_payload(zones):
    """bb 00 <len> b0 00 <n> [n x RGB] <xor>, base64-encoded."""
    body = bytearray([0x00, len(zones)])
    for r, g, b in zones:
        body += bytes((r & 0xFF, g & 0xFF, b & 0xFF))
    pkt = bytearray([0xBB, 0x00, len(body), 0xB0]) + body
    chk = 0
    for x in pkt:
        chk ^= x
    pkt.append(chk)
    return base64.b64encode(bytes(pkt)).decode()


class Panel:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, TTL)
        self.last_enable = 0.0

    def send(self, cmd, data=None):
        msg = json.dumps({"msg": {"cmd": cmd, "data": data or {}}}).encode()
        self.sock.sendto(msg, (GROUP_ADDR, GROUP_PORT))

    def start(self, brightness=100):
        self.send("turn", {"value": 1})
        time.sleep(0.5)
        self.send("brightness", {"value": brightness})
        time.sleep(0.2)
        self.enable()
        time.sleep(0.2)

    def enable(self):
        self.send("razer", {"pt": ENABLE_PT})
        self.last_enable = time.time()

    def frame(self, zones):
        if time.time() - self.last_enable >= ENABLE_EVERY:
            self.enable()
        self.send("razer", {"pt": build_payload(zones)})

    def close(self):
        self.sock.close()


class Claim:
    """Shared claim file so scripts driving the same lights take turns:
    the most recent start wins. path=None disables it."""

    def __init__(self, path):
        self.path = os.path.expanduser(path) if path else None

    def write(self, ts):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.write(repr(ts))
        os.replace(tmp, self.path)

    def newer_than(self, ts):
        if not self.path:
            return None
        try:
            with open(self.path) as f:
                claim = float(f.read().strip())
        except (OSError, ValueError):
            return None
        return claim if claim > ts + 1e-6 else None
