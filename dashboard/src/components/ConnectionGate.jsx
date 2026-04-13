import { useState } from 'react';
import { cn } from '../utils/cn.js';

/**
 * Shown on first launch when running as a web page or Capacitor mobile app
 * and no hypervisor URL has been configured yet.
 *
 * Not shown in Electron — the desktop app stores the URL automatically.
 */
export default function ConnectionGate({ onConnect }) {
  const [url,     setUrl]     = useState('http://192.168.1.');
  const [testing, setTesting] = useState(false);
  const [error,   setError]   = useState(null);

  async function connect() {
    setTesting(true);
    setError(null);
    const clean = url.trim().replace(/\/$/, '');
    try {
      const r = await fetch(`${clean}/health`, {
        signal: AbortSignal.timeout(5000),
      });
      if (r.ok) {
        onConnect(clean);
      } else {
        setError('Server replied but returned an error. Is this the right address?');
      }
    } catch {
      setError('Could not reach that address. Check the IP and make sure Arka is running.');
    }
    setTesting(false);
  }

  return (
    <div className="min-h-screen bg-black flex flex-col items-center justify-center px-4">

      <div className="mb-10 text-center">
        <p className="font-jetbrains text-2xl font-bold tracking-[0.3em] gradient-text mb-2">ARKA</p>
        <p className="text-muted text-sm">Connect to your Arka server to continue</p>
      </div>

      <div className="w-full max-w-sm">
        <div className="bg-card border border-edge rounded-2xl p-6 space-y-4">

          <div>
            <label className="block text-[10px] font-bold uppercase tracking-widest text-muted mb-1.5">
              Arka Server Address
            </label>
            <input
              type="url"
              value={url}
              onChange={e => setUrl(e.target.value)}
              placeholder="http://192.168.1.x:8000"
              className="w-full px-3 py-2.5 bg-black border border-edge rounded-xl text-cream text-sm font-jetbrains outline-none focus:border-info/50 transition-colors placeholder:text-muted/25"
              onKeyDown={e => e.key === 'Enter' && !testing && connect()}
            />
            <p className="text-[10px] text-muted mt-2 leading-relaxed">
              Find your server IP with{' '}
              <span className="font-jetbrains text-data">arp -a</span> on your local network,
              or use{' '}
              <span className="font-jetbrains text-data">http://localhost:8000</span>{' '}
              if Arka is on the same device.
            </p>
          </div>

          {error && (
            <div className="flex items-start gap-2 p-3 bg-loss/5 border border-loss/15 rounded-xl text-xs text-muted">
              <span className="shrink-0 text-loss">⚠</span>
              <span>{error}</span>
            </div>
          )}

          <button
            onClick={connect}
            disabled={testing || !url.trim()}
            className={cn(
              'w-full py-3 font-bold text-sm rounded-xl transition-all duration-200',
              testing || !url.trim()
                ? 'bg-line text-muted cursor-wait'
                : 'bg-info text-black hover:bg-info/90 active:scale-[0.98]',
            )}
          >
            {testing ? 'Connecting...' : 'Connect →'}
          </button>
        </div>

        <p className="text-center text-[10px] text-muted mt-5 leading-relaxed">
          On a Mac, Windows, or Linux computer?{' '}
          Download the{' '}
          <span className="text-cream font-semibold">Arka desktop app</span>{' '}
          for automatic connection without this step.
        </p>
      </div>
    </div>
  );
}
