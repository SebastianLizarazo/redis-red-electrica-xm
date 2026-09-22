/**
 * Banner de modo (T-DASH-002).
 *
 * Los tres estados que pide el roadmap salen de dos campos de
 * `/api/health`, no de uno:
 *
 *   REAL       mode=real   y failures=0   → XM respondiendo
 *   DEGRADADO  mode=real   y failures>0   → XM falla, aún no conmuta
 *   SIMULADOR  mode=simulator             → conmutado, datos sintéticos
 *
 * El estado degradado existe porque el publisher solo cambia de fuente
 * tras 3 fallos seguidos. En esa ventana el modo sigue diciendo "real"
 * pero los datos ya vienen del simulador, y el operador tiene que
 * enterarse. Sin este matiz el banner mentiría durante varios minutos.
 */

import { getHealth } from "../api.js";

const REFRESCO_MS = 5000;

export function crearBanner(elemento) {
  let sinConexion = false;

  const pintar = (clase, texto, detalle = "") => {
    elemento.className = `banner ${clase}`;
    elemento.innerHTML = `
      <span class="dot"></span>
      <span class="texto">${texto}</span>
      ${detalle ? `<span class="detail">${detalle}</span>` : ""}
    `;
  };

  const refrescar = async () => {
    try {
      const salud = await getHealth();
      sinConexion = false;

      if (salud.mode === "simulator") {
        pintar(
          "is-sim",
          "MODO: SIMULADOR",
          salud.source_switches > 0
            ? `conmutado ${salud.source_switches}× · XM no responde`
            : "datos sintéticos",
        );
      } else if (salud.failures > 0) {
        pintar(
          "is-degraded",
          "MODO: DEGRADADO",
          `${salud.failures} fallo(s) de XM · aún no conmuta`,
        );
      } else {
        pintar("is-real", "MODO: REAL", "XM respondiendo");
      }
    } catch {
      // El backend no responde. Distinto de "XM caído": aquí el que no
      // está es nuestro propio API, y el dashboard entero está ciego.
      if (!sinConexion) {
        sinConexion = true;
        pintar("is-down", "SIN CONEXIÓN", "el backend no responde");
      }
    }
  };

  refrescar();
  const timer = setInterval(refrescar, REFRESCO_MS);

  return {
    refrescar,
    destruir: () => clearInterval(timer),
  };
}
