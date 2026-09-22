# ROADMAP — `redis-red-electrica-xm`

> Estado del proyecto al cierre del taller.
> **Demo programada**: martes 22-sept-2026.
> **Entregables finales**: [`README.md`](README.md) +
> [`GUION_PRESENTACION.md`](GUION_PRESENTACION.md) +
> [`docs/README_TECNICO.md`](docs/README_TECNICO.md) +
> [`docs/DEPLOY.md`](docs/DEPLOY.md) +
> [`docs/TESTING.md`](docs/TESTING.md).
>
> Para el detalle técnico de modelos, keys Redis y contratos, ver
> [`docs/README_TECNICO.md`](docs/README_TECNICO.md). Este roadmap es
> operativo.

## Estado actual

- **Branch**: `main` limpio en `77ac4d7`.
- **Tests**: 133 tests verde (`pytest --collect-only`), sin red, usando
  `fakeredis`.
- **Lint + tipos**: `ruff check .` + `mypy .` limpios en el baseline.
- **Pipeline**: Redis + publisher + subscriber + API + dashboard
  levantados por `docker compose -f infra/docker-compose.yml up` (Plan
  A en [`docs/DEPLOY.md`](docs/DEPLOY.md)).
- **PRs mergeados**: #1 al #13 (scaffolding, publisher, subscriber,
  SUB-002 fix, api-core, docs bloque rojo, dashboard + infra).

## Entregables finales del taller

| Entregable | Path | Estado |
|------------|------|--------|
| Codigo fuente | todo el repo | listo |
| Documento tecnico | [`docs/README_TECNICO.md`](docs/README_TECNICO.md) | listo |
| Guia de despliegue | [`docs/DEPLOY.md`](docs/DEPLOY.md) | listo |
| Guia de testing | [`docs/TESTING.md`](docs/TESTING.md) | listo |
| README raiz (audiencia general) | [`README.md`](README.md) | listo |
| Guion de presentacion (sin diapositivas) | [`GUION_PRESENTACION.md`](GUION_PRESENTACION.md) | listo |

## Demo en vivo

Cobertura de los 9 pasos del enunciado en
[`GUION_PRESENTACION.md`](GUION_PRESENTACION.md):

1. Publisher en marcha.
2. Generar un nuevo evento (modo automatico + stress).
3. Evento en Redis (Pub/Sub + Stream + Hash).
4. Procesamiento por el subscriber (M1/M2/M3 + alertas).
5. Dashboard actualizandose sin recargar.
6. Condicion de alerta (Déficit critico o Pico de demanda).
7. Cambio visual del dashboard (KPI en rojo, panel de alertas).
8. Estado actual (`GET /api/state` + `state:zone:*` + `state:sin`).
9. Historico reciente (`XLEN energy:stream`, ZRANGE, `/api/alerts`).

Planes de contingencia documentados:

- **Plan A** — demo local con `make up` (preferido).
- **Plan B** — tunnel `cloudflared` si el profesor quiere ver la demo
  desde otra maquina.

Detalles en [`docs/DEPLOY.md`](docs/DEPLOY.md) secciones 1-2.

## Pendientes menores (no bloquean la entrega)

Items que quedaron abiertos despues del cierre, sin impacto sobre la
demo ni la entrega del 22-sept. Listados aqui como referencia para
una posible segunda iteracion del taller.

### Codigo

- **SUB-001** (deferred) — `_failures` en `subscriber/processor.py` es
  in-memory. La spec original pedia 3 contadores Redis INCR separados
  para visibilidad desde el dashboard. El contrato funcional esta
  preservado (counter + degrade + ERROR log al llegar al threshold);
  el seguimiento desde Redis se decidio aceptar como deuda tecnica.
  Ver `subscriber/processor.py:EnergyProcessor.__init__`.

### Documentacion

- **T-DOC-005** — typos menores en `docs/SMOKE_TEST_subscriber.md`
  (`XM_FORCE_SOURCE` → `FORCE_SOURCE`, `docker compose` sin `-f`). No
  bloquea la entrega; el documento ya no es la guia principal porque
  el flujo esta cubierto en `docs/DEPLOY.md`.

## Decisiones que se aceptaron como deuda tecnica

Items originalmente identificados como warnings o follow-ups que el
equipo decidio NO resolver durante el taller. Documentados para
visibilidad, no como pendientes.

- **R3-002** — `force_source=real` no persiste salud cuando XM falla.
  Aceptado: el banner del dashboard puede quedar stale durante la
  ventana de retry, comportamiento conocido.
- **R3-003** — `consecutive_failures` crece sin tope en modo SIM.
  Aceptado: el operador puede leer el contador y resetear manualmente
  si lo necesita; no hay impacto operacional en la ventana del taller.
- **R4-004** — shutdown latency hasta 5 min en REAL mode bajo SIGKILL.
  Aceptado: el loop de 5 minutos es intencional, refleja la cadencia
  regulatoria de XM.
- **R4-005** — XM retry backoff sin jitter. Aceptado: el despliegue
  es de instancia unica, no hay thundering-herd que mitigar.
- **R4-006** — REAL-mode wait de 5 min retrasa deteccion de fallos.
  Aceptado: trade-off entre cadencia regulatoria y velocidad de
  fallback.
- **R4-007** — `HSET + EXPIRE` no atomico. Aceptado: el publisher
  usa `pipeline(transaction=True)` que garantiza atomicidad del
  grupo de comandos en una sola instancia de Redis. La nota original
  sobre Lua EVAL queda para una segunda iteracion.
- **R4-008** — SIGTERM no se maneja en Windows. Aceptado: el codigo
  usa `contextlib.suppress(NotImplementedError)` y depende de
  `KeyboardInterrupt` en Windows. Funcional para el entorno de
  desarrollo.

## Validaciones manuales para la demo

Items que no son chequeables por tests automatizados (requieren
intervencion humana o Redis real). Todos estan cubiertos por el guion
de presentacion.

- Validar que `make up` levanta los 4 contenedores en estado healthy.
- Validar que `curl http://localhost:8000/api/health` responde 200.
- Validar que el dashboard en `http://localhost:5173` actualiza KPIs
  sin recargar.
- Validar el switch REAL → DEGRADADO → SIMULADOR con
  `XM_TIMEOUT_SECONDS=0.001` o forzando `health:failures=3`.
- Validar al menos una alerta en vivo con el boton `critical_deficit`.
- Validar los 4 escenarios de stress desde los botones del dashboard.

---

**Owner**: Sebastian (lider tecnico del taller). **Proxima accion**:
demo en vivo martes 22-sept-2026.
