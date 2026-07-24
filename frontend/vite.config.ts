import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  build: {
    rollupOptions: {
      output: {
        // Split rarely-changing vendor libs into their own long-cached chunks,
        // separate from app code (which changes every deploy). Combined with the
        // lazy route chunks, the initial download for any page is much smaller.
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return;
          if (id.includes('lucide-react')) return 'icons';
          if (
            id.includes('react-router') ||
            id.includes('react-dom') ||
            id.includes('/react/') ||
            id.includes('scheduler')
          ) {
            return 'react-vendor';
          }
        },
      },
    },
  },
});
