#!/usr/bin/env python
"""
zcode_send.py -- Send a message into a ZCode session from an external agent
(Claude / Codex / Hermes / OpenClaw / WorkBuddy / ...), reusing that session's
full conversation context, and reading the reply back on stdout.

Why this exists: multi-agent hand-off. Another agent often needs the context
that lives in an existing ZCode session -- the project history, the earlier
decisions, the remembered facts. Starting a fresh session means re-explaining
everything. `--resume <sessionId>` continues ON the same session (verified: no
fork, same sessionId in the DB, context intact across processes), so the
external agent just sends its conclusion/instruction and ZCode keeps going.

Requires the ZCode desktop app to be installed (its CLI bundle is used), and
model credentials are read automatically from ~/.zcode/v2/config.json.

Usage:
  python zcode_send.py --list                                    # list sessions
  python zcode_send.py --list --cwd "C:\\path\\to\\project"      # sessions of one project
  python zcode_send.py --cwd "C:\\path\\to\\project" --msg "..."            # continue latest
  python zcode_send.py --session-id sess_xxx --msg "..."                    # exact session
  python zcode_send.py --session-id sess_xxx --check                        # busy? don't send
  python zcode_send.py --session-id sess_xxx --msg "..." --wait-idle 60     # wait if busy
  python zcode_send.py --cwd "..." --msg "..." --new                        # force new session
"""
import argparse, json, os, subprocess, sys, time

ZCODE_HOME = os.path.expanduser("~/.zcode")
DB_PATH = os.path.join(ZCODE_HOME, "cli", "db", "db.sqlite")
CLI_CANDIDATES = [
    os.path.expanduser("~/AppData/Local/Programs/ZCode/resources/glm/zcode.cjs"),
    "C:/Program Files/ZCode/resources/glm/zcode.cjs",
]

# ------------------------------------------------------------------ sessions

def _connect():
    import sqlite3
    return sqlite3.connect(DB_PATH)

def list_sessions(cwd=None, limit=15):
    c = _connect()
    try:
        if cwd:
            rows = c.execute(
                "SELECT id, directory, title, time_created, time_updated "
                "FROM session WHERE directory = ? ORDER BY time_updated DESC LIMIT ?",
                (cwd, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT id, directory, title, time_created, time_updated "
                "FROM session ORDER BY time_updated DESC LIMIT ?", (limit,)).fetchall()
    finally:
        c.close()
    return rows

def latest_session_id(cwd):
    c = _connect()
    try:
        row = c.execute(
            "SELECT id FROM session WHERE directory = ? ORDER BY time_updated DESC LIMIT 1",
            (cwd,)).fetchone()
    finally:
        c.close()
    return row[0] if row else None

# ------------------------------------------------------------------ busy

def session_busy(session_id):
    """True if the session's last assistant turn is still being generated.

    Signal source: the message table. An assistant message whose JSON
    ``time`` object has no ``completed`` key means that turn is still in
    flight. Verified against the running/finished cases:
      - mid-task : tail assistant message  -> has_completed == False  -> busy
      - finished : tail assistant message  -> has_completed == True   -> idle
    A trailing bare user message (queued, agent not started yet) is busy too.
    """
    import sqlite3
    c = _connect()
    try:
        rows = c.execute(
            "SELECT data FROM message WHERE session_id = ? ORDER BY sequence DESC LIMIT 3",
            (session_id,)).fetchall()
    finally:
        c.close()
    for (raw,) in rows:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        role = d.get("role")
        t = d.get("time") or {}
        if role == "assistant":
            return "completed" not in t
        if role == "user":
            return True  # queued, agent hasn't answered yet
    return False

# ------------------------------------------------------------------ send

def _resolve_cli():
    for p in CLI_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None

def _model_env():
    env = dict(os.environ)
    if env.get("ZCODE_API_KEY") and env.get("ZCODE_BASE_URL") and env.get("ZCODE_MODEL"):
        return env
    cfg = os.path.join(ZCODE_HOME, "v2", "config.json")
    with open(cfg, "r", encoding="utf-8") as f:
        v2 = json.load(f)
    entry = None
    for (name, p) in (v2.get("provider") or {}).items():
        if p.get("enabled") and (p.get("options") or {}).get("apiKey") \
                and (p.get("options") or {}).get("baseURL"):
            entry = (name, p)
            break
    if not entry:
        raise RuntimeError("no enabled model provider in %s" % cfg)
    opts = entry[1]["options"]
    env["ZCODE_MODEL"] = "%s/%s" % (entry[0], env.get("ZCODE_MODEL", "GLM-5.3-Flash"))
    env["ZCODE_BASE_URL"] = opts["baseURL"]
    env["ZCODE_API_KEY"] = opts["apiKey"]
    return env

def send_message(cwd, message, session_id=None, new=False, timeout=600):
    cli = _resolve_cli()
    if not cli:
        raise RuntimeError("zcode.cjs not found; is the ZCode desktop app installed?")
    args = [cli, "--cwd", cwd, "--mode", "yolo", "--prompt", message]
    if session_id:
        args += ["--resume", session_id]
    elif not new:
        args += ["-c"]
    node = env_node = None
    for cand in (os.environ.get("ZCODE_NODE_BIN"),
                 os.path.expanduser("~/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"),
                 "node"):
        if cand and _which(cand):
            node = cand
            break
    r = subprocess.run([node] + args, env=_model_env(),
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr, r.returncode

def _which(cand):
    try:
        return os.path.isfile(cand)
    except Exception:
        return False

# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="only check busy state and print a status line; do not send")
    ap.add_argument("--session-id")
    ap.add_argument("--cwd")
    ap.add_argument("--msg")
    ap.add_argument("--new", action="store_true", help="force a new session")
    ap.add_argument("--wait-idle", type=int, default=0,
                    help="seconds to wait for the session to become idle before sending")
    args = ap.parse_args()

    if args.list:
        rows = list_sessions(args.cwd)
        if not rows:
            print("(no sessions found%s)" % (" for %s" % args.cwd if args.cwd else ""))
            return
        for r in rows:
            busy = session_busy(r[0])
            print("session=%s busy=%s cwd=%s title=%s" %
                  (r[0], busy, r[1], (r[2] or "")[:40]))
        return

    if not args.msg and not args.check:
        ap.error("--msg is required unless --list / --check")

    session_id = args.session_id
    if not session_id and args.cwd:
        session_id = latest_session_id(args.cwd)
        if session_id:
            print("auto-picked latest session for %s: %s" % (args.cwd, session_id))
    if not session_id:
        print("ERROR: no session-id given and no session found for cwd=%s" % args.cwd,
              file=sys.stderr)
        return 2

    if args.wait_idle:
        deadline = time.time() + args.wait_idle
        while time.time() < deadline:
            if not session_busy(session_id):
                break
            print("waiting: session busy...")
            time.sleep(2)

    busy = session_busy(session_id)
    if busy:
        print("BUSY|现在会话正忙，请稍后再发。|session=%s" % session_id)
        return 4
    if args.check:
        print("IDLE|会话空闲，可以发送。|session=%s" % session_id)
        return 0
    if not args.msg:
        ap.error("--msg is required")

    cwd = args.cwd
    if not cwd:
        c = _connect()
        try:
            row = c.execute("SELECT directory FROM session WHERE id = ?",
                            (session_id,)).fetchone()
        finally:
            c.close()
        cwd = row[0] if row else os.getcwd()
    out, err, rc = send_message(cwd, args.msg, session_id,
                                new=args.new)
    sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return 1 if rc != 0 else 0

if __name__ == "__main__":
    sys.exit(main() or 0)
