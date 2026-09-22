/**
 * Cliente SSE (T-DASH-009).
 *
 * Se conecta a `GET /api/stream` y reparte los eventos a quien se
 * suscriba. El backend emite tres tipos, discriminados por el campo
 * `type` del JSON y reflejados en el `event:` del protocolo SSE:
 *
 *   tick           — una lectura de zona o del SIN (publisher)
 *   alert          — alerta disparada o resuelta (subscriber)
 *   source_switch  — el publisher cambió de fuente (XM ↔ simulador)
 *
 * Por qué no basta con `EventSource` a secas: el reconnect nativo
 * reintenta cada ~3 s indefinidamente. Si el backend está caído durante
 * la demo, eso son cientos de peticiones fallidas llenando la consola.
 * Aquí el backoff crece hasta 30 s y avisa del estado de la conexión
 * para que el banner pueda mostrar que se perdió el enlace.
 */

import { apiUrl } from "./api.js";

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

export function crearClienteSSE() {
  const manejadores = new Map(); // tipo de evento -> [callback]
  let fuente = null;
  let intentos = 0;
  let timerReconexion = null;
  let cerradoAdrede = false;

  const emitir = (tipo, dato) => {
    for (const cb of manejadores.get(tipo) ?? []) {
      try {
        cb(dato);
      } catch (err) {
        // Un componente que revienta no puede tumbar a los demás ni
        // matar la conexión SSE.
        console.error(`[sse] handler de "${tipo}" falló:`, err);
      }
    }
  };

  const escuchar = (tipo) => {
    fuente.addEventListener(tipo, (ev) => {
      let payload;
      try {
        payload = JSON.parse(ev.data);
      } catch {
        console.warn(`[sse] evento "${tipo}" con JSON inválido, ignorado`);
        return;
      }
      emitir(tipo, payload);
    });
  };

  const conectar = () => {
    if (cerradoAdrede) return;

    fuente = new EventSource(apiUrl("/api/stream"));

    fuente.onopen = () => {
      intentos = 0;
      emitir("_conexion", { conectado: true });
    };

    ["tick", "alert", "source_switch"].forEach(escuchar);

    fuente.onerror = () => {
      // EventSource no distingue "backend caído" de "corte pasajero", así
      // que cerramos siempre y reintentamos nosotros con backoff.
      fuente.close();
      emitir("_conexion", { conectado: false });
      if (cerradoAdrede) return;

      const espera = Math.min(RECONNECT_BASE_MS * 2 ** intentos, RECONNECT_MAX_MS);
      intentos += 1;
      clearTimeout(timerReconexion);
      timerReconexion = setTimeout(conectar, espera);
    };
  };

  return {
    /** Registra un callback. `_conexion` avisa de conexión/desconexión. */
    on(tipo, callback) {
      if (!manejadores.has(tipo)) manejadores.set(tipo, []);
      manejadores.get(tipo).push(callback);
      return this;
    },
    conectar() {
      cerradoAdrede = false;
      conectar();
      return this;
    },
    cerrar() {
      cerradoAdrede = true;
      clearTimeout(timerReconexion);
      fuente?.close();
    },
  };
}
