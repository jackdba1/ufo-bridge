// ═══════════════════════════════════════════════════════════════════════════
// UFO² COMMAND BRIDGE — Renderer
// WebSocket client, Canvas2D effects, PartyGraph aesthetic
// ═══════════════════════════════════════════════════════════════════════════

const $  = (s) => document.querySelector(s);
const log = $('#log'),
      loadBar = $('#load-bar'),
      led = $('#status-led'),
      stateEl = $('#s-state'),
      sAgent = $('#s-agent'),
      gaugeCvs = $('#gauge-canvas'),
      sparkEl = $('#sparkline'),
      bgCvs = $('#bg-canvas'),
      input = $('#cmd-input'),
      btn = $('#btn-send'),
      tbSessions = $('#tb-sessions'),
      tbLines = $('#tb-lines'),
      tbClock = $('#tb-clock');

const gaugeCtx = gaugeCvs.getContext('2d');
const bgCtx = bgCvs.getContext('2d');

let ws = null, running = false, sessions = 0, lineTotal = 0,
    startTime = Date.now(), frame = 0;
const sparkBuf = new Array(100).fill(0);
const particles = [];

// ── WebSocket ──

async function connect() {
  const port = (window.bridge && await window.bridge.getPort()) || 8199;
  const url = `ws://127.0.0.1:${port}/ws?token=bridge`;

  ws = new WebSocket(url);
  ws.onopen  = () => logLine('s', '⬡ CONNECTED — UFO² COMMAND BRIDGE ONLINE');
  ws.onclose = () => {
    if (running) onDone();
    logLine('w', '⏳ CONNECTION LOST — reconnecting…');
    setTimeout(connect, 2500);
  };
  ws.onerror = () => {};  // onclose handles it
  ws.onmessage = (e) => {
    if (e.data === '__START__') onStart();
    else if (e.data === '__DONE__') onDone();
    else if (e.data === '__PONG__') return;
    else onChunk(e.data);
  };
}

function sendMsg(m) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(m);
}

function send() {
  const cmd = input.value.trim();
  if (!cmd || running) return;
  input.value = '';
  running = true;
  sessions++;
  onStart();
  tbSessions.textContent = sessions;
  logLine('t', `▸ REQUEST: ${cmd}`);
  sendMsg(`__CMD__:${cmd}`);
}

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') send();
});

// ── Line classifier ──

function classify(raw) {
  if (/ERROR|❌|✗/.test(raw)) return 'e';
  if (/✓|✅|COMPLETE|FINISH/.test(raw)) return 's';
  if (/WARNING/.test(raw)) return 'w';
  if (/Round.*Step|HostAgent.*───|AppAgent.*───/.test(raw)) return 't';
  if (/Action applied|⚒/.test(raw)) return 'a';
  if (/Response|┌─|└─/.test(raw)) return 'r';
  if (/Task Results|task complete/i.test(raw)) return 'f';
  return 'i';
}

