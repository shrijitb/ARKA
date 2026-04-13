import { useState, useEffect, useCallback } from 'react';
import { arkaFetch } from '../utils/api.js';

export function useArkaData(intervalMs = 10000) {
  const [data,        setData]        = useState(null);
  const [setupStatus, setSetupStatus] = useState(null);
  const [error,       setError]       = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const [stateRes, setupRes] = await Promise.all([
        arkaFetch('/dashboard/state'),
        arkaFetch('/setup/status'),
      ]);
      if (stateRes.ok) setData(await stateRes.json());
      if (setupRes.ok) setSetupStatus(await setupRes.json());
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, intervalMs);
    return () => clearInterval(id);
  }, [fetchData, intervalMs]);

  return {
    data,
    setupStatus,
    setupComplete: setupStatus?.setup_complete ?? false,
    error,
    refresh: fetchData,
  };
}
