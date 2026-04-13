import { useState } from 'react';
import { arkaFetch } from '../utils/api.js';
import StepIndicator from '../components/setup/StepIndicator.jsx';
import WelcomeStep   from '../components/setup/WelcomeStep.jsx';
import DeviceStep    from '../components/setup/DeviceStep.jsx';
import ExchangeStep  from '../components/setup/ExchangeStep.jsx';
import DataStep      from '../components/setup/DataStep.jsx';
import TelegramStep  from '../components/setup/TelegramStep.jsx';
import ReviewStep    from '../components/setup/ReviewStep.jsx';

async function postCreds(creds) {
  try {
    const r = await arkaFetch('/setup/credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(creds),
    });
    return r.ok;
  } catch { return false; }
}

export default function SetupWizard({ onComplete, setupStatus }) {
  const [step,      setStep]     = useState(0);
  const [launching, setLaunching] = useState(false);

  const next = () => setStep(s => Math.min(s + 1, 5));
  const back = () => setStep(s => Math.max(s - 1, 0));

  async function launch() {
    setLaunching(true);
    await postCreds({ SETUP_COMPLETE: 'true' });
    setTimeout(() => { setLaunching(false); onComplete(); }, 600);
  }

  return (
    <div className="min-h-screen bg-black flex flex-col">
      {/* Top bar */}
      <header className="fixed top-0 inset-x-0 z-20 glass border-b border-edge flex items-center justify-center h-12">
        <span className="font-jetbrains text-base font-bold tracking-[0.25em] gradient-text">ARKA</span>
      </header>

      {/* Scroll area */}
      <main className={`flex-1 flex flex-col items-center px-4 pb-12 ${step === 0 ? 'justify-center pt-16' : 'justify-start pt-24'}`}>
        {/* Step indicator (hidden on welcome) */}
        {step > 0 && (
          <div className="w-full max-w-md mb-2">
            <StepIndicator current={step} />
          </div>
        )}

        {step === 0 && <WelcomeStep onNext={next} />}
        {step === 1 && <DeviceStep   onNext={next} onBack={back} />}
        {step === 2 && <ExchangeStep onNext={next} onBack={back} onSave={postCreds} />}
        {step === 3 && <DataStep     onNext={next} onBack={back} onSave={postCreds} />}
        {step === 4 && <TelegramStep onNext={next} onBack={back} onSave={postCreds} />}
        {step === 5 && (
          <ReviewStep
            onBack={back}
            onLaunch={launch}
            setupStatus={setupStatus}
            launching={launching}
          />
        )}
      </main>
    </div>
  );
}
