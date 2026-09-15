#!/usr/bin/env python
"""
acp_live_send.py -- Send a message into a LIVE WorkBuddy PC-client conversation
so it appears in the PC client UI in REAL TIME.

Key insight (2026-09-16): the PC client's own daemon (daemon-app-server-entry.js
--stdio, child of WorkBuddy.exe main process) owns "interactive" ACP sessions
spawned from its prewarm pool. Sending session/prompt to THAT session's
/api/v1/acp endpoint updates the PC client's conversation and the daemon pushes
wb:event -> main -> renderer -> live UI update.

The ACP main channel /api/v1/acp is loopback-exempt (AcpSecurityMiddleware),
and POST /api/v1/acp/connect issues a fresh connectionId + sessionToken with NO
password. This is the channel "their app" uses -- NOT the separate
`codebuddy --serve` daemon we used before (9527), which the PC client never
subscribed to.

Usage:
  python acp_live_send.py --list
  python acp_live_send.py --session-id <uuid> --cwd "D:\\hermes\\workbuddy" --msg "hello"
  python acp_live_send.py --cwd "D:\\hermes\\workbuddy" --msg "hello"   # auto-pick session
"""
import argparse, json, os, re, socket, subprocess, sys, time, urllib.request, urllib.error

SESSIONS_DIR = os.path.expanduser("~/.workbuddy/sessions")

# ---------------------------------------------------------------- discovery

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def list_interactive_sessions():
    """All live interactive sessions known by the registry."""
    out = []
    if not os.path.isdir(SESSIONS_DIR):
        return out
    for name in sorted(os.listdir(SESSIONS_DIR)):
        if not name.endswith(".json"):
            continue
        rec = _read_json(os.path.join(SESSIONS_DIR, name))
        if not rec or rec.get("kind") != "interactive":
            continue
        pid = rec.get("pid")
        out.append({
            "pid": pid,
            "sessionId": rec.get("sessionId"),
            "cwd": rec.get("cwd"),
            "startedAt": rec.get("startedAt"),
            "updatedAt": rec.get("updatedAt"),
            "port": _listening_port(pid),
        })
    return out

def _listening_port(pid):
    """Best-effort: find 127.0.0.1 port LISTENING by pid (netstat via temp file)."""
    if not pid:
        return None
    import tempfile
    out = None
    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(mode="w+", suffix=".netstat", delete=False)
        tmp.close()
        with open(tmp.name, "w", encoding="utf-8", errors="replace") as fh:
            subprocess.run(["netstat", "-ano"], stdout=fh, timeout=15)
        with open(tmp.name, "r", encoding="utf-8", errors="replace") as fh:
            out = fh.read()
    except Exception:
        out = None
    finally:
        if tmp:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
    if not out:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            if parts[4] == str(pid):
                m = re.match(r"127\.0\.0\.1:(\d+)", parts[1])
                if m:
                    return int(m.group(1))
    return None

def find_live_endpoint(session_id=None, cwd=None):
    """Return (port, sessionId, cwd) for a live interactive session."""
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
    best = cands[-1]
    return best["port"], best["sessionId"], best.get("cwd")

# ---------------------------------------------------------------- activation

def activate_via_deeplink(session_id, timeout=25):
    """Bring a conversation alive in the PC client without any manual click.

    The client registers the `workbuddy://` URL scheme; opening
    `workbuddy://chat/<sessionId>` focuses that conversation in the UI, and
    the client's daemon then promotes a prewarm CLI process into an interactive
    session for it (a live 127.0.0.1 port appears in the session registry).
    Returns the live endpoint tuple, or None if it did not come up in time.
    """
    if not session_id:
        return None
    url = "workbuddy://chat/%s" % session_id
    try:
        os.startfile(url)  # Windows: shell-execute the registered URL scheme
    except Exception as e:
        print("activate: failed to open deep link: %s" % e)
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        found = find_live_endpoint(session_id=session_id)
        if found:
            return found
    return None

# ---------------------------------------------------------------- busy check

def _project_key(cwd):
    """D:\\hermes\\workbuddy -> d-hermes-workbuddy (drive letter lower + joined)."""
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
    """Scan the transcript tail from the end; return (busy:bool, last_status:str).

    Walks backwards over jsonl lines and picks the LAST record carrying a
    "status" field (tool calls / assistant messages / request records all do).
    If that status is non-terminal -> busy. A trailing user message with no
    status (request just queued, agent not started yet) also counts as busy.
    """
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
        # no status field: a bare user message at the tail = request queued,
        # agent has not produced anything yet -> busy
        if rtype == "message" and role == "user":
            return True, "queued"
        if rtype == "function_call":
            return True, "running"
    return False, None

def session_busy_via_transcript(cwd, session_id):
    """True if the last request in the transcript has not reached a terminal
    state. Terminal: completed / error / cancelled / interrupted."""
    busy, _ = _last_busy_state(cwd, session_id)
    return busy

