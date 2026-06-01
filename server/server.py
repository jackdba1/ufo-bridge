#!/usr/bin/env python3
"""
UFO2 BRIDGE — standalone AIP device endpoint.
Audited and hardened per bridge-audit.md.

Fixes applied:
  C1 — subprocess queue: max 1 concurrent UAV2 task. Reject overlap.
  H2 — parse errors return JSON error. No silent swallowing.
  H3 — AIP JSON only. No __START__/__DONE__ raw markers.
  H4 — optional BRIDGE_TOKEN for auth.
  H5 — echo request_id in all responses.
  L3 — salted task IDs (timestamp prefix prevents log collision).
"""

import os, sys, re, json, uuid, time, asyncio, logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any
import websockets

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("bridge")

# ── Config ──
UFO_ROOT  = os.environ.get("UFO_ROOT",  r"C:\UFO")
VENV_PY   = os.environ.get("UFO_VENV",  r"C:\UFO\.venv\Scripts\python.exe")
PORT      = int(os.environ.get("UFO_BRIDGE_PORT", "5099"))
DEVICE_ID = os.environ.get("BRIDGE_DEVICE_ID", "ufo2_bridge")
AUTH_TOKEN = os.environ.get("BRIDGE_TOKEN", "")   # H4 — empty = no auth

# ── Helpers ──

def _id() -> str:  return uuid.uuid4().hex[:8]
def _ts() -> str:  return datetime.now(timezone.utc).isoformat()

def ok_msg(msg_type: str, **fields) -> dict:
    return {"type": msg_type, "status": "ok", "timestamp": _ts(), "response_id": _id()} | fields

def err_msg(reason: str, **fields) -> dict:
    return {"type": "error", "status": "error", "error": reason, "timestamp": _ts()} | fields

# ── Subprocess pool (C1 — max 1 concurrent) ──

class UFOPool:
    """Single-task pool. Rejects overlapping requests with status=business."""
    def __init__(self):
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._lock = asyncio.Lock()

    async def run(self, request: str) -> Dict[str, Any]:
        if not self._lock.locked():
            async with self._lock:
                return await self._execute(request)
        return {"status": "busy", "error": "UFO2 is already executing a task. Retry in a few seconds."}

    async def cancel(self):
        if self._proc:
            try: self._proc.terminate()
            except: pass
            self._running = False

    async def _execute(self, request: str) -> Dict[str, Any]:
        tid = f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9]', '_', request.lower())[:30]}"  # L3
        env = os.environ.copy(); env["PYTHONUTF8"] = "1"
        self._proc = await asyncio.create_subprocess_exec(
            VENV_PY, "-m", "ufo", "--task", tid, "--request", request,
            cwd=UFO_ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self._running = True
        lines = []
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        noise = {"AuthlibDeprecation","PydanticDeprecated","warnings.warn",
                 "PyPDF2","AgentRegistry","Cost is not","Cost information"}
        try:
            while self._proc.returncode is None:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=120)
                if not raw: break
                clean = ansi.sub("", raw.decode("utf-8","replace")).rstrip()
                if clean and not any(x in clean for x in noise):
                    lines.append(clean)
        except asyncio.TimeoutError:
            pass
        finally:
            self._running = False
        success = any("COMPLETE" in l or "FINISH" in l or "\u2705" in l for l in lines)
        return {"status": "success" if success else "failure",
                "output": "\n".join(lines[-40:]),
                "line_count": len(lines),
                "task_id": tid}

pool = UFOPool()

# ── Connection handler ──

