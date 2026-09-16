#!/usr/bin/env python3
"""Hermes Desktop bridge: send a message into a session of a running `hermes serve`
backend (the process the Hermes Desktop app spawns per profile), with busy detection.

Conventions match the WorkBuddy ACP bridge (acp_live_send.py) and ZCode bridge
(zcode_send.py):
  --list            list backends/profiles and their sessions
  --check           busy-check only; prints IDLE|... (exit 0) or BUSY|... (exit 4)
  --msg TEXT        send via prompt.submit over the /api/ws JSON-RPC websocket
  --wait-idle SEC   if busy, poll up to SEC seconds before giving up

Discovery (zero config):
  1. %APPDATA%/Hermes/backend-ownership.json -> per-profile serve PIDs
  2. each PID (and its python child, shim->real server) -> loopback LISTENING port
  3. GET http://127.0.0.1:<port>/ -> window.__HERMES_SESSION_TOKEN__
  4. WS /api/ws?token=... -> JSON-RPC: session.list / session.most_recent /
     session.active_list / prompt.submit

Busy semantics: prompt.submit obeys display.busy_input_mode (default `interrupt`
REDIRECTS the running turn). To avoid hijacking a turn the desktop user is
watching, we busy-check via session.active_list first and refuse with BUSY
unless --force-busy.
"""
import argparse
import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

OWNERSHIP = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "Hermes", "backend-ownership.json")
BUSY_MSG = "现在会话正忙，请稍后再发。"
IDLE_MSG = "会话空闲，可以发送。"
BUSY_STATES = {"streaming", "running", "thinking", "working"}


# ---------------------------------------------------------------- discovery

def load_backends():
    try:
        with open(OWNERSHIP, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    return [
        {"profile": b.get("profile") or "?", "pid": b.get("pid"), "command": b.get("command") or ""}
        for b in data.get("backends", [])
    ]


def _loopback_listeners():
    """[(port, pid)] of all loopback LISTENING sockets via netstat."""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=20,
                             check=False).stdout.decode(errors="replace")
    except Exception:
        return []
    out_list = []
    for ln in out.splitlines():
        m = re.match(r"\s*TCP\s+(\S+):(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", ln)
        if not m:
            continue
        addr, port, pid = m.group(1), int(m.group(2)), int(m.group(3))
        if addr in ("127.0.0.1", "::1", "0.0.0.0"):
            out_list.append((port, pid))
    return out_list


