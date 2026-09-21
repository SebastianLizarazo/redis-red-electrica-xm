/**
 * Entry point del dashboard.
 *
 * Arma los componentes y reparte los datos. Dos fuentes, cada una con su
 * razón de ser:
 *
 *   SSE (`/api/stream`)  — lo que cambia constantemente: ticks y alertas.
 *                          Empuja el dato apenas ocurre, sin preguntar.
 *   Polling              — lo que el SSE no emite: el estado de salud
 *                          (banner, cada 5 s) y las métricas derivadas que
 *                          calcula el subscriber (cada 5 s).
 *
 * La carga inicial sí usa HTTP: al abrir la página el SSE todavía no ha
 * entregado nada, y en modo real el siguiente tick puede tardar 5 minutos.
 * Sin ese primer fetch el dashboard se vería vacío en la demo.
 */

import "./styles.css";

import { getMetrics, getState } from "./api.js";
import { crearClienteSSE } from "./sse.js";
import { crearBanner } from "./components/banner.js";
import { crearKPIs } from "./components/kpis.js";
import { crearChartHistorico, crearChartVivo } from "./components/charts.js";
import { crearMapa } from "./components/mapa.js";
import { crearPanelAlertas } from "./components/alertas.js";
import { crearBotonesStress } from "./components/stress.js";

const METRICAS_REFRESCO_MS = 5000;

const $ = (sel) => document.querySelector(sel);

function iniciar() {
  const banner = crearBanner($("#banner"));
  const kpis = crearKPIs($("#kpis"));
  const chartVivo = crearChartVivo($("#chart-live"));
  const chartHistorico = crearChartHistorico($("#chart-history"));
  const mapa = crearMapa($("#mapa"));
  const alertas = crearPanelAlertas($("#alertas"));
  crearBotonesStress($("#stress"), $("#stress-feedback"));

  // --- Carga inicial ------------------------------------------------------
  getState()
    .then((estado) => {
      mapa.cargarEstado(estado);
      if (estado.sin) {
        kpis.actualizarDesdeTick({
          timestamp: estado.sin.timestamp,
          data: {
            demanda_mw: estado.sin.demanda_mw,
            generacion_mw: estado.sin.generacion_mw,
          },
        });
      }
    })
    .catch((err) => console.warn("[main] no se pudo cargar el estado inicial:", err));

  alertas.cargarIniciales();

  // --- Métricas derivadas (polling) --------------------------------------
  const refrescarMetricas = () =>
    getMetrics()
      .then((m) => kpis.actualizarDesdeMetricas(m))
      .catch((err) => console.warn("[main] métricas no disponibles:", err));

  refrescarMetricas();
  setInterval(refrescarMetricas, METRICAS_REFRESCO_MS);

  // --- Tiempo real (SSE) --------------------------------------------------
  crearClienteSSE()
    .on("tick", (evento) => {
      if (evento.entity_id === "SIN") {
        // El consolidado nacional alimenta KPIs y gráficas.
        kpis.actualizarDesdeTick(evento);
        chartVivo.agregarTick(evento);
        chartHistorico.agregarTick(evento);
      } else {
        // Los ticks de zona solo mueven el mapa.
        mapa.actualizarDesdeTick(evento);
      }
    })
    .on("alert", (alerta) => alertas.agregar(alerta))
    .on("source_switch", (evento) => {
      // El publisher cambió de fuente. Refrescamos el banner de inmediato
      // en vez de esperar hasta 5 s al siguiente poll: es el momento que
      // más se mira durante la demo del fallback.
      console.info(`[main] la fuente cambió a "${evento.mode}"`);
      banner.refrescar();
    })
    .on("_conexion", ({ conectado }) => {
      if (!conectado) console.warn("[main] SSE desconectado, reintentando…");
    })
    .conectar();
}

iniciar();
