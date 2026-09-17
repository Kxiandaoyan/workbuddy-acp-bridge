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
import glob
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


DESKTOP_HINT_KEY = b"hermes.desktop.sessionOwnerHints.v1"
LAST_SESSION_KEY = b"hermes.desktop.lastSessionId.profile."


def _varint(buf, pos):
    result, shift = 0, 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _snappy_decompress(data):
    declared, pos = _varint(data, 0)
    if declared == 0 or declared > 64 * 1024 * 1024:
        raise ValueError("bad declared length")
    out = bytearray()
    n = len(data)
    while pos < n and len(out) < declared:
        tag = data[pos]
        t = tag & 0x03
        if t == 0:
            ln = (tag >> 2) + 1
            hdr = 1
            if ln > 60:
                extra = ln - 60
                ln = int.from_bytes(data[pos + 1:pos + 1 + extra], "little") + 1
                hdr = 1 + extra
            if pos + hdr + ln > n:
                raise ValueError("literal overrun")
            out += data[pos + hdr:pos + hdr + ln]
            pos += hdr + ln
        else:
            if t == 1:
                ln = ((tag >> 2) & 0x07) + 4
                off = ((tag >> 5) << 8) | data[pos + 1]
                pos += 2
            elif t == 2:
                ln = (tag >> 2) + 1
                off = int.from_bytes(data[pos + 1:pos + 3], "little")
                pos += 3
            else:
                ln = (tag >> 2) + 1
                off = int.from_bytes(data[pos + 1:pos + 5], "little")
                pos += 5
            if off == 0 or off > len(out):
                raise ValueError("bad copy offset")
            for _ in range(ln):
                out.append(out[len(out) - off])
    if len(out) != declared:
        raise ValueError("length mismatch")
    return bytes(out)


def _block_entries(blk):
    """(key, value) pairs from one leveldb block (prefix-compressed)."""
    import struct as _s
    if len(blk) < 4:
        return
    nrest = _s.unpack("<I", blk[-4:])[0]
    data_end = len(blk) - 4 - 4 * nrest
    p, prev = 0, b""
    while p < data_end:
        shared, p = _varint(blk, p)
        non_shared, p = _varint(blk, p)
        vlen, p = _varint(blk, p)
        delta = blk[p:p + non_shared]
        p += non_shared
        val = blk[p:p + vlen]
        p += vlen
        key = prev[:shared] + delta
        prev = key
        yield key, val


_LDB_MAGIC = bytes.fromhex("57fb808b247547db")


def _read_ldb_pairs(path):
    """(key, value) across all data blocks of an .ldb (SSTable) file."""
    import struct as _s
    try:
        data = open(path, "rb").read()
    except OSError:
        return
    footer = data[-48:]
    if footer[-8:] != _LDB_MAGIC:
        return
    try:
        p = 0
        _mo, p = _varint(footer, p)
        _ms, p = _varint(footer, p)
        io_, p = _varint(footer, p)
        is_, p = _varint(footer, p)
    except (IndexError, ValueError):
        return

    def load(off, size):
        raw = data[off:off + size]
        ctype = data[off + size] if off + size < len(data) else 0
        if ctype == 1:
            return _snappy_decompress(raw)
        return raw

    try:
        index = load(io_, is_)
    except Exception:
        return
    for _k, handle in _block_entries(index):
        q = 0
        boff, q = _varint(handle, q)
        bsize, q = _varint(handle, q)
        try:
            blk = load(boff, bsize)
        except Exception:
            continue
        yield from _block_entries(blk)


