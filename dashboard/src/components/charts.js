/**
 * Gráficas de líneas con Chart.js (T-DASH-004 y T-DASH-005).
 *
 * Las dos responden preguntas distintas y por eso son dos:
 *
 *   chart-live     ¿el sistema está cubriendo la demanda AHORA?
 *                  Demanda vs generación, ventana corta, alimentado por SSE.
 *   chart-history  ¿cómo viene comportándose la demanda?
 *                  Solo demanda, ventana de 30 min, para ver la curva del día.
 *
 * Se importa `chart.js/auto` para que registre controladores y escalas
 * solo; el import selectivo ahorraría unos KB pero obliga a mantener la
 * lista de registros a mano cada vez que se toca una gráfica.
 */

import Chart from "chart.js/auto";

// Puntos máximos en pantalla. El publisher emite cada 5 s en simulador,
// así que 60 puntos son ~5 min de ventana viva.
const MAX_PUNTOS_VIVO = 60;
// 30 min de histórico a 5 s por punto. El roadmap pide esa ventana.
const MAX_PUNTOS_HISTORICO = 360;

const COLOR_TEXTO = "#8b9aa8";
const COLOR_REJILLA = "rgba(139, 154, 168, 0.12)";

const horaCorta = (iso) =>
  new Date(iso).toLocaleTimeString("es-CO", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

/** Opciones comunes: tema oscuro y sin animación de datos. */
function opcionesBase(tituloY) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    // La animación por defecto interpola cada punto nuevo y con datos que
    // entran cada 5 s la línea se ve "elástica". Sin animación el trazo
    // refleja el dato real.
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: {
        labels: { color: COLOR_TEXTO, boxWidth: 12, usePointStyle: true },
      },
      tooltip: {
        callbacks: {
          label: (ctx) =>
            `${ctx.dataset.label}: ${ctx.parsed.y.toLocaleString("es-CO", {
              maximumFractionDigits: 0,
            })} MW`,
        },
      },
    },
    scales: {
      x: {
        ticks: { color: COLOR_TEXTO, maxTicksLimit: 8, maxRotation: 0 },
        grid: { color: COLOR_REJILLA },
      },
      y: {
        title: { display: true, text: tituloY, color: COLOR_TEXTO },
        ticks: { color: COLOR_TEXTO },
        grid: { color: COLOR_REJILLA },
      },
    },
  };
}

function recortar(chart, maximo) {
  if (chart.data.labels.length <= maximo) return;
  chart.data.labels.shift();
  chart.data.datasets.forEach((ds) => ds.data.shift());
}

/** T-DASH-004 — demanda vs generación en tiempo real. */
export function crearChartVivo(canvas) {
  const chart = new Chart(canvas, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label: "Demanda",
          data: [],
          borderColor: "#58a6ff",
          backgroundColor: "rgba(88, 166, 255, 0.12)",
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.3,
          fill: true,
        },
        {
          label: "Generación",
          data: [],
          borderColor: "#3fb950",
          backgroundColor: "rgba(63, 185, 80, 0.10)",
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.3,
          fill: true,
        },
      ],
    },
    options: opcionesBase("MW"),
  });

  return {
    agregarTick(evento) {
      const d = evento.data;
      chart.data.labels.push(horaCorta(evento.timestamp));
      chart.data.datasets[0].data.push(d.demanda_mw);
      chart.data.datasets[1].data.push(d.generacion_mw);
      recortar(chart, MAX_PUNTOS_VIVO);
      chart.update("none");
    },
  };
}

/** T-DASH-005 — histórico de demanda, ventana de 30 min. */
export function crearChartHistorico(canvas) {
  const chart = new Chart(canvas, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label: "Demanda SIN",
          data: [],
          borderColor: "#e3b341",
          backgroundColor: "rgba(227, 179, 65, 0.12)",
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.3,
          fill: true,
        },
      ],
    },
    options: opcionesBase("MW"),
  });

  return {
    agregarTick(evento) {
      chart.data.labels.push(horaCorta(evento.timestamp));
      chart.data.datasets[0].data.push(evento.data.demanda_mw);
      recortar(chart, MAX_PUNTOS_HISTORICO);
      chart.update("none");
    },
  };
}