def session_busy(port, session_id, cwd=None, timeout=8):
    """Combined busy check: transcript status (authoritative) + endpoint liveness."""
    if cwd and session_busy_via_transcript(cwd, session_id):
        return True
    return False

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

    def _post(self, path, body=None, headers=None, timeout=30, stream=False):
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

    def _read_sse(self, resp, max_bytes=None, idle_timeout=120):
        """Read an SSE/chunked response to completion; tolerate truncation.

        Returns (text, finished) where finished=True if we saw a terminal marker.
        """
        buf = []
        total = 0
        last = time.time()
        finished = False
        try:
            while True:
                if max_bytes is not None and total >= max_bytes:
                    break
                chunk = resp.read(65536)
                if not chunk:
                    break
                s = chunk.decode("utf-8", "replace")
                buf.append(s)
                total += len(chunk)
                last = time.time()
                if '"finishReason":"stop"' in s or '"outcome":"SUCCESS"' in s \
                        or '"sessionUpdate":"session_end"' in s:
                    finished = True
                    break
                if time.time() - last > idle_timeout:
                    break
        except Exception as e:
            self.log("  [stream truncated: %s]" % e)
        return "".join(buf), finished

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
                       "clientInfo": {"name": "hermes-bridge", "version": "1.0.0"},
                       "clientCapabilities": {}}}, self._auth(), timeout=30)
        text, _ = self._read_sse(r, max_bytes=200000)
        return st, text

    def load_session(self, session_id, cwd):
        st, r = self._post("/api/v1/acp", {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "session/load",
            "params": {"sessionId": session_id, "cwd": cwd, "mcpServers": []}},
            self._auth(), timeout=60)
        text, _ = self._read_sse(r, max_bytes=400000)
        return st, text

    def prompt(self, session_id, message, timeout=600):
        st, r = self._post("/api/v1/acp", {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "session/prompt",
            "params": {"sessionId": session_id,
                       "prompt": [{"type": "text", "text": message}]}},
            self._auth(), timeout=timeout)
        text, finished = self._read_sse(r, idle_timeout=120)
        return st, text, finished

    def close(self):
        if not self.conn_id:
            return
        try:
            self._post("/api/v1/acp", None, self._auth(), timeout=5, stream=True)
        except Exception:
            pass

# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="only check busy state and print a status line; do not send")
    ap.add_argument("--session-id")
    ap.add_argument("--cwd")
    ap.add_argument("--msg", required=False)
    ap.add_argument("--wait-idle", type=int, default=0,
                    help="seconds to wait for session to become idle before sending")
    ap.add_argument("--ensure", action="store_true",
                    help="if no live endpoint exists, auto-activate the "
                         "conversation via the workbuddy:// deep link first")
    args = ap.parse_args()

    if args.list:
        rows = list_interactive_sessions()
        if not rows:
            print("(no live interactive sessions)")
            return
        for c in rows:
            print("pid=%-6s port=%-6s session=%s cwd=%s" %
                  (c["pid"], c["port"], c["sessionId"], c.get("cwd")))
        return

    if not args.msg and not args.check:
        ap.error("--msg is required unless --list / --check")

    found = find_live_endpoint(args.session_id, args.cwd)
    if not found and args.ensure and args.session_id:
        print("no live endpoint; activating %s via deep link..." % args.session_id)
        found = activate_via_deeplink(args.session_id)
        if not found:
            print("ERROR: deep link did not bring up a live session for %s" %
                  args.session_id, file=sys.stderr)
            return 2
    if not found:
        print("ERROR: no live interactive session for session-id=%s cwd=%s" %
              (args.session_id, args.cwd), file=sys.stderr)
        print("available:", file=sys.stderr)
        for c in list_interactive_sessions():
            print("  pid=%s session=%s cwd=%s" % (c["pid"], c["sessionId"], c.get("cwd")), file=sys.stderr)
        print("hint: pass --ensure to auto-activate the conversation via the "
              "workbuddy:// deep link (requires --session-id)", file=sys.stderr)
        return 2
    port, session_id, cwd = found
    print("live session: pid-port=%s session=%s cwd=%s" % (port, session_id, cwd))

    # ---- busy gate (user requirement: don't barge into a running task)
    if args.wait_idle:
        deadline = time.time() + args.wait_idle
        while time.time() < deadline:
            busy, last = _last_busy_state(cwd, session_id)
            if not busy:
                break
            print("waiting: session busy (last status=%s)..." % last)
            time.sleep(2)

    busy, last = _last_busy_state(cwd, session_id)
    if busy:
        # Clear, caller-relayable notice. Hermes should hand this line to the user.
        print("BUSY|现在会话正忙，请稍后再发。|session=%s|last_status=%s" % (session_id, last))
        return 4

    if args.check:
        print("IDLE|会话空闲，可以发送。|session=%s|last_status=%s" % (session_id, last))
        return 0

    if not args.msg:
        ap.error("--msg is required")

    client = AcpClient(port)
    cid = client.connect()
    print("connected: connectionId=%s" % cid)
    st, text = client.initialize()
    print("initialize: HTTP %s" % st)
    st, text = client.load_session(session_id, cwd)
    ok = '"error"' not in text
    print("session/load: HTTP %s ok=%s" % (st, ok))
    if not ok:
        print(text[:600])
        return 3
    st, text, finished = client.prompt(session_id, args.msg)
    print("session/prompt: HTTP %s finished=%s bytes=%d" % (st, finished, len(text)))
    # show the tail of the agent reply
    tail = text[-1200:]
    print("--- tail ---")
    print(tail)

if __name__ == "__main__":
    sys.exit(main() or 0)