function logLine(cls, text) {
  const el = document.createElement('div');
  el.className = `line ${cls}`;
  el.textContent = text.replace(/\x1b\[[0-9;]*m/g, '');
  log.appendChild(el);
  requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  lineTotal++;
  tbLines.textContent = lineTotal;
  if (/HostAgent/.test(text)) sAgent.textContent = 'HostAgent';
  if (/AppAgent/.test(text)) sAgent.textContent = 'AppAgent';
  if (/EvaluationAgent/.test(text)) sAgent.textContent = 'Evaluator';
}

// ── State ──

function onStart() {
  led.className = 'busy';
  stateEl.textContent = 'RUNNING';
  loadBar.classList.add('on');
  btn.disabled = true;
}

function onDone() {
  running = false;
  led.className = '';
  stateEl.textContent = 'IDLE';
  loadBar.classList.remove('on');
  btn.disabled = false;
}

function onChunk(text) {
  for (const raw of text.split('\n')) {
    const c = raw.trim();
    if (!c) continue;
    if (/Authlib|Pydantic|warnings\.warn|PyPDF2|AgentRegistry|Cost is not|Cost information/.test(c)) continue;
    logLine(classify(c), c);
  }
}

// ── Canvas: background particles ──

function resizeBg() {
  bgCvs.width = window.innerWidth;
  bgCvs.height = window.innerHeight;
  if (particles.length === 0) {
    for (let i = 0; i < 120; i++) {
      particles.push({
        x: Math.random() * bgCvs.width,
        y: Math.random() * bgCvs.height,
        vx: (Math.random() - 0.5) * 0.25,
        vy: -(0.15 + Math.random() * 0.45),
        s: Math.random() * 1.6 + 0.3,
        a: Math.random() * 0.45 + 0.08,
        hue: 48 + Math.random() * 28,
      });
    }
  }
}

function drawBg() {
  bgCtx.clearRect(0, 0, bgCvs.width, bgCvs.height);
  for (const p of particles) {
    const alpha = p.a * (0.35 + 0.65 * Math.sin(frame * 0.014 + p.x));
    bgCtx.fillStyle = `hsla(${p.hue},85%,52%,${alpha})`;
    bgCtx.fillRect(p.x, p.y, p.s, p.s);
    p.x += p.vx;
    p.y += p.vy;
    if (p.y < -10) { p.y = bgCvs.height + 10; p.x = Math.random() * bgCvs.width; }
    if (p.x < -10) p.x = bgCvs.width + 10;
    if (p.x > bgCvs.width + 10) p.x = -10;
  }
}

// ── Canvas: uptime gauge ──

function drawGauge() {
  const dpr = window.devicePixelRatio || 1;
  const rect = gaugeCvs.parentElement.getBoundingClientRect();
  const w = rect.width - 24;
  const h = 82 * dpr;
  gaugeCvs.width = w * dpr;
  gaugeCvs.height = h;
  gaugeCvs.style.width = w + 'px';
  gaugeCvs.style.height = '82px';
  const ctx = gaugeCvs.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, 82);

  const elapsed = (Date.now() - startTime) / 1000;
  const angle = Math.min(1, elapsed / 300) * Math.PI * 2;
  const cx = w / 2, cy = 64, r = 52;

  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, 0);
  ctx.strokeStyle = 'rgba(47,74,52,0.4)';
  ctx.lineWidth = 7;
  ctx.stroke();

  const grad = ctx.createLinearGradient(0, 0, w, 0);
  grad.addColorStop(0, '#2d4a34');
  grad.addColorStop(0.5, '#39ff14');
  grad.addColorStop(1, '#39e6ff');
  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, Math.PI + angle);
  ctx.strokeStyle = grad;
  ctx.lineWidth = 7;
  ctx.lineCap = 'round';
  ctx.stroke();

  const dx = cx + Math.cos(Math.PI + angle) * r;
  const dy = cy + Math.sin(Math.PI + angle) * r;
  ctx.beginPath();
  ctx.arc(dx, dy, 3.5, 0, Math.PI * 2);
  ctx.fillStyle = '#39ff14';
  ctx.shadowColor = '#39ff14';
  ctx.shadowBlur = 12;
  ctx.fill();
  ctx.shadowBlur = 0;

  sparkBuf.push((elapsed % 60) / 60);
  sparkBuf.shift();
  sparkEl.textContent = brailleLine(sparkBuf);
}

function brailleLine(arr) {
  let out = '';
  const ramp = '⠀⠁⠂⠃⠄⠅⠆⠇⠈⠉⠊⠋⠌⠍⠎⠏⠐⠑⠒⠓⠔⠕⠖⠗⠘⠙⠚⠛⠜⠝⠞⠟⠠⠡⠢⠣⠤⠥⠦⠧⠨⠩⠪⠫⠬⠭⠮⠯⠰⠱⠲⠳⠴⠵⠶⠷⠸⠹⠺⠻⠼⠽⠾⠿';
  for (let i = 0; i < arr.length; i += 2) {
    const a = Math.min(4, Math.floor(arr[i] * 5)) || 0;
    const b = Math.min(4, Math.floor((arr[i + 1] || 0) * 5)) || 0;
    out += ramp[a * 10 + b] || ramp[0];
  }
  return out;
}

// ── Clock ──

function tick() {
  const e = (Date.now() - startTime) / 1000;
  const h = Math.floor(e / 3600);
  const m = Math.floor((e % 3600) / 60);
  const s = Math.floor(e % 60);
  const ts = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  tbClock.textContent = ts;
}

// ── Loop ──

function loop() {
  frame++;
  drawBg();
  if (frame % 15 === 0) drawGauge();
  if (frame % 30 === 0) tick();
  requestAnimationFrame(loop);
}

// ── Init ──

resizeBg();
window.addEventListener('resize', resizeBg);
connect();
loop();
input.focus();

document.addEventListener('keydown', (e) => {
  if (document.activeElement !== input && e.key.length === 1 &&
      !e.ctrlKey && !e.metaKey && !e.altKey) {
    input.focus();
  }
});

setInterval(() => sendMsg('__PING__'), 20000);

setInterval(() => {
  const el = $('#load-blink');
  if (el) el.style.visibility = el.style.visibility === 'hidden' ? 'visible' : 'hidden';
}, 500);

// ── Window controls ──

window.addEventListener('keydown', (e) => {
  if (e.key === 'F12') {
    // dev tools toggle would need a separate IPC — skip for now
  }
});

logLine('s', '⬡ UFO\u00b2 COMMAND BRIDGE');
logLine('i', 'Gemini 2.5 Flash via OpenRouter  |  Visual mode  |  Type a request + Enter');
logLine('i', 'Ctrl+Q to quit  |  F11 fullscreen');
