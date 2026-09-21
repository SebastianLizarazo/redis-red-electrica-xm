/**
 * Botones de prueba de stress (T-DASH-008).
 *
 * Cada botón hace `POST /api/stress/{evento}`, que escribe un flag en
 * Redis con TTL de 30 s. El simulador del publisher lo lee en su siguiente
 * ciclo y aplica el escenario.
 *
 * El feedback avisa que el efecto no es instantáneo: entre el clic y el
 * cambio visible pasa un ciclo del publisher. Sin ese aviso, en la demo
 * parece que el botón no funcionó y uno termina haciendo clic tres veces.
 */

import { postStress } from "../api.js";

const ESCENARIOS = [
  {
    id: "demand_surge",
    nombre: "Pico de demanda",
    desc: "+25% de consumo nacional",
  },
  {
    id: "hydro_drop",
    nombre: "Sequía",
    desc: "Hidráulica al 45%, entra térmica",
  },
  {
    id: "critical_deficit",
    nombre: "Déficit crítico",
    desc: "Generación cae, dispara alerta",
  },
  {
    id: "recovery",
    nombre: "Recuperación",
    desc: "Limpia los escenarios activos",
  },
];

export function crearBotonesStress(contenedor, feedback) {
  contenedor.innerHTML = ESCENARIOS.map(
    (e) => `
      <button class="stress-btn" data-evento="${e.id}">
        <span class="nombre">${e.nombre}</span>
        <span class="desc">${e.desc}</span>
      </button>`,
  ).join("");

  const disparar = async (boton) => {
    const evento = boton.dataset.evento;
    boton.disabled = true;
    boton.classList.remove("ok", "error");
    feedback.textContent = `Enviando «${evento}»…`;

    try {
      await postStress(evento);
      boton.classList.add("ok");
      feedback.textContent =
        `Escenario «${evento}» activado (vigente 30 s). ` +
        "El efecto aparece en el próximo ciclo del publisher.";
    } catch (err) {
      boton.classList.add("error");
      feedback.textContent = `No se pudo activar «${evento}»: ${err.message}`;
    } finally {
      boton.disabled = false;
      setTimeout(() => boton.classList.remove("ok", "error"), 2500);
    }
  };

  contenedor.addEventListener("click", (ev) => {
    const boton = ev.target.closest(".stress-btn");
    if (boton) disparar(boton);
  });
}
