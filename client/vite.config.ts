import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (['react', 'react-dom', 'react-router-dom'].some((p) => id.includes(`/node_modules/${p}/`))) return 'vendor-react';
            if (id.includes('/node_modules/framer-motion/')) return 'vendor-motion';
            if (id.includes('/node_modules/recharts/')) return 'vendor-charts';
            if (id.includes('/node_modules/@sentry/')) return 'vendor-sentry';
            if (id.includes('/node_modules/@tremor/')) return 'vendor-tremor';
          }
        },
      },
    },
  },
  server: {
    host: true,
    allowedHosts: [
      'localhost',
    ],
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
})
