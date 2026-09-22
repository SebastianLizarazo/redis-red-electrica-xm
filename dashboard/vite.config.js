import { defineConfig } from "vite";

// Configuración de Vite para el dashboard del monitor XM.
//
// El backend FastAPI vive en :8000; el dev server de Vite corre en :5173.
// En desarrollo el proxy de `/api` evita CORS por completo. En un build
// estático (GH Pages) no hay proxy, así que la URL del backend se inyecta
// con `VITE_API_URL` — ver `src/api.js`.
export default defineConfig(({ command }) => ({
  root: ".",

  // GH Pages sirve el sitio bajo `/<repo>/`, no bajo la raíz del dominio,
  // así que los assets del build necesitan ese prefijo (T-INFRA-013). En
  // `vite dev` tiene que quedar en "/" o no carga nada en localhost.
  // `VITE_BASE` permite sobrescribirlo si se despliega en otro hosting
  // (Netlify, un tunnel de cloudflared) donde la raíz sí es "/".
  base:
    process.env.VITE_BASE ??
    (command === "build" ? "/redis-red-electrica-xm/" : "/"),

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
      // El dashboard hace fetch a `/api/*` y Vite lo reenvía al backend.
      // `/api/stream` es SSE: el proxy de Vite lo soporta, pero hay que
      // dejar el buffering desactivado para que los eventos lleguen uno
      // a uno y no en bloque al cerrar.
      "/api": {
        target: process.env.VITE_PROXY_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },

  preview: {
    port: 5173,
    host: "0.0.0.0",
  },
}));
