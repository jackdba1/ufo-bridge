#!/usr/bin/env python3
"""UFO2 BRIDGE — thin WebSocket backend.
Streams UFO2 stdout to the fake-TUI frontend in real time.
"""

import os
import sys
import re
import json
import asyncio
import subprocess
from pathlib import Path
from datetime import datetime

UFO_ROOT = os.environ.get("UFO_ROOT", r"C:\UFO")
VENV_PY = os.environ.get("UFO_VENV", r"C:\UFO\.venv\Scripts\python.exe")
PORT = int(os.environ.get("UFO_BRIDGE_PORT", "8099"))

HTTP_HTML = """\
HTTP/1.1 200 OK\r
Content-Type: text/html; charset=utf-8\r
Connection: close\r
\r
"""

# ── UFO² subprocess wrapper ──

class UFOTask:
    def __init__(self, request: str):
        tid = re.sub(r"[^a-zA-Z0-9]", "_", request.lower())[:30]
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self._proc = subprocess.Popen(
            [VENV_PY, "-m", "ufo", "--task", tid, "--request", request],
            cwd=UFO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env,
        )
        self.running = True

    def readline(self) -> str | None:
        if not self.running:
            return None
        assert self._proc.stdout
        line = self._proc.stdout.readline()
        if not line and self._proc.poll() is not None:
            self.running = False
            return None
        return line

    def stop(self):
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self.running = False


# ── WebSocket handler ──

class BridgeProtocol:
    """Custom WebSocket-like protocol over raw TCP for zero-dependency server."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self._ufo: UFOTask | None = None

    async def handle(self):
        try:
            data = await self.reader.readline()
            if not data:
                return
            data = data.decode("utf-8", errors="replace").strip()

            # Handle HTTP upgrade request
            if data.startswith("GET "):
                await self._http_handshake(data)
                return

            # Raw WebSocket frame — we do minimal framing
            # All frames are text from the browser
            self._ufo = None
            while True:
                frame = await self._read_frame()
                if frame is None:
                    break
                msg = frame.decode("utf-8", errors="replace").strip()
                if msg == "__PING__":
                    await self._send_text("__PONG__")
                elif msg.startswith("__CMD__:"):
                    request = msg[8:]
                    self._ufo = UFOTask(request)
                    asyncio.create_task(self._stream_ufo())
        except Exception:
            pass
        finally:
            if self._ufo:
                self._ufo.stop()
            try:
                self.writer.close()
            except Exception:
                pass

    async def _http_handshake(self, first_line: str):
        # Read all headers
        headers = {}
        while True:
            line = await self.reader.readline()
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                break
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        # If WebSocket upgrade
        if headers.get("upgrade", "").lower() == "websocket":
            key = headers.get("sec-websocket-key", "")
            accept = self._ws_accept(key)
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            )
            self.writer.write(resp.encode())
            await self.writer.drain()
            # Continue to message loop
            await self._ws_loop()
        else:
            # Serve the HTML file
            html_path = Path(__file__).parent.parent / "renderer" / "index.html"
            if html_path.exists():
                content = html_path.read_text(encoding="utf-8")
            else:
                content = "<h1>UFO2 Bridge</h1><p>Frontend not found.</p>"
            resp = HTTP_HTML + content
            self.writer.write(resp.encode())
            await self.writer.drain()
            self.writer.close()

    async def _ws_loop(self):
        try:
            while True:
                frame = await self._read_frame()
                if frame is None:
                    break
                msg = frame.decode("utf-8", errors="replace").strip()
                if msg == "__PING__":
                    await self._send_text("__PONG__")
                elif msg.startswith("__CMD__:"):
                    if self._ufo:
                        self._ufo.stop()
                    request = msg[8:]
                    self._ufo = UFOTask(request)
                    asyncio.create_task(self._stream_ufo())
        except Exception:
            pass

    async def _read_frame(self) -> bytes | None:
        try:
            header = await self.reader.readexactly(2)
        except Exception:
            return None
        b0, b1 = header[0], header[1]
        fin = (b0 & 0x80) != 0
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        length = b1 & 0x7F

        if length == 126:
            try:
                ext = await self.reader.readexactly(2)
            except Exception:
                return None
            length = int.from_bytes(ext, "big")
        elif length == 127:
            try:
                ext = await self.reader.readexactly(8)
            except Exception:
                return None
            length = int.from_bytes(ext, "big")

        mask_key = None
        if masked:
            try:
                mask_key = await self.reader.readexactly(4)
            except Exception:
                return None

        try:
            payload = await self.reader.readexactly(length)
        except Exception:
            return None

        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == 0x8:  # Close
            return None
        if opcode == 0x9:  # Ping
            await self._send_frame(0xA, payload)
            return await self._read_frame()

        return payload

    async def _send_text(self, text: str):
        await self._send_frame(0x1, text.encode("utf-8"))

    async def _send_frame(self, opcode: int, payload: bytes):
        frame = bytearray()
        frame.append(0x80 | opcode)
        plen = len(payload)
        if plen < 126:
            frame.append(plen)
        elif plen < 65536:
            frame.append(126)
            frame.extend(plen.to_bytes(2, "big"))
        else:
            frame.append(127)
            frame.extend(plen.to_bytes(8, "big"))
        frame.extend(payload)
        try:
            self.writer.write(bytes(frame))
            await self.writer.drain()
        except Exception:
            pass

    async def _stream_ufo(self):
        if not self._ufo:
            return
        await self._send_text("__START__")
        buf = ""
        ansi_strip = re.compile(r"\x1b\[[0-9;]*m")
        while self._ufo and self._ufo.running:
            line = self._ufo.readline()
            if line is None:
                break
            clean = ansi_strip.sub("", line).rstrip()
            if not clean:
                continue
            # Skip boring lines
            if any(skip in clean for skip in [
                "AuthlibDeprecation", "PydanticDeprecated", "warnings.warn",
                "PyPDF2", "AgentRegistry", "Cost is not available",
                "Cost information",
            ]):
                continue
            buf += clean + "\n"
            if len(buf) > 4096:
                await self._send_text(buf)
                buf = ""
        if buf:
            await self._send_text(buf)
        await self._send_text("__DONE__")

    @staticmethod
    def _ws_accept(key: str) -> str:
        import hashlib, base64
        GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        sha1 = hashlib.sha1((key + GUID).encode()).digest()
        return base64.b64encode(sha1).decode()


async def main():
    html_path = Path(__file__).parent.parent / "renderer" / "index.html"
    if not html_path.exists():
        print("Error: bridge/static/index.html not found")
        return
    server = await asyncio.start_server(
        lambda r, w: BridgeProtocol(r, w).handle(),
        "127.0.0.1", PORT,
    )
    print(f"\n  \u2b21 UFO\u00b2 BRIDGE :{PORT}")
    print(f"  Open http://localhost:{PORT}\n")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
