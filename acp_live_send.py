#!/usr/bin/env python
"""
acp_live_send.py v2 -- Send a message into a LIVE WorkBuddy PC-client conversation
so it appears in the PC client UI in REAL TIME.

Architecture (verified 2026-09-16, from app.asar forensics): the PC client's
daemon (daemon-app-server-entry.js, child of WorkBuddy.exe) owns "interactive"
ACP sessions from its prewarm pool. POST /api/v1/acp/connect is loopback-exempt
(no password); session/prompt on that endpoint streams through the daemon to the
UI (wb:event -> main -> renderer).

v2 changes (2026-09-18, after "double answer + stall + low success rate"
forensics on a 42MB session):
  1. NO session/load before prompt. The interactive worker already has the
     session loaded; session/load replays the whole history through the daemon
     (HistoryLoaded / RequestsChanged full-snapshot re-render) which the user
     sees as the agent "answering twice", and it stalls the daemon on huge
     transcripts (Empty-stream timeouts -> error-recovery retries). The PC
     client itself never loads before prompting (promptWithSessionResume).
  2. Health-gated port discovery: every candidate port is probed with a real
     POST /api/v1/acp/connect before use; stale ports (session recycled
     minutes ago, registry row still present) answer 502 / refuse.
  3. SSE early exit: return as soon as the turn is confirmed started (first
     session_update frame). The UI has its own event stream; holding ours open
     for the whole turn (up to 600s) only added load and timeouts.
  4. Fallback: if the worker freshly recycled and genuinely lost the session
     (prompt returns "not found"), do ONE session/load then retry once.

Usage:
  python acp_live_send.py --list / --list-probe
  python acp_live_send.py --session-id <uuid> --msg "hello"
  python acp_live_send.py --session-id <uuid> --msg "hello" --ensure
  python acp_live_send.py --session-id <uuid> --check
  python acp_live_send.py --session-id <uuid> --msg "..." --wait-reply   # hold stream to turn end
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request, urllib.error

SESSIONS_DIR = os.path.expanduser("~/.workbuddy/sessions")

# ---------------------------------------------------------------- discovery

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _netstat_snapshot():
    """ONE netstat run -> {pid(str): [127.0.0.1 listen ports]}."""
    import tempfile
    snap = {}
    tmp = None
    out = ""
    try:
        tmp = tempfile.NamedTemporaryFile(mode="w+", suffix=".netstat", delete=False)
        tmp.close()
        with open(tmp.name, "w", encoding="utf-8", errors="replace") as fh:
            subprocess.run(["netstat", "-ano"], stdout=fh, timeout=15)
        with open(tmp.name, "r", encoding="utf-8", errors="replace") as fh:
            out = fh.read()
    except Exception:
        return snap
    finally:
        if tmp:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
    if not out:
        return snap
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            m = re.match(r"127\.0\.0\.1:(\d+)", parts[1])
            if m:
                snap.setdefault(parts[4], []).append(int(m.group(1)))
    return snap

def _probe_acp(port, timeout=4):
    """True when POST /api/v1/acp/connect answers 200 on this port."""
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/v1/acp/connect" % port,
        data=b"", method="POST",
        headers={"Content-Type": "application/json", "x-codebuddy-request": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False

def list_interactive_sessions(probe=False):
    """Live interactive sessions; probe=True health-checks each port via ACP."""
    out = []
    if not os.path.isdir(SESSIONS_DIR):
        return out
    snap = _netstat_snapshot()
    for name in sorted(os.listdir(SESSIONS_DIR)):
        if not name.endswith(".json"):
            continue
        rec = _read_json(os.path.join(SESSIONS_DIR, name))
        if not rec or rec.get("kind") != "interactive":
            continue
        pid = rec.get("pid")
        ports = snap.get(str(pid)) if pid else None
        port = ports[0] if ports else None
        entry = {
            "pid": pid,
            "sessionId": rec.get("sessionId"),
            "cwd": rec.get("cwd"),
            "startedAt": rec.get("startedAt"),
            "updatedAt": rec.get("updatedAt"),
            "port": port,
            "healthy": None,
        }
        if probe and port:
            entry["healthy"] = _probe_acp(port)
        out.append(entry)
    return out

def find_live_endpoint(session_id=None, cwd=None, probe=True):
    """(port, sessionId, cwd) for a live interactive session.

    v2: when probe=True only trust a port that answers POST connect. Recycled
    sessions leave stale registry rows whose port is dead or 502.
    """
    cands = list_interactive_sessions()
    if session_id:
        cands = [c for c in cands if c["sessionId"] == session_id]
    elif cwd:
        norm = cwd.replace("\\", "/").rstrip("/").lower()
        cands = [c for c in cands
                 if (c.get("cwd") or "").replace("\\", "/").rstrip("/").lower() == norm]
    cands = [c for c in cands if c["port"]]
    if not cands:
        return None
    cands.sort(key=lambda c: c.get("updatedAt") or c.get("startedAt") or 0)
    for c in reversed(cands):  # newest first
        if not probe or _probe_acp(c["port"]):
            return c["port"], c["sessionId"], c.get("cwd")
    return None

# ---------------------------------------------------------------- activation

def activate_via_deeplink(session_id, timeout=25):
    """Bring a conversation alive in the PC client without any manual click.

    workbuddy://chat/<sessionId> focuses that conversation; the daemon promotes
    a prewarm CLI process into an interactive session (live port appears).
    Returns the live endpoint tuple, or None if it did not come up in time.
    Note: this only works for conversations that still exist in the client's
    list; a deleted/cleared conversation cannot be re-raised.
    """
    if not session_id:
        return None
    url = "workbuddy://chat/%s" % session_id
    try:
        os.startfile(url)
    except Exception as e:
        print("activate: failed to open deep link: %s" % e)
        return None
    started = time.time()
    deadline = started + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        found = find_live_endpoint(session_id=session_id)
        if found:
            print("activate: endpoint up after %.1fs (port=%s)" % (time.time() - started, found[0]))
            return found
    return None

# ---------------------------------------------------------------- busy check

def _project_key(cwd):
    p = (cwd or "").replace("\\", "/").strip("/")
    if not p:
        return None
    parts = [x for x in p.split("/") if x]
    if not parts:
        return None
    parts[0] = parts[0].rstrip(":").lower()
    return "-".join(parts)

def _transcript_path(cwd, session_id):
    key = _project_key(cwd)
    if not key:
        return None
    return os.path.join(os.path.expanduser("~/.workbuddy/projects"), key,
                        session_id + ".jsonl")

_BUSY_STATUS = ("in_progress", "streaming", "running", "pending", "queued")
_TERMINAL_STATUS = ("completed", "error", "cancelled", "interrupted", "failed")

def _last_busy_state(cwd, session_id):
    """Scan the transcript tail from the end; return (busy:bool, last_status:str)."""
    path = _transcript_path(cwd, session_id)
    if not path or not os.path.exists(path):
        return False, None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 400000))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return False, None
    nl = tail.find("\n")  # align to a full line (seek may land mid-line)
    if nl >= 0:
        tail = tail[nl + 1:]
    lines = [l for l in tail.splitlines() if l.strip()]
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        status = rec.get("status")
        rtype = rec.get("type")
        role = rec.get("role")
        if status:
            s = str(status).lower()
            busy = any(s.startswith(b) for b in _BUSY_STATUS) and \
                   not any(s.startswith(t) for t in _TERMINAL_STATUS)
            return busy, s
        if rtype == "message" and role == "user":
            return True, "queued"
        if rtype == "function_call":
            return True, "running"
    return False, None

# ---------------------------------------------------------------- ACP client

class AcpClient:
    def __init__(self, port, log=print):
        self.base = "http://127.0.0.1:%d" % port
        self.log = log
        self.conn_id = None
        self.token = None
        self._id = 0

    def _next_id(self):
        self._id += 1
        return self._id

    def _post(self, path, body=None, headers=None, timeout=30):
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "x-codebuddy-request": "1"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(self.base + path, data=data, headers=h, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
            return r.status, r
        except urllib.error.HTTPError as e:
            return e.code, e

    def connect(self):
        st, r = self._post("/api/v1/acp/connect", body=None, timeout=15)
        if st != 200:
            raise RuntimeError("connect failed: HTTP %s" % st)
        body = r.read().decode("utf-8", "replace")
        creds = json.loads(body)
        self.conn_id = creds.get("connectionId")
        self.token = creds.get("sessionToken")
        if not self.conn_id:
            raise RuntimeError("connect: no connectionId in %s" % body[:200])
        return self.conn_id

    def _auth(self):
        h = {"acp-connection-id": self.conn_id}
        if self.token:
            h["acp-session-token"] = self.token
        return h

    def initialize(self):
        st, r = self._post("/api/v1/acp", {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {"protocolVersion": 1,
                       "clientInfo": {"name": "acp-bridge", "version": "2.0.0"},
                       "clientCapabilities": {}}}, self._auth(), timeout=30)
        try:
            r.read(200000)  # drain capability frames
        except Exception:
            pass
        return st

    def prompt(self, session_id, message, confirm_start=True, idle_timeout=20):
        """session/prompt with v2 early-exit.

        confirm_start=True  -> return once the first update frame arrives
                               (turn confirmed running; UI streams it itself).
        confirm_start=False -> read to terminal marker (finishReason stop).
        Returns (http_status, text, ok).
        """
        st, r = self._post("/api/v1/acp", {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "session/prompt",
            "params": {"sessionId": session_id,
                       "prompt": [{"type": "text", "text": message}]}},
            self._auth(), timeout=60)
        if st != 200:
            return st, "", False
        buf = []
        started = False
        finished = False
        last = time.time()
        try:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                s = chunk.decode("utf-8", "replace")
                buf.append(s)
                last = time.time()
                if not started and ('"sessionUpdate"' in s or '"user_message_chunk"' in s
                                    or '"agent_message_chunk"' in s):
                    started = True
                    if confirm_start:
                        break
                if '"finishReason":"stop"' in s or '"outcome":"SUCCESS"' in s \
                        or '"sessionUpdate":"session_end"' in s:
                    finished = True
                    break
                if time.time() - last > idle_timeout:
                    break
        except Exception as e:
            self.log("  [stream read ended: %s]" % e)
        return st, "".join(buf), finished or started

    def load_session(self, session_id, cwd):
        st, r = self._post("/api/v1/acp", {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "session/load",
            "params": {"sessionId": session_id, "cwd": cwd, "mcpServers": []}},
            self._auth(), timeout=60)
        try:
            text = r.read(400000).decode("utf-8", "replace")
        except Exception:
            text = ""
        return st, text

# ---------------------------------------------------------------- send flow

def send_message(session_id, message, cwd=None, ensure=False, wait_idle=0,
                 retries=3, log=print):
    """v2 flow: discover(health-gated) -> connect -> initialize -> prompt.

    No session/load up front (double-render + stall forensics). One load+retry
    only when the worker reports the session missing (fresh recycle).
    """
    last_err = None
    for attempt in range(1, retries + 1):
        found = find_live_endpoint(session_id=session_id, cwd=cwd, probe=True)
        if not found:
            if ensure and session_id and attempt == 1:
                log("no live endpoint; activating via deep link...")
                found = activate_via_deeplink(session_id)
            if not found:
                last_err = "no live endpoint"
                time.sleep(2)
                continue
        port, sid, scwd = found
        cwd = cwd or scwd
        try:
            client = AcpClient(port, log=log)
            client.connect()
            client.initialize()

            if cwd:
                busy, last = _last_busy_state(cwd, sid)
                if busy and wait_idle:
                    deadline = time.time() + wait_idle
                    while time.time() < deadline:
                        time.sleep(2)
                        busy, _ = _last_busy_state(cwd, sid)
                        if not busy:
                            break
                if busy:
                    return 4, "BUSY|现在会话正忙，请稍后再发。|session=%s|last_status=%s" % (sid, last)

            st, text, ok = client.prompt(sid, message, confirm_start=True)
            if st == 200 and ok:
                return 0, "SENT|session=%s|port=%s|attempt=%d" % (sid, port, attempt)
            # worker freshly recycled and lost the session? one load + retry
            if st == 200 and "not found" in text.lower():
                log("worker lost session; one session/load then retry...")
                client.load_session(sid, cwd or "")
                st, text, ok = client.prompt(sid, message, confirm_start=True)
                if st == 200 and ok:
                    return 0, "SENT|session=%s|port=%s|after-reload" % (sid, port)
            last_err = "prompt HTTP %s ok=%s tail=%s" % (st, ok, text[-120:])
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
        log("attempt %d/%d failed: %s" % (attempt, retries, last_err))
        time.sleep(2)
    return 2, "ERROR|%s|session=%s" % (last_err, session_id)

# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--list-probe", action="store_true",
                    help="list with per-port ACP health probe")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--session-id")
    ap.add_argument("--cwd")
    ap.add_argument("--msg")
    ap.add_argument("--wait-idle", type=int, default=0)
    ap.add_argument("--ensure", action="store_true",
                    help="auto-activate via workbuddy:// deep link (needs --session-id)")
    args = ap.parse_args()

    if args.list or args.list_probe:
        rows = list_interactive_sessions(probe=args.list_probe)
        if not rows:
            print("(no live interactive sessions)")
            return 0
        for c in rows:
            health = "" if c["healthy"] is None else ("healthy" if c["healthy"] else "DEAD")
            print("pid=%-6s port=%-6s %-7s session=%s cwd=%s" %
                  (c["pid"], c["port"], health, c["sessionId"], c.get("cwd")))
        return 0

    if not args.session_id and not args.cwd:
        ap.error("--session-id or --cwd required")

    if args.check:
        found = find_live_endpoint(args.session_id, args.cwd, probe=True)
        if not found and args.ensure and args.session_id:
            found = activate_via_deeplink(args.session_id)
        if not found:
            print("ERROR: no live interactive session", file=sys.stderr)
            return 2
        port, sid, cwd = found
        busy, last = _last_busy_state(cwd, sid)
        if busy:
            print("BUSY|现在会话正忙，请稍后再发。|session=%s|last_status=%s" % (sid, last))
            return 4
        print("IDLE|会话空闲，可以发送。|session=%s|last_status=%s" % (sid, last))
        return 0

    if not args.msg:
        ap.error("--msg is required")

    code, line = send_message(args.session_id, args.msg, cwd=args.cwd,
                              ensure=args.ensure, wait_idle=args.wait_idle)
    print(line)
    return code

if __name__ == "__main__":
    sys.exit(main() or 0)
