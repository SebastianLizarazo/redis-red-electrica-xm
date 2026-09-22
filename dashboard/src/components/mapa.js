/**
 * Mapa de zonas con Leaflet (T-DASH-006).
 *
 * Cinco marcadores sobre Colombia, uno por zona del SIN. El color dice si
 * la zona cubre su demanda: verde si genera de más, rojo si tiene déficit.
 * Al hacer clic se abre el detalle con el desglose por recurso.
 *
 * Dos decisiones que vale la pena explicar:
 *
 * 1. `circleMarker` en vez de los pines por defecto. Los iconos de Leaflet
 *    se cargan como imágenes relativas y con el bundling de Vite terminan
 *    en 404; la solución habitual es re-apuntar `L.Icon.Default` a mano.
 *    Un círculo vectorial evita el problema y además se colorea según el
 *    estado, que es justo lo que pide el ticket.
 *
 * 2. Las coordenadas viven aquí. `/api/state` devuelve `ZoneState`, que no
 *    incluye `location`; solo los eventos `tick` la traen. Para poder
 *    pintar el mapa antes del primer tick necesitamos las posiciones en
 *    el cliente. Son un espejo de `ZONE_PROFILES` en
 *    `publisher/normalizer.py` — si allá cambian, acá también.
 */

import L from "leaflet";
import "leaflet/dist/leaflet.css";

const ZONAS = {
  BOG: { nombre: "Bogotá - Cundinamarca", lat: 4.711, lon: -74.0721 },
  ANT: { nombre: "Antioquia", lat: 6.2442, lon: -75.5812 },
  ATL: { nombre: "Atlántico - Caribe", lat: 10.9639, lon: -74.7964 },
  VAL: { nombre: "Valle del Cauca", lat: 3.4516, lon: -76.532 },
  SAN: { nombre: "Santander", lat: 7.1193, lon: -73.1227 },
};

const COLOR_OK = "#3fb950";
const COLOR_DEFICIT = "#f85149";
const COLOR_SIN_DATO = "#8b9aa8";

const mw = (valor) =>
  valor == null
    ? "—"
    : `${valor.toLocaleString("es-CO", { maximumFractionDigits: 0 })} MW`;

function popup(zonaId, estado) {
  const { nombre } = ZONAS[zonaId];
  if (!estado) {
    return `<div class="popup-zona"><strong>${nombre}</strong>Sin datos todavía</div>`;
  }

  const balance = estado.demanda_mw - estado.generacion_mw;
  const g = estado.generacion ?? {};
  // El desglose por recurso solo llega en los eventos `tick`; si la zona
  // se pintó desde `/api/state` todavía no lo tenemos.
  const desglose = estado.generacion
    ? `
      <tr><td>Hidráulica</td><td>${mw(g.hidraulica)}</td></tr>
      <tr><td>Térmica</td><td>${mw(g.termica)}</td></tr>
      <tr><td>Solar</td><td>${mw(g.solar)}</td></tr>
      <tr><td>Eólica</td><td>${mw(g.eolica)}</td></tr>`
    : "";

  return `
    <div class="popup-zona">
      <strong>${nombre}</strong>
      <table>
        <tr><td>Demanda</td><td>${mw(estado.demanda_mw)}</td></tr>
        <tr><td>Generación</td><td>${mw(estado.generacion_mw)}</td></tr>
        <tr><td>${balance > 0 ? "Déficit" : "Excedente"}</td><td>${mw(Math.abs(balance))}</td></tr>
        ${desglose}
      </table>
    </div>`;
}

export function crearMapa(elemento) {
  const mapa = L.map(elemento, {
    center: [5.5, -74.5],
    zoom: 5,
    scrollWheelZoom: false, // hacer scroll en la página no debe hacer zoom
  });

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 12,
  }).addTo(mapa);

  const marcadores = {};
  const estados = {};

  for (const [id, zona] of Object.entries(ZONAS)) {
    marcadores[id] = L.circleMarker([zona.lat, zona.lon], {
      radius: 12,
      color: "#0f1419",
      weight: 2,
      fillColor: COLOR_SIN_DATO,
      fillOpacity: 0.85,
    })
      .addTo(mapa)
      .bindTooltip(zona.nombre, { direction: "top" })
      .bindPopup(popup(id, null));
  }

  const repintar = (zonaId) => {
    const estado = estados[zonaId];
    const marcador = marcadores[zonaId];
    if (!marcador) return;

    let color = COLOR_SIN_DATO;
    if (estado) {
      color = estado.generacion_mw >= estado.demanda_mw ? COLOR_OK : COLOR_DEFICIT;
    }
    marcador.setStyle({ fillColor: color });
    marcador.setPopupContent(popup(zonaId, estado));
  };

  return {
    /** Carga inicial desde `/api/state` (sin desglose por recurso). */
    cargarEstado(respuesta) {
      for (const zona of respuesta.zones ?? []) {
        if (!ZONAS[zona.zone_id]) continue;
        estados[zona.zone_id] = {
          demanda_mw: zona.demanda_mw,
          generacion_mw: zona.generacion_mw,
        };
        repintar(zona.zone_id);
      }
    },

    /** Actualización en vivo desde un `tick` de zona (trae desglose). */
    actualizarDesdeTick(evento) {
      const id = evento.entity_id;
      if (!ZONAS[id]) return; // el tick del SIN no va al mapa

      const d = evento.data;
      estados[id] = {
        demanda_mw: d.demanda_mw,
        generacion_mw: d.generacion_mw,
        generacion: {
          hidraulica: d.generacion_hidraulica_mw,
          termica: d.generacion_termica_mw,
          solar: d.generacion_solar_mw,
          eolica: d.generacion_eolica_mw,
        },
      };
      repintar(id);
    },
  };
}
