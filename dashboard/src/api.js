/**
 * Cliente HTTP del backend.
 *
 * En desarrollo el proxy de Vite reenvía `/api/*` a `localhost:8000`, así
 * que la base queda vacía y no hay CORS. En un build estático (GH Pages)
 * el backend vive en otro dominio y hay que inyectarlo con `VITE_API_URL`.
 */

const BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

export const apiUrl = (ruta) => `${BASE}${ruta}`;

/**
 * GET que devuelve JSON, o lanza con un mensaje legible.
 *
 * Los componentes atrapan el error y pintan su estado de "sin datos" en
 * vez de romper la página: que el mapa falle no puede tumbar los KPIs.
 */
async function getJSON(ruta) {
  const res = await fetch(apiUrl(ruta), { headers: { Accept: "application/json" } });
  if (!res.ok) {
    throw new Error(`${ruta} respondió HTTP ${res.status}`);
  }
  return res.json();
}

export const getHealth = () => getJSON("/api/health");
export const getState = () => getJSON("/api/state");
export const getMetrics = () => getJSON("/api/metrics");
export const getAlerts = () => getJSON("/api/alerts");

/**
 * Dispara un escenario de stress. El backend responde 204 sin cuerpo.
 *
 * Un 400 trae el detalle del error en JSON; lo propagamos tal cual para
 * que el botón muestre el mismo mensaje que daría la CLI del publisher.
 */
export async function postStress(evento) {
  const res = await fetch(apiUrl(`/api/stress/${evento}`), { method: "POST" });
  if (res.status === 204) return;

  let detalle = `HTTP ${res.status}`;
  try {
    const cuerpo = await res.json();
    if (cuerpo?.detail) detalle = cuerpo.detail;
  } catch {
    // Respuesta sin JSON: nos quedamos con el código de estado.
  }
  throw new Error(detalle);
}
