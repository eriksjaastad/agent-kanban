import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Add-on pages live outside this folder, in addons/<name>/frontend/ (see
// src/addons.ts). `@core` is how they import the core's components, and
// `dedupe` makes their bare imports (react, ...) resolve from this folder's
// node_modules, which is not on the path above addons/.
const addons = fileURLToPath(new URL('../../addons', import.meta.url))

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@core': fileURLToPath(new URL('./src', import.meta.url)) },
    dedupe: ['react', 'react-dom', 'react-router-dom', '@testing-library/react', '@testing-library/user-event'],
  },
  server: { fs: { allow: ['.', addons] } },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    include: ['src/**/*.test.{ts,tsx,js}', `${addons}/*/frontend/**/*.test.{ts,tsx}`],
  },
})
