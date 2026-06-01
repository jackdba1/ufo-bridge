// UFO² COMMAND BRIDGE — Electron main process
const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

let mainWindow = null;
let pythonProcess = null;

// ── Config ──
const PYTHON = 'C:\\UFO\\.venv\\Scripts\\python.exe';
const SERVER  = path.join(__dirname, 'server', 'server.py');
const PORT    = 8099;
const isDev   = process.argv.includes('--dev');

// ── Python server lifecycle ──

function startPythonServer() {
  if (!fs.existsSync(PYTHON)) {
    console.error(`Python not found at ${PYTHON}`);
    return null;
  }
  if (!fs.existsSync(SERVER)) {
    console.error(`Server not found at ${SERVER}`);
    return null;
  }

  const proc = spawn(PYTHON, [SERVER], {
    cwd: path.join(__dirname, 'server'),
    env: { ...process.env, PYTHONUTF8: '1', UFO_BRIDGE_PORT: String(PORT) },
    stdio: isDev ? 'inherit' : 'pipe',
    windowsHide: !isDev,
  });

  proc.on('error', (err) => {
    console.error('Failed to start Python server:', err.message);
  });

  proc.on('exit', (code) => {
    console.log(`Python server exited (${code})`);
    pythonProcess = null;
  });

  if (!isDev) {
    proc.stdout?.on('data', (d) => { /* silent */ });
    proc.stderr?.on('data', (d) => { /* silent */ });
  }

  return proc;
}

function stopPythonServer() {
  if (pythonProcess) {
    // On Windows, tree-kill the Python process and its UFO2 subprocess
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(pythonProcess.pid), '/f', '/t'], {
        windowsHide: true,
      });
    } else {
      pythonProcess.kill('SIGTERM');
    }
    pythonProcess = null;
  }
}

// ── Window ──

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 750,
    minWidth: 700,
    minHeight: 450,
    frame: false,
    transparent: false,
    backgroundColor: '#04140d',
    titleBarStyle: 'hidden',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
    icon: path.join(__dirname, 'assets', 'icon.png'),
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  if (isDev) {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }

  mainWindow.webContents.on('did-fail-load', (event, errorCode, errorDescription, validatedURL) => {
    console.error('Failed to load:', errorDescription, validatedURL);
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}

// ── IPC handlers ──

ipcMain.handle('window:minimize', () => mainWindow?.minimize());
ipcMain.handle('window:maximize', () => {
  if (mainWindow?.isMaximized()) mainWindow.unmaximize();
  else mainWindow?.maximize();
});
ipcMain.handle('window:close', () => mainWindow?.close());
ipcMain.handle('window:isMaximized', () => mainWindow?.isMaximized() ?? false);
ipcMain.handle('server:port', () => PORT);

// ── App lifecycle ──

app.whenReady().then(() => {
  pythonProcess = startPythonServer();
  // Give server a moment to start before opening window
  setTimeout(createWindow, 1500);
});

app.on('window-all-closed', () => {
  stopPythonServer();
  app.quit();
});

app.on('before-quit', () => {
  stopPythonServer();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    setTimeout(createWindow, 500);
  }
});
