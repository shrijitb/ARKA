import { defineConfig } from 'vite';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [tailwindcss(), react()],

  // './' base is required for Electron — it loads index.html via file:// so
  // absolute paths like '/assets/...' won't resolve. Relative paths work for
  // both Electron and web deploys.
  base: './',

  server: {
    // Dev-server proxy: forward all known API prefixes to the local hypervisor.
    // The app no longer uses an '/api/' prefix — it calls the hypervisor directly.
    proxy: {
      '^/(setup|dashboard|system|status|regime|pause|resume|workers|watchlist|health|metrics|allocate|signal|execute)(/.*)?$': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
