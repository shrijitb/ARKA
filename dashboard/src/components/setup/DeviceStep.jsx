import { useState, useEffect } from 'react';
import { arkaFetch } from '../../utils/api.js';
import { cn } from '../../utils/cn.js';

const PROFILES = {
  pi5_16gb:    { name: 'Raspberry Pi 5 · 16 GB', model: 'Qwen 2.5 (3B)', cycle: '90s', icon: '🖥' },
  pi5_8gb:     { name: 'Raspberry Pi 5 · 8 GB',  model: 'Qwen 2.5 (3B)', cycle: '120s', icon: '🖥' },
  pi4_8gb:     { name: 'Raspberry Pi 4 · 8 GB',  model: 'TinyLlama (1.1B)', cycle: '180s', icon: '🖥' },
  laptop_cpu:  { name: 'x86 Laptop (CPU only)',   model: 'Qwen 2.5 (3B)', cycle: '60s', icon: '💻' },
  desktop_gpu: { name: 'Desktop with GPU',        model: 'Qwen 2.5 (7B)', cycle: '30s', icon: '🖱' },
};

export default function DeviceStep({ onNext, onBack }) {
  const [hw, setHw] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    arkaFetch('/system/hardware').then(r => r.ok ? r.json() : null)
      .then(d => { setHw(d); setLoading(false); }).catch(() => setLoading(false));
  }, []);

  const key    = hw?.hardware_profile || 'pi5_8gb';
  const prof   = PROFILES[key] || PROFILES.pi5_8gb;
  const rows   = hw ? [
    ['Board', hw.device_board || hw.board || '—'],
    ['CPU',   hw.cpu_model || '—'],
    ['RAM',   hw.ram_gb ? `${hw.ram_gb} GB` : hw.ram_mb ? `${Math.round(hw.ram_mb/1024)} GB` : '—'],
    ['Arch',  hw.arch || '—'],
  ] : [];

  return (
    <div className="anim-fade-in max-w-md mx-auto">
      <div className="bg-card border border-edge rounded-2xl p-6 mb-4">
        <p className="text-[10px] font-bold uppercase tracking-widest text-muted mb-1">Step 2 of 6</p>
        <h2 className="text-xl font-bold text-cream mb-5">Your Device</h2>

        <div className="text-center mb-5">
          <div className="text-5xl mb-2">{prof.icon}</div>
          {loading ? (
            <p className="text-muted text-sm anim-pulse">Detecting hardware...</p>
          ) : (
            <>
              <p className="text-base font-bold text-cream">{prof.name}</p>
              <p className="text-xs font-jetbrains text-muted mt-0.5">{key}</p>
            </>
          )}
        </div>

        {rows.length > 0 && (
          <div className="bg-black/50 rounded-xl overflow-hidden mb-4">
            {rows.map(([k, v]) => (
              <div key={k} className="flex justify-between px-4 py-2 border-b border-edge last:border-0">
                <span className="text-xs text-muted">{k}</span>
                <span className="text-xs font-jetbrains text-data">{v}</span>
              </div>
            ))}
          </div>
        )}

        <div className="bg-profit/5 border border-profit/15 rounded-xl p-3.5">
          <p className="text-[10px] font-bold uppercase tracking-wider text-profit mb-2">Arka Will Use</p>
          {[
            ['🧠', 'AI Model', prof.model],
            ['⏱', 'Cycle',    `Every ${prof.cycle}`],
            ['📈', 'Strategy', 'Swing + Mean-Reversion'],
          ].map(([ico, k, v]) => (
            <div key={k} className="flex items-center gap-2 text-xs py-0.5">
              <span>{ico}</span>
              <span className="text-muted">{k}:</span>
              <span className="text-cream">{v}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="flex gap-2.5">
        <button onClick={onBack} className={ghost}>← Back</button>
        <button onClick={onNext} className={primary}>This looks right →</button>
      </div>
    </div>
  );
}

const primary = 'flex-1 py-3 bg-warn text-black font-bold text-sm rounded-xl hover:bg-warn/90 active:scale-[0.98] transition-all';
const ghost   = 'px-5 py-3 bg-transparent border border-edge text-muted text-sm rounded-xl hover:border-line hover:text-cream transition-all';
