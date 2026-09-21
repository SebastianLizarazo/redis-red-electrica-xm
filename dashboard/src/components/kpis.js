/**
 * Tarjetas de KPI (T-DASH-003).
 *
 * Cuatro indicadores del SIN: demanda, generación, % renovable y balance.
 *
 * Demanda y generación se actualizan desde el evento `tick` del SIN que
 * llega por SSE — son propiedades de una sola lectura y viajan en el
 * propio evento. El % renovable y el balance se leen de `/api/metrics`
 * porque son métricas derivadas que calcula el subscriber, y recalcularlas
 * en el navegador significaría tener la fórmula en dos sitios.
 */

const KPIS = [
  { id: "demanda", label: "Demanda SIN", unidad: "MW" },
  { id: "generacion", label: "Generación SIN", unidad: "MW" },
  { id: "renovable", label: "Generación renovable", unidad: "%" },
  // El nombre lleva la fórmula porque el signo es contraintuitivo:
  // el subscriber define balance = demanda − generación, así que
  // POSITIVO es déficit, no superávit.
  { id: "balance", label: "Balance (dem − gen)", unidad: "MW" },
];

const formatear = (valor, decimales = 0) =>
  valor == null || Number.isNaN(valor)
    ? "—"
    : valor.toLocaleString("es-CO", {
        minimumFractionDigits: decimales,
        maximumFractionDigits: decimales,
      });

export function crearKPIs(contenedor) {
  contenedor.innerHTML = KPIS.map(
    (k) => `
      <div class="card kpi">
        <div class="label">${k.label}</div>
        <div class="value is-empty" id="kpi-${k.id}">—<span class="unit">${k.unidad}</span></div>
      </div>`,
  ).join("");

  const nodos = Object.fromEntries(
    KPIS.map((k) => [k.id, contenedor.querySelector(`#kpi-${k.id}`)]),
  );

  const set = (id, texto, clase = "") => {
    const nodo = nodos[id];
    const unidad = KPIS.find((k) => k.id === id).unidad;
    nodo.className = `value ${clase}`;
    nodo.innerHTML = `${texto}<span class="unit">${unidad}</span>`;
  };

  return {
    /** Actualiza demanda y generación desde un `tick` del SIN. */
    actualizarDesdeTick(evento) {
      const d = evento.data;
      set("demanda", formatear(d.demanda_mw));
      set("generacion", formatear(d.generacion_mw));
    },

    /** Actualiza renovable y balance desde `/api/metrics`. */
    actualizarDesdeMetricas(metricas) {
      const porNombre = Object.fromEntries(metricas.map((m) => [m.name, m.value]));

      const renovable = porNombre.renewable_pct;
      // El umbral de la alerta A2 son 30%: por debajo, la métrica se pinta
      // en el mismo color que tendrá la alerta.
      set(
        "renovable",
        formatear(renovable, 1),
        renovable == null ? "is-empty" : renovable < 30 ? "is-warn" : "is-ok",
      );

      const balance = porNombre.balance_mw;
      // El subscriber define el balance como demanda − generación, así que
      // un valor positivo es DÉFICIT. El umbral de A1 son 800 MW.
      let clase = "is-empty";
      if (balance != null) {
        clase = balance > 800 ? "is-danger" : balance > 0 ? "is-warn" : "is-ok";
      }
      set("balance", formatear(balance), clase);
    },
  };
}
