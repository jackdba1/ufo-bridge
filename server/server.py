#!/usr/bin/env python3
"""UFO2 BRIDGE — AIP-compliant device endpoint.
Uses websockets library for reliable Windows serving.
"""

import os, sys, re, json, uuid, asyncio, subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import websockets

UFO_ROOT = os.environ.get("UFO_ROOT", r"C:\UFO")
VENV_PY  = os.environ.get("UFO_VENV", r"C:\UFO\.venv\Scripts\python.exe")
PORT     = int(os.environ.get("UFO_BRIDGE_PORT", "8099"))
DEVICE_ID = "ufo2_bridge"

# ── Helpers ──

def _id(): return uuid.uuid4().hex[:12]

def server_msg(msg_type: str, **fields) -> dict:
    return {
        "type": msg_type, "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "response_id": _id(),
        "session_id": fields.pop("session_id", None),
        "task_name": fields.pop("task_name", None),
        "agent_name": None, "process_name": None,
        "root_name": None, "actions": None,
        "messages": None, "error": None,
        "user_request": None, "result": None,
    } | {k: v for k, v in fields.items()}

def client_msg(msg_type: str, **fields) -> dict:
    return {
        "type": msg_type, "status": "ok",
        "client_type": "device", "client_id": DEVICE_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": fields.pop("session_id", None),
        "task_name": fields.pop("task_name", None),
        "target_id": None, "request": None,
        "action_results": None, "request_id": None,
        "prev_response_id": None, "error": None, "metadata": None,
    } | {k: v for k, v in fields.items()}

# ── UFO² subprocess ──

class UFOTask:
    def __init__(self):
        self._proc = None
        self.running = False

    async def start(self, req: str):
        tid = re.sub(r"[^a-zA-Z0-9]", "_", req.lower())[:30]
        env = os.environ.copy(); env["PYTHONUTF8"] = "1"
        self._proc = await asyncio.create_subprocess_exec(
            VENV_PY, "-m", "ufo", "--task", tid, "--request", req,
            cwd=UFO_ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self.running = True

    async def readline(self) -> Optional[str]:
        if not self.running or not self._proc or not self._proc.stdout:
            return None
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=120)
        except asyncio.TimeoutError:
            return None
        if not line:
            self.running = False
            return None
        return line.decode("utf-8", errors="replace")

    async def stop(self):
        if self._proc:
            try: self._proc.terminate()
            except: pass
            self.running = False

# ── Connection state ──

class BridgeSession:
    def __init__(self):
        self.client_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self.task: Optional[UFOTask] = None
        self.legacy = False  # Browser text mode vs AIP JSON mode

# ── WebSocket handler ──

async def bridge_handler(ws: websockets.WebSocketServerProtocol, path: str):
    s = BridgeSession()
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    noise = {"AuthlibDeprecation", "PydanticDeprecated", "warnings.warn",
             "PyPDF2", "AgentRegistry", "Cost is not available", "Cost information"}

    try:
        async for raw in ws:
            # Try JSON (AIP) first
            try:
                msg = json.loads(raw)
                await _handle_aip(ws, s, msg)
                continue
            except (json.JSONDecodeError, TypeError):
                pass

            # Legacy browser text mode
            text = raw.strip()
            if text == "__PING__":
                await ws.send("__PONG__")
            elif text.startswith("__CMD__:"):
                await _handle_legacy(ws, s, text[8:], ansi, noise)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if s.task: s.task.stop()