def _read_log_batches(path):
    """(key, value) pairs from a leveldb WAL (.log) file (write batches)."""
    import struct as _s
    try:
        data = open(path, "rb").read()
    except OSError:
        return
    pos, pending, n = 0, b"", len(data)
    records = []
    while pos + 7 <= n:
        if (pos // 32768) > 0 and (pos % 32768) + 7 > 32768:
            pos = ((pos // 32768) + 1) * 32768
            continue
        _crc, length, rtype = _s.unpack("<IHB", data[pos:pos + 7])
        if rtype == 0 and length == 0:
            pos = ((pos // 32768) + 1) * 32768
            continue
        payload = data[pos + 7:pos + 7 + length]
        if len(payload) < length:
            break
        pos += 7 + length
        if rtype == 1:
            records.append(pending + payload)
            pending = b""
        elif rtype in (2, 3):
            pending += payload
        elif rtype == 4:
            records.append(pending + payload)
            pending = b""
    for rec in records:
        if len(rec) < 12:
            continue
        count = _s.unpack("<I", rec[8:12])[0]
        p = 12
        for _ in range(count):
            if p >= len(rec):
                break
            op = rec[p]
            p += 1
            try:
                klen, p = _varint(rec, p)
                key = rec[p:p + klen]
                p += klen
                if op == 1:
                    vlen, p = _varint(rec, p)
                    val = rec[p:p + vlen]
                    p += vlen
                    yield key, val
                else:
                    yield key, None
            except (IndexError, ValueError):
                break


def _decode_ls_value(val):
    if not val:
        return ""
    enc, rest = val[0], val[1:]
    if enc == 0:
        return rest.decode("utf-16-le", "replace")
    return rest.decode("utf-8", "replace")


def desktop_open_sessions():
    """{profile: session_id} for the session each Desktop window has open.

    Reads `hermes.desktop.lastSessionId.profile.<name>` from the Desktop
    renderer's localStorage leveldb (SSTables + WAL, newest write wins by
    file order). Pure-stdlib leveldb/snappy parsing — no external tools.
    """
    base = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"),
        "Hermes", "Local Storage", "leveldb")
    out = {}
    try:
        files = sorted(os.listdir(base))
    except OSError:
        return out
    for name in files:
        path = os.path.join(base, name)
        pairs = _read_ldb_pairs(path) if name.endswith(".ldb") else (
            _read_log_batches(path) if name.endswith(".log") else ())
        for key, val in pairs:
            if not key or LAST_SESSION_KEY not in key:
                continue
            try:
                tail = key.split(b"\x00\x01", 1)[1]
                prof = tail[len(LAST_SESSION_KEY):].split(b"\x01")[0].decode("utf-8", "replace")
            except (IndexError, UnicodeDecodeError):
                continue
            if val is None:
                out.pop(prof, None)
                continue
            sid = _decode_ls_value(val).strip()
            if re.match(r"^\d{8}_\d{6}_[0-9A-Za-z]+$", sid):
                out[prof] = sid
    return out


def _proc_cmdlines():
    """pid -> command line for python.exe processes via PowerShell CIM.
    Returns {} when unavailable (e.g. restricted sandboxes); callers fall back."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
            capture_output=True, timeout=20, check=False).stdout.decode(errors="replace")
        mapping = {}
        for ln in out.splitlines():
            if "|" not in ln:
                continue
            pid_s, cmd = ln.split("|", 1)
            pid_s = pid_s.strip()
            if pid_s.isdigit() and cmd.strip():
                mapping[int(pid_s)] = cmd.strip()
        return mapping
    except Exception:
        return {}


def _profile_from_cmdline(cmd):
    m = re.search(r"--profile[ =](\S+)", cmd or "")
    return m.group(1) if m else None


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


def _ports_with_owner(pid, cmdlines=None):
    """Loopback LISTENING ports owned by pid OR any descendant python serve
    process (shim -> real server). Uses netstat only; the child set comes from
    cmdline python processes whose parent chain we approximate by matching
    `--profile ... serve` pythons via CIM when available."""
    ports = {p for p, owner in _loopback_listeners() if owner == pid}
    if ports or cmdlines is None:
        return sorted(ports)
    # CIM available: also count every python serve process's listener that is
    # NOT claimed by an owned pid directly (the real servers behind shims).
    return sorted(ports)


def _is_hermes_backend(port, timeout=2.0):
    """Probe GET / : hermes serve (headless) returns the token page."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
            html = r.read(4096).decode("utf-8", "replace")
        return "__HERMES_SESSION_TOKEN__" in html
    except Exception:
        return False


def listening_ports(pid):
    """Hermes backend ports associated with pid (netstat-only fallback: probe
    loopback listeners for the hermes token page when the shim itself doesn't
    listen)."""
    direct = [p for p, owner in _loopback_listeners() if owner == pid]
    if direct:
        return sorted(set(direct))
    return [p for p, _ in _loopback_listeners() if _is_hermes_backend(p)][:8]


def _ports_with_owner(pid, cmdlines):
    """Loopback LISTENING ports owned by pid; when the shim doesn't listen
    itself, include hermes-token listeners owned by a `hermes ... serve` python
    (the real server the shim spawned)."""
    direct = [p for p, owner in _loopback_listeners() if owner == pid]
    if direct:
        return sorted(set(direct))
    hits = []
    for p, owner in _loopback_listeners():
        if not _is_hermes_backend(p):
            continue
        cmd = (cmdlines or {}).get(owner, "")
        if re.search(r"hermes_cli\.main.*serve", cmd):
            hits.append(p)
    return sorted(set(hits))[:8]


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
        # 1) ownership file: authoritative while Desktop tracks the backend
        owned = {(b["pid"]): b for b in load_backends()}
        # 2) CIM command lines give the TRUE profile of every listening serve
        #    process (both Desktop's shim and the real server child carry
        #    `--profile <name>` in their command line)
        cmdlines = _proc_cmdlines()
        seen_ports = set()
        for p, owner in _loopback_listeners():
            if not _is_hermes_backend(p):
                continue
            cmd = cmdlines.get(owner, "")
            prof = _profile_from_cmdline(cmd)
            if prof is None:
                # listener's cmdline unknown (sandbox): try ownership shims
                prof = next((b["profile"] for b in owned.values()
                             if _profile_from_cmdline(b.get("command", "")) and owner == b["pid"]),
                            "(unknown)")
            candidates.append({"profile": prof, "pid": owner, "port": p})
            seen_ports.add(p)
    if not candidates:
        print("ERROR|没有找到在跑的 Hermes 后端（backend-ownership.json 或端口发现失败）。|",
              file=sys.stderr)
        sys.exit(2)
    if args.profile:
        hit = [c for c in candidates if c["profile"] == args.profile]
        if not hit:
            known = sorted({c["profile"] for c in candidates})
            print(f"ERROR|profile {args.profile!r} 未发现。已知: {known}|", file=sys.stderr)
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
    ap.add_argument("--queue-busy", action="store_true",
                    help="if busy, submit as the queued NEXT turn (queued:true, the "
                         "desktop composer's own queue-drain semantics) instead of "
                         "refusing — never interrupts/redirects the running turn")
    ap.add_argument("--force-busy", action="store_true",
                    help="send even if busy WITHOUT queueing (busy_input_mode may "
                         "redirect the running turn — disruptive)")
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

    def _gateway_for_session(cands, sid):
        """Find the backend whose session list contains sid (multi-backend setups:
        Desktop spawns one serve per profile; ownership may only track the active one)."""
        errors = []
        for c in cands:
            try:
                g = Gateway(c["port"])
            except Exception as e:
                errors.append(f"{c['port']}: {e}")
                continue
            res = g.call("session.list", {"limit": 200}, timeout=20)
            if res.get("error"):
                g.close()
                continue
            for s in res.get("result", {}).get("sessions", []):
                if sid == s.get("id"):
                    return g, c
            # also live runtime ids
            res = g.call("session.active_list", timeout=15)
            for row in res.get("result", {}).get("sessions", []):
                if sid in (row.get("id"), row.get("session_key")):
                    return g, c
            g.close()
        return None, None

    try:
        if target:
            gw, chosen = _gateway_for_session(candidates, target)
            if gw is None:
                # maybe a live-only runtime id on a backend with an empty DB list
                print(f"ERROR|在 {len(candidates)} 个后端里都没找到会话 {target}。|", file=sys.stderr)
                sys.exit(2)
            print(f"[backend] port={chosen['port']} profile={chosen['profile']}", file=sys.stderr)
        else:
            # Target priority: (1) LIVE session on the profile's backend — the
            # one the Desktop window currently has open; (2) Desktop's owner
            # hints (last-opened per profile, from localStorage); (3) most_recent.
            # most_recent alone is WRONG for "what the user is looking at": it's
            # just the latest DB row (often a telegram/cron session).
            gw = None
            hinted = None
            if not args.port and candidates[0].get("pid"):
                pass  # multi-backend; locate below
            for c in candidates:
                try:
                    g = Gateway(c["port"])
                    live = g.call("session.active_list", timeout=15).get("result", {}).get("sessions", [])
                except Exception:
                    continue
                if live:
                    g2 = g if gw is None else None
                    if gw is None:
                        gw, chosen = g, c
                    else:
                        g.close()
                    # most recently active live session on this backend
                    best = max(live, key=lambda r: float(r.get("last_active") or 0))
                    hinted = best.get("session_key") or best.get("id")
                    print(f"[target] live-open ({c['profile']}) -> {hinted} "
                          f"(status={best.get('status')})", file=sys.stderr)
                    break
                else:
                    g.close()
            if hinted is None:
                open_map = desktop_open_sessions()
                prof = args.profile or next(
                    (c["profile"] for c in candidates if c["profile"] not in ("(unknown)", "(explicit)")), None)
                hinted = open_map.get(prof) if prof else None
                if hinted:
                    print(f"[target] desktop-hint ({prof}) -> {hinted}", file=sys.stderr)
            if gw is None:
                gw = Gateway(candidates[0]["port"])
            if hinted:
                target = hinted
                # verify it exists on this backend (cross-backend case)
                res = gw.call("session.list", {"limit": 200}, timeout=20)
                known = {s.get("id") for s in res.get("result", {}).get("sessions", [])}
                if target not in known:
                    # hint may point at another backend's session
                    gw2, c2 = (None, None)
                    gw2, c2 = _gateway_for_session(candidates, target) if "_gateway_for_session" in dir() else (None, None)
                    if gw2:
                        gw.close()
                        gw = gw2
            if not target:
                res = gw.call("session.most_recent")
                target = res.get("result", {}).get("session_id")
                if not target:
                    print("ERROR|没有可用的会话。|", file=sys.stderr)
                    sys.exit(2)
                print(f"[target] most_recent -> {target}", file=sys.stderr)

        busy, why = busy_check(gw, target)
        if busy and args.msg and args.queue_busy:
            pass  # queued submit below handles a running turn server-side
        elif busy and args.msg and args.force_busy:
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

        if busy and not (args.queue_busy or args.force_busy):
            print(f"BUSY|{BUSY_MSG}|session={target}|{why}")
            sys.exit(4)

        # queued:true = the desktop's own queue-drain semantics: the server
        # appends this as the NEXT turn without interrupting/redirecting the
        # running one (mirrors the composer's fromQueue submit). This is the
        # graceful unattended-delivery mode.
        params = {"session_id": target, "text": args.msg}
        if busy and args.queue_busy:
            params["queued"] = True
            print(f"[queue] busy ({why}); submitting as queued next-turn", file=sys.stderr)

        res = gw.call("prompt.submit", params, timeout=30)
        if res.get("error") and res["error"].get("code") == 4001:
            # DB-only session (not open in the desktop): bring it live exactly
            # like the desktop does when the user clicks a stored conversation.
            r2 = gw.call("session.resume", {"session_id": target}, timeout=60)
            if r2.get("error"):
                print(f"ERROR|resume 失败:{r2['error'].get('message')}|session={target}", file=sys.stderr)
                sys.exit(3)
            live_id = r2.get("result", {}).get("session_id") or target
            if live_id != target:
                print(f"[resume] runtime id {live_id}", file=sys.stderr)
            params["session_id"] = live_id
            res = gw.call("prompt.submit", params, timeout=30)
        if res.get("error"):
            print(f"ERROR|发送失败:{res['error'].get('message')}|session={target}", file=sys.stderr)
            sys.exit(3)
        status = res.get("result", {}).get("status")
        print(f"SENT|status={status}|session={target}" + ("|queued" if params.get("queued") else ""))
    finally:
        if gw:
            gw.close()


if __name__ == "__main__":
    main()