def _is_hermes_backend(port, timeout=2.0):
    """Probe GET / : hermes serve (headless) returns the token page."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
            html = r.read(4096).decode("utf-8", "replace")
        return "__HERMES_SESSION_TOKEN__" in html
    except Exception:
        return False


def listening_ports(pid):
    """Hermes backend ports associated with pid OR its serve child (netstat-only:
    wmic is gone on modern Windows and spawning powershell from python is blocked
    in some sandboxes; we scan listeners and probe instead)."""
    candidates = [p for p, owner in _loopback_listeners() if owner == pid]
    if candidates:
        return sorted(set(candidates))
    # shim does not listen itself: its python child does. Probe every loopback
    # listener for the hermes token page and return matches (cannot tie to pid
    # without CIM, but the token page IS the hermes fingerprint).
    return [p for p, _ in _loopback_listeners() if _is_hermes_backend(p)][:8]


def fetch_token(port, timeout=5.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(r'window\.__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"', html)
    if not m:
        raise RuntimeError("session token not found at GET / (gated backend?)")
    return m.group(1)


# ---------------------------------------------------------------- tiny WS client

class TinyWS:
    """RFC6455 text-frame WebSocket client (stdlib only)."""

    def __init__(self, host, port, path, timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0].decode(errors="replace")
        if " 101 " not in f" {status} ":
            raise RuntimeError(f"WS handshake failed: {status}")
        self.buf = rest

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ws closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_text(self, timeout):
        """One text frame payload, or None on timeout."""
        self.sock.settimeout(timeout)
        try:
            while True:
                b1, b2 = self._read_exact(2)
                opcode, masked, ln = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", self._read_exact(2))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", self._read_exact(8))[0]
                mask = self._read_exact(4) if masked else None
                data = self._read_exact(ln)
                if mask:
                    data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
                if opcode == 1:
                    return data.decode("utf-8", "replace")
                if opcode == 8:
                    raise ConnectionError("ws close frame")
        except socket.timeout:
            return None

    def send_text(self, text):
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        ln = len(payload)
        if ln < 126:
            header = struct.pack(">BB", 0x81, 0x80 | ln)
        elif ln < 65536:
            header = struct.pack(">BBH", 0x81, 0x80 | 126, ln)
        else:
            header = struct.pack(">BBQ", 0x81, 0x80 | 127, ln)
        self.sock.sendall(header + mask + masked)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Gateway:
    """JSON-RPC over TinyWS: drains event frames, matches replies by id."""

    def __init__(self, port, timeout=10.0):
        token = fetch_token(port)
        self.ws = TinyWS("127.0.0.1", port, f"/api/ws?token={urllib.parse.quote(token)}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.ws.recv_text(max(0.2, deadline - time.time()))
            if msg is None:
                continue
            obj = json.loads(msg)
            if obj.get("method") == "event" and obj.get("params", {}).get("type") == "gateway.ready":
                return
        raise TimeoutError("gateway.ready not received")

    _id = 0

    def call(self, method, params=None, timeout=20.0):
        Gateway._id += 1
        rid = Gateway._id
        self.ws.send_text(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.ws.recv_text(max(0.2, deadline - time.time()))
            if msg is None:
                continue
            obj = json.loads(msg)
            if obj.get("id") == rid and ("result" in obj or "error" in obj):
                return obj
        raise TimeoutError(f"no reply for {method} (id={rid})")

    def close(self):
        self.ws.close()


# ---------------------------------------------------------------- busy check

def busy_check(gw, session_id):
    """(busy, why) via session.active_list — live sessions expose status/inflight/queued."""
    res = gw.call("session.active_list")
    if res.get("error"):
        return False, f"active_list error: {res['error'].get('message')}"
    for row in res.get("result", {}).get("sessions", []):
        if session_id in (row.get("id"), row.get("session_key")):
            status = str(row.get("status") or "").lower()
            if status in BUSY_STATES:
                return True, f"status={status}"
            if row.get("inflight"):
                return True, "inflight present"
            if row.get("queued"):
                return True, "queued prompt"
            return False, f"status={status or 'idle'}"
    return False, "not-live (no running turn in gateway memory)"


# ---------------------------------------------------------------- main

def discover(args):
    candidates = []
    if args.port:
        candidates = [{"profile": "(explicit)", "pid": None, "port": args.port}]
    else:
        for b in load_backends():
            for p in listening_ports(b["pid"]):
                candidates.append({**b, "port": p})
    if not candidates:
        print("ERROR|没有找到在跑的 Hermes 后端（backend-ownership.json 或端口发现失败）。|",
              file=sys.stderr)
        sys.exit(2)
    if args.profile:
        hit = [c for c in candidates if c["profile"] == args.profile]
        if not hit:
            print(f"ERROR|profile {args.profile!r} 不在 backend-ownership.json 里。|", file=sys.stderr)
            sys.exit(2)
        candidates = hit
    return candidates


def main():
    ap = argparse.ArgumentParser(description="Send a message into a Hermes Desktop backend session")
    ap.add_argument("--port", type=int, default=None, help="backend port (default: auto-discover)")
    ap.add_argument("--profile", default=None, help="profile name (see backend-ownership.json); needs auto-discovery")
    ap.add_argument("--list", action="store_true", help="list backends and sessions")
    ap.add_argument("--session-id", default=None, help="target session id (from --list)")
    ap.add_argument("--msg", default=None, help="message text to send")
    ap.add_argument("--check", action="store_true", help="busy-check only")
    ap.add_argument("--wait-idle", type=int, default=None, metavar="SEC",
                    help="if busy, poll every 2s up to SEC seconds before giving up")
    ap.add_argument("--force-busy", action="store_true",
                    help="send even if busy (busy_input_mode may redirect the running turn)")
    args = ap.parse_args()

    candidates = discover(args)

    if args.list:
        for c in candidates:
            print(f"== profile={c['profile']} port={c['port']} pid={c.get('pid')}")
            try:
                gw = Gateway(c["port"])
                for s in gw.call("session.list", {"limit": 15}).get("result", {}).get("sessions", []):
                    print(f"   id={s.get('id')} source={s.get('source')} title={s.get('title')!r}")
                for row in gw.call("session.active_list").get("result", {}).get("sessions", []):
                    print(f"   [live] id={row.get('id')} status={row.get('status')!r} "
                          f"preview={str(row.get('preview'))[:60]!r}")
                gw.close()
            except Exception as e:
                print(f"   (gateway unreachable: {e})")
        return

    if not (args.check or args.msg):
        ap.print_help()
        return

    target = args.session_id
    gw = None
    try:
        gw = Gateway(candidates[0]["port"])
        if not target:
            res = gw.call("session.most_recent")
            target = res.get("result", {}).get("session_id")
            if not target:
                print("ERROR|没有可用的会话。|", file=sys.stderr)
                sys.exit(2)
            print(f"[target] most_recent -> {target}", file=sys.stderr)

        busy, why = busy_check(gw, target)
        if busy and args.msg and args.force_busy:
            print(f"[warn] busy ({why}) but --force-busy set; sending anyway", file=sys.stderr)
        elif busy and args.wait_idle:
            deadline = time.time() + args.wait_idle
            while time.time() < deadline and busy:
                time.sleep(2)
                busy, why = busy_check(gw, target)

        if args.check:
            if busy:
                print(f"BUSY|{BUSY_MSG}|session={target}|{why}")
                sys.exit(4)
            print(f"IDLE|{IDLE_MSG}|session={target}|{why}")
            return

        if busy:
            print(f"BUSY|{BUSY_MSG}|session={target}|{why}")
            sys.exit(4)

        res = gw.call("prompt.submit", {"session_id": target, "text": args.msg}, timeout=30)
        if res.get("error"):
            print(f"ERROR|发送失败:{res['error'].get('message')}|session={target}", file=sys.stderr)
            sys.exit(3)
        print(f"SENT|status={res.get('result', {}).get('status')}|session={target}")
    finally:
        if gw:
            gw.close()


if __name__ == "__main__":
    main()