async def _handle_aip(ws, s: BridgeSession, msg: dict):
    mtype = msg.get("type", "")

    if mtype == "register":
        s.client_id = msg.get("client_id", "unknown")
        resp = server_msg("heartbeat")
        await ws.send(json.dumps(resp))

    elif mtype == "heartbeat":
        resp = server_msg("heartbeat", session_id=msg.get("session_id"))
        await ws.send(json.dumps(resp))

    elif mtype == "task":
        sid = msg.get("session_id", _id())
        s.session_id = sid
        resp = server_msg("task",
            session_id=sid, task_name=msg.get("task_name", "task"),
            user_request=msg.get("request", ""),
        )
        await ws.send(json.dumps(resp))

    elif mtype == "command":
        sid = msg.get("session_id") or s.session_id or _id()
        s.session_id = sid
        rid = msg.get("response_id", "")
        actions = msg.get("actions", [])
        req_text = msg.get("user_request", "")

        results = []
        for act in actions:
            tool = act.get("tool_name", "")
            r = await _exec_tool(s, tool, act.get("parameters", {}), req_text)
            results.append({
                "status": r["status"],
                "result": r.get("result"),
                "error": r.get("error"),
                "namespace": act.get("tool_type"),
                "call_id": act.get("call_id"),
            })

        resp = client_msg("command_results",
            session_id=sid, action_results=results,
            prev_response_id=rid,
        )
        await ws.send(json.dumps(resp))

    elif mtype == "task_end":
        if s.task: await s.task.stop(); s.task = None


async def _exec_tool(s: BridgeSession, tool: str, params: dict, req_text: str) -> dict:
    if tool == "run_task":
        request = params.get("request", req_text)
        if not request:
            return {"status": "failure", "error": "No request provided"}
        if s.task: await s.task.stop()
        s.task = UFOTask()
        await s.task.start(request)
        lines = []
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        while s.task.running:
            l = await s.task.readline()
            if l is None: break
            clean = ansi.sub("", l).rstrip()
            if clean and not any(x in clean for x in [
                "AuthlibDeprecation","PydanticDeprecated","warnings.warn",
                "PyPDF2","AgentRegistry","Cost is not","Cost information",
            ]):
                lines.append(clean)
        success = any("COMPLETE" in l or "FINISH" in l or "\u2705" in l for l in lines)
        return {"status": "success" if success else "failure", "result": "\n".join(lines[-30:])}
    elif tool == "get_status":
        return {"status": "success", "result": "running" if s.task else "idle"}
    elif tool == "cancel":
        if s.task: await s.task.stop(); s.task = None
        return {"status": "success", "result": "cancelled"}
    return {"status": "failure", "error": f"Unknown tool: {tool}"}


async def _handle_legacy(ws, s: BridgeSession, req: str, ansi, noise):
    if s.task: await s.task.stop()
    s.task = UFOTask()
    await s.task.start(req)
    await ws.send("__START__")
    buf = ""
    while s.task and s.task.running:
        l = s.task.readline()
        if l is None: break
        clean = ansi.sub("", l).rstrip()
        if not clean or any(x in clean for x in noise): continue
        buf += clean + "\n"
        if len(buf) > 4096:
            await ws.send(buf); buf = ""
    if buf: await ws.send(buf)
    await ws.send("__DONE__")


# ── HTTP (serve HTML) ──

async def http_handler(reader, writer):
    html_path = Path(__file__).parent.parent / "renderer" / "index.html"
    if html_path.exists():
        content = html_path.read_text(encoding="utf-8")
    else:
        content = "<h1>UFO2 Bridge</h1><p>Frontend not found.</p>"
    resp = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(content.encode('utf-8'))}\r\nConnection: close\r\n\r\n{content}"
    writer.write(resp.encode()); await writer.drain()
    writer.close(); await writer.wait_closed()


async def main():
    html_path = Path(__file__).parent.parent / "renderer" / "index.html"
    if not html_path.exists():
        print(f"Warning: {html_path} not found")

    # WebSocket server
    ws_server = await websockets.serve(
        bridge_handler, "127.0.0.1", PORT,
        ping_interval=None,  # Disable auto-ping to prevent timeouts during UFO execution
        ping_timeout=None,
        close_timeout=10,
    )
    # HTTP server
    http_server = await asyncio.start_server(http_handler, "127.0.0.1", PORT + 1)

    print(f"  UFO2 BRIDGE ws://127.0.0.1:{PORT}  [AIP device endpoint]")
    print(f"  Device ID: {DEVICE_ID}")
    print(f"  UI: http://127.0.0.1:{PORT+1}")

    await asyncio.gather(
        asyncio.create_task(ws_server.wait_closed()),
        http_server.serve_forever(),
    )


if __name__ == "__main__":
    asyncio.run(main())
