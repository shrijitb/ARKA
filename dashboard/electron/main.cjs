'use strict';
const { app, BrowserWindow, ipcMain, Menu } = require('electron');
const path = require('node:path');
const fs   = require('node:fs');

const isDev = process.env.ELECTRON_IS_DEV === '1';

// ── Lightweight settings store (no external deps) ───────────────────────────
let _settings = null;

function settingsFile() {
  return path.join(app.getPath('userData'), 'arka-settings.json');
}
function readSettings() {
  if (!_settings) {
    try { _settings = JSON.parse(fs.readFileSync(settingsFile(), 'utf8')); }
    catch { _settings = {}; }
  }
  return _settings;
}
function saveSetting(key, value) {
  readSettings()[key] = value;
  fs.writeFileSync(settingsFile(), JSON.stringify(_settings, null, 2));
}

// ── IPC (synchronous so preload can expose values before page load) ──────────
ipcMain.on('arka:get-hypervisor-url', (e) => {
  e.returnValue = readSettings().hypervisorUrl || 'http://localhost:8000';
});
ipcMain.on('arka:set-hypervisor-url', (_, url) => {
  saveSetting('hypervisorUrl', url);
});

// ── Window ───────────────────────────────────────────────────────────────────
function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth:  960,
    minHeight: 640,
    title: 'Arka',
    backgroundColor: '#000000',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (isDev) {
    win.loadURL('http://localhost:5173');
  } else {
    // dist/ is one directory up from electron/
    win.loadFile(path.join(__dirname, '../dist/index.html'));
  }

  return win;
}

app.whenReady().then(() => {
  // Remove default menu on Windows / Linux (macOS keeps its native menu)
  if (process.platform !== 'darwin') Menu.setApplicationMenu(null);

  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
