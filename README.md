# UFO² Command Bridge

Electron desktop app wrapping UFO² Desktop AgentOS in a biopunk terminal.

## Architecture

```
Electron main process  →  spawns Python WebSocket server (port 8099)
       ↓
Electron renderer  →  WebSocket client  →  UFO² subprocess pipeline
```

The Python server streams UFO² stdout to the renderer in real time via WebSocket.
The renderer is a canvas-based fake-TUI with CRT effects, particle field, and arc gauge.

## Prerequisites

- Node.js >= 18
- Python >= 3.11 at `C:\UFO\.venv\Scripts\python.exe` (UFO² must be installed)
- OpenRouter API key configured at `C:\UFO\config\ufo\agents.yaml`

## Quick start

```bash
npm install
npm start
```

Dev mode (with Chrome DevTools):

```bash
npm run dev
```

Build standalone `.exe`:

```bash
npm run dist
```

## Project structure

```
ufo-bridge/
├── main.js              Electron main process (window, Python lifecycle)
├── preload.js           Context bridge (window controls IPC)
├── renderer/
│   ├── index.html       Main UI
│   ├── styles.css       PartyGraph biopunk palette
│   └── app.js           WebSocket client, Canvas2D, state
├── server/
│   └── server.py        Python WebSocket backend (spawns UFO²)
├── assets/
│   └── icon.png         App icon
└── package.json
```

## Dependencies

- **electron** — Chromium + Node.js runtime
- **electron-builder** — packaging for Windows
- **Python** — `websockets`, `asyncio` (already in UFO² venv)

## License

MIT
