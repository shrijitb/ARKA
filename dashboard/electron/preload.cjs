'use strict';
const { contextBridge, ipcRenderer } = require('electron');

// Expose a synchronous window.arka API to the renderer.
// Uses sendSync so the URL is available before any React code runs.
contextBridge.exposeInMainWorld('arka', {
  platform: 'electron',

  /** Returns the stored hypervisor URL (e.g. 'http://localhost:8000') */
  getHypervisorUrl: () => ipcRenderer.sendSync('arka:get-hypervisor-url'),

  /** Persists a new hypervisor URL to disk */
  setHypervisorUrl: (url) => ipcRenderer.send('arka:set-hypervisor-url', url),
});