async def bridge_handler(ws: websockets.WebSocketServerProtocol, path: str):
    agent: Dict[str, Any] = {"id": None, "type": None, "authenticated": not AUTH_TOKEN}
    try:
        async for raw in ws:
            # ── Parse ──
            try:
                msg = json.loads(raw)
            except Exception as e:
                log.warning("JSON parse error from %s: %s", ws.remote_address, e)
                await ws.send(json.dumps(err_msg(f"JSON parse error: {e}")))
                continue  # H2 — error feedback instead of silent pass

            # ── Auth check (H4) ──
            if not agent["authenticated"]:
                if msg.get("type") == "authenticate":
                    token = msg.get("token", "")
                    if AUTH_TOKEN and token == AUTH_TOKEN:
                        agent["authenticated"] = True
                        await ws.send(json.dumps(ok_msg("authenticated")))
                    else:
                        await ws.send(json.dumps(err_msg("Invalid token")))
                    continue
                elif msg.get("type") in ("register", "heartbeat", "ping"):
                    pass  # Allow register before auth
                else:
                    await ws.send(json.dumps(err_msg("Authentication required. Send {\"type\":\"authenticate\",\"token\":\"...\"}")))
                    continue

            # ── Echo request_id (H5) ──
            rid = msg.get("request_id", "")

            mtype = msg.get("type", "")
            if mtype == "authenticate":
                resp = ok_msg("authenticated") if agent["authenticated"] else err_msg("Already authenticated")

            elif mtype == "register":
                agent["id"] = msg.get("client_id") or msg.get("agent_id", "unknown")
                agent["type"] = msg.get("client_type") or msg.get("agent_type", "device")
                resp = ok_msg("heartbeat", agent_id=agent["id"])

            elif mtype == "heartbeat":
                resp = ok_msg("heartbeat")

            elif mtype == "task":
                session_id = msg.get("session_id", _id())
                resp = ok_msg("task", session_id=session_id, task_name=msg.get("task_name","task"),
                              user_request=msg.get("request",""))

            elif mtype == "command":
                actions = msg.get("actions", [])
                results = []
                for act in actions:
                    tool = act.get("tool_name","")
                    params = act.get("parameters",{})
                    if tool == "run_task":
                        req = params.get("request", msg.get("user_request",""))
                        r = await pool.run(req)
                        results.append({
                            "status": r["status"],
                            "result": r if r["status"] != "busy" else None,
                            "error": r.get("error"),
                            "namespace": "action",
                            "call_id": act.get("call_id"),
                            "task_id": r.get("task_id"),
                        })
                    elif tool == "get_status":
                        results.append({"status": "success", "result": "running" if pool._running else "idle", "namespace": "data_collection", "call_id": act.get("call_id")})
                    elif tool == "cancel":
                        await pool.cancel()
                        results.append({"status": "success", "result": "cancelled", "namespace": "action", "call_id": act.get("call_id")})
                    else:
                        results.append({"status": "failure", "error": f"Unknown tool: {tool}", "namespace": act.get("tool_type",""), "call_id": act.get("call_id")})

                resp = {
                    "type": "command_results", "status": "ok",
                    "client_type": "device", "client_id": DEVICE_ID,
                    "session_id": msg.get("session_id"),
                    "action_results": results,
                    "prev_response_id": msg.get("response_id",""),
                    "timestamp": _ts(),
                }

                # If task ended, send task_end (H3 — JSON only, no raw markers)
                if any(a.get("tool_name") == "run_task" for a in actions):
                    end = ok_msg("task_end",
                        session_id=msg.get("session_id"),
                        status="completed" if all(r["status"]=="success" for r in results) else "failed",
                    )
                    await ws.send(json.dumps(end))

            elif mtype == "task_end":
                resp = ok_msg("heartbeat")  # acknowledge

            elif mtype == "ping":
                resp = ok_msg("pong")

            elif mtype == "discover":
                resp = ok_msg("discover", agents=[{
                    "agent_id": DEVICE_ID, "agent_type": "device",
                    "capabilities": ["windows_apps","file_mgmt","web_browse","shell","screenshots","uia_control"],
                    "status": "online",
                }])

            else:
                resp = err_msg(f"Unknown message type: {mtype}")

            if rid:
                resp["request_id"] = rid  # H5

            await ws.send(json.dumps(resp))

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        log.error("Handler error: %s", e)
    finally:
        if agent["id"]:
            log.info("Agent disconnected: %s", agent["id"])

# ── HTTP UI ──

async def http_handler(reader, writer):
    html_path = Path(__file__).parent.parent / "renderer" / "index.html"
    content = html_path.read_text(encoding="utf-8") if html_path.exists() else "<h1>UFO2 Bridge</h1>"
    body = content.encode("utf-8")
    resp = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    writer.write(resp.encode()); writer.write(body); await writer.drain()
    writer.close(); await writer.wait_closed()

# ── Main ──

async def main():
    html_path = Path(__file__).parent.parent / "renderer" / "index.html"
    if not html_path.exists():
        log.warning("HTML UI not found at %s", html_path)

    ws_server = await websockets.serve(bridge_handler, "127.0.0.1", PORT, ping_interval=None, ping_timeout=None)
    http_srv = await asyncio.start_server(http_handler, "127.0.0.1", PORT + 1)

    auth_str = "(token) " if AUTH_TOKEN else "(no auth) "
    log.info("UFO2 BRIDGE ws://127.0.0.1:%s %s device=%s", PORT, auth_str, DEVICE_ID)
    await asyncio.gather(ws_server.wait_closed(), http_srv.serve_forever())

if __name__ == "__main__":
    asyncio.run(main())
