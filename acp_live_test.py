#!/usr/bin/env python
# ACP direct-inject smoke test: send one message into a LIVE interactive
# session owned by the PC client's own daemon, so the PC client shows it
# in real-time. Minimal, no discovery -- pass an explicit port.
#
# Usage: python acp_live_test.py <port> <sessionId> <cwd> <message>
import json, sys, urllib.request, urllib.error

BASE = "http://127.0.0.1:%s" % sys.argv[1]
SESSION_ID = sys.argv[2]
CWD = sys.argv[3]
MESSAGE = sys.argv[4] if len(sys.argv) > 4 else "bridge ping from external agent"

def post(path, body=None, headers=None, timeout=30):
    url = BASE + path
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "x-codebuddy-request": "1"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

# 1. connect -> connectionId + sessionToken
st, body = post("/api/v1/acp/connect", body=None)
print("CONNECT", st, body[:300])
if st != 200:
    sys.exit(1)
creds = json.loads(body)
conn_id = creds.get("connectionId")
token = creds.get("sessionToken")
print("connectionId =", conn_id)
print("sessionToken =", (token or "")[:24], "...")

auth = {"acp-connection-id": conn_id}
if token:
    auth["acp-session-token"] = token

# 2. initialize
st, body = post("/api/v1/acp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": 1,
               "clientInfo": {"name": "acp-bridge", "version": "1.0.0"},
               "clientCapabilities": {}}}, auth)
print("INITIALIZE", st, body[:400])

# 3. session/load
st, body = post("/api/v1/acp", {"jsonrpc": "2.0", "id": 2, "method": "session/load",
    "params": {"sessionId": SESSION_ID, "cwd": CWD, "mcpServers": []}}, auth)
print("LOAD", st, body[:400])

# 4. session/prompt
st, body = post("/api/v1/acp", {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
    "params": {"sessionId": SESSION_ID,
               "prompt": [{"type": "text", "text": MESSAGE}]}}, auth, timeout=180)
print("PROMPT", st)
print(body[:2000])
