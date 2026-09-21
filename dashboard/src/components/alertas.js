/**
 * Panel de alertas (T-DASH-007).
 *
 * Carga inicial desde `GET /api/alerts` y luego se mantiene al día con los
 * eventos `alert` del SSE, sin volver a consultar el backend.
 *
 * Las alertas resueltas (`state: "cleared"`) se atenúan en vez de
 * desaparecer: durante la demo interesa ver que la alerta se disparó Y que
 * el sistema la resolvió solo. Si se borraran, el operador solo vería una
 * lista vacía y no sabría si nunca pasó nada o si ya se arregló.
 */

import { getAlerts } from "../api.js";

const MAX_VISIBLES = 20;

const NOMBRES = {
  DEMAND_GENERATION_GAP: "Déficit de generación",
  LOW_RENEWABLE: "Renovable bajo",
};

const num = (valor, decimales = 0) =>
  Number(valor).toLocaleString("es-CO", {
    minimumFractionDigits: decimales,
    maximumFractionDigits: decimales,
  });

/**
 * Redacta la alerta en español a partir de los campos estructurados.
 *
 * No se usa `alerta.message` porque el subscriber lo arma en inglés
 * (`"LOW_RENEWABLE active for zone ATL: 19.49 vs threshold 30.00"`) y
 * desentonaría con el resto de la interfaz. Los campos `value` y
 * `threshold` traen lo mismo, así que la traducción es de presentación y
 * no depende de cambiar el backend.
 */
function describir(alerta) {
  if (alerta.rule === "LOW_RENEWABLE") {
    return `Generación renovable en ${num(alerta.value, 1)}% · umbral ${num(alerta.threshold, 1)}%`;
  }
  if (alerta.rule === "DEMAND_GENERATION_GAP") {
    return `Déficit de ${num(alerta.value)} MW · umbral ${num(alerta.threshold)} MW`;
  }
  // Regla desconocida (A3 en el futuro): mejor el texto crudo que nada.
  return alerta.message;
}

const hora = (iso) =>
  new Date(iso).toLocaleTimeString("es-CO", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

const escapar = (texto) =>
  String(texto).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

function plantilla(alerta) {
  const regla = NOMBRES[alerta.rule] ?? alerta.rule;
  const resuelta = alerta.state === "cleared";
  // El subscriber usa `zone_id: ""` como centinela para las alertas de
  // alcance sistémico (A1 evalúa el SIN completo). Un badge vacío no
  // comunica nada, así que lo nombramos.
  const zona = alerta.zone_id === "" ? "SISTEMA" : alerta.zone_id;
  return `
    <div class="alerta sev-${alerta.severity} ${resuelta ? "is-cleared" : ""}">
      <div class="top">
        <span class="regla">${escapar(regla)}${resuelta ? " · resuelta" : ""}</span>
        <span class="hora">${hora(alerta.timestamp)}</span>
      </div>
      <div class="mensaje">${escapar(describir(alerta))}</div>
      <span class="zona">${escapar(zona)}</span>
    </div>`;
}

export function crearPanelAlertas(contenedor) {
  let alertas = [];

  const pintar = () => {
    contenedor.innerHTML = alertas.length
      ? alertas.slice(0, MAX_VISIBLES).map(plantilla).join("")
      : '<p class="vacio">Sin alertas. El sistema opera dentro de los umbrales.</p>';
  };

  pintar();

  return {
    async cargarIniciales() {
      try {
        const { recent } = await getAlerts();
        alertas = recent ?? [];
        pintar();
      } catch (err) {
        console.warn("[alertas] no se pudieron cargar las iniciales:", err);
        contenedor.innerHTML =
          '<p class="vacio">No se pudo consultar el historial de alertas.</p>';
      }
    },

    /** Inserta una alerta recibida por SSE al principio de la lista. */
    agregar(alerta) {
      // El subscriber emite un evento nuevo tanto al disparar como al
      // resolver. Si ya teníamos esa alerta la reemplazamos en su sitio
      // para que no aparezca dos veces en el panel.
      const existente = alertas.findIndex((a) => a.id === alerta.id);
      if (existente >= 0) {
        alertas[existente] = alerta;
      } else {
        alertas.unshift(alerta);
        alertas = alertas.slice(0, MAX_VISIBLES);
      }
      pintar();
    },
  };
}
