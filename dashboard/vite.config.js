import { defineConfig } from "vite";

// Configuración mínima de Vite para el dashboard del monitor XM.
// El backend FastAPI vive en :8000; el dev server de Vite corre en :5173
// y la URL del API se inyecta por env var (VITE_API_URL) para permitir
// tanto demo local como deploy estático (GH Pages) en el futuro.
export default defineConfig({
  root: ".",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    host: "0.0.0.0",
    strictPort: true,
    proxy: {
      // Proxy útil para evitar CORS en desarrollo local:
      // el dashboard hace fetch a /api/* y Vite lo reenvía al backend.
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  preview: {
    port: 5173,
    host: "0.0.0.0",
  },
});
