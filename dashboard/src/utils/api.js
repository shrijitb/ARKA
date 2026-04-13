/**
 * API URL resolution across all Arka surfaces:
 *
 *   Electron (desktop)  →  window.arka.getHypervisorUrl()  (persisted in userData)
 *   Capacitor (mobile)  →  localStorage 'arka_hypervisor_url' (set in ConnectionGate)
 *   Web / always-on     →  localStorage 'arka_hypervisor_url', or '' (relative/same-origin)
 *
 * When base URL is '' the fetch uses relative paths, which works when the app
 * is served directly from the hypervisor (e.g. http://pi:8000/ui).
 */

export function getBaseUrl() {
  if (typeof window === 'undefined') return '';

  // Electron: URL stored in native settings file, exposed synchronously by preload
  if (window.arka?.platform === 'electron') {
    return window.arka.getHypervisorUrl() || 'http://localhost:8000';
  }

  // Capacitor / web browser: from localStorage (set in ConnectionGate during first run)
  return localStorage.getItem('arka_hypervisor_url') || '';
}

/** Returns an absolute URL for the given API path. */
export function apiUrl(path) {
  const base = getBaseUrl();
  return base ? `${base}${path}` : path;
}

/** fetch() wrapper that automatically resolves the correct base URL. */
export async function arkaFetch(path, options = {}) {
  return fetch(apiUrl(path), options);
}
