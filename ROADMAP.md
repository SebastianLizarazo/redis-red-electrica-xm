# ROADMAP — `redis-red-electrica-xm`

> **Inicio**: Sábado 19 sept 2026 · **Entrega**: Martes 22 sept 2026 (4 días)
> **Estrategia**: Paralelismo donde se pueda, sincronización al final
> **Para detalles técnicos**: ver [`docs/README_TECNICO.md`](docs/README_TECNICO.md) — este roadmap es operativo, el README es la fuente de verdad técnica

## Tablero de progreso general

| Día | Fecha | Estado | Foco |
|-----|-------|--------|------|
| Día 1 | Sábado 19 | ✅ cerrado | Scaffolding |
| Día 2 | Domingo 20 | ⬜ | Publisher + Subscriber + Redis (conexión) |
| Día 3 | Lunes 21 | ⬜ | API + Frontend (conexión) |
| Día 4 | Martes 22 | ⬜ | Integración end-to-end + docs + demo |

## Orden sugerido de ejecución

```
DÍA 1 (sábado 19) ──► todos en paralelo sobre la base de Fase 0
  Edwar       arranca publisher/xm_client + simulator + normalizer
  Alejandro   arranca subscriber/metrics + alerts (lógica pura)
  David       arranca dashboard setup Vite + componentes standalone (banner, estilos)
  Jonathan    arranca infra/Dockerfile.publisher + .subscriber + .api
  Sebastián   (Fase 0 cerrada) + specs guías para los demás

DÍA 2 (domingo 20) ──► conexión publisher ↔ subscriber ↔ Redis
  Edwar       cierra publisher/main.py + source_selector.py + tests
  Alejandro   cierra subscriber/processor.py + main.py + tests con fakeredis
  David       componentes que leen estado (KPIs, line chart)
  Jonathan    actualiza docker-compose con publisher + subscriber + healthchecks
  Sebastián   integración local publisher↔subscriber↔Redis (smoke end-to-end)

DÍA 3 (lunes 21) ──► conexión API ↔ Frontend
  Alejandro   api/routers/*.py + tests
  David       cliente SSE + conecta todos los componentes
  Jonathan    build de Vite + GH Pages workflow
  Edwar + Seb fixes, validación de métricas, ajustes

DÍA 4 (martes 22) ──► integración final + entrega
  Todos       validación end-to-end completa (publisher→Redis→subscriber→API→frontend)
  Sebastián   DEPLOY.md + TESTING.md + slides de presentación + demo
```

---

## Edwar — `publisher/` (Publisher del sistema)

> **Refs técnicas útiles**: [`docs/README_TECNICO.md` §6.1 Modelos Pydantic](docs/README_TECNICO.md#61-modelos-pydantic-commonmodelspy), [§6.2 Keys Redis](docs/README_TECNICO.md#62-keys-redis-commonredis_keyspy), [§6.3 Protocol DataSource](docs/README_TECNICO.md#63-protocol-datasource-commondata_sourcepy)

- [x] **T-PUB-001** — Implementar `publisher/xm_client.py`
  - Cliente HTTP async (`httpx`) contra `DemandaTiempoReal`
  - Manejo de timeout (10s configurable), 3 reintentos con backoff exponencial
  - Conversión de excepciones a `DataSourceError` / `DataSourceTimeoutError`
  - Ref de errores: §6.3 del README técnico (excepciones específicas)

- [x] **T-PUB-002** — Implementar `publisher/simulator.py`
  - Genera eventos sintéticos realistas con patrones circadianos (valle 3-5am, pico 19h)
  - 4 eventos anómalos inyectables: `demand_surge`, `hydro_drop`, `critical_deficit`, `recovery`
  - Endpoint REST para inyectarlos (referencia: `stress_key` en §6.2)
  - Publica eventos de las 5 zonas (ANT/VAL/ATL/BOG/SAN) + global SIN

- [x] **T-PUB-003** — Implementar `publisher/normalizer.py`
  - Transforma respuesta cruda de XM o simulador al formato `Event` definido en §6.1
  - Validación con Pydantic, descarte de valores fuera de rango
  - Mism formato para XM y simulador (publisher agnóstico de fuente)

- [x] **T-PUB-004** — Implementar `publisher/source_selector.py`
  - Implementa Protocol `DataSource` (§6.3) con selector entre XM y simulador
  - Lógica de fallback: 3 fallos consecutivos → switch automático a simulador
  - Backoff de reintento: 5 → 10 → 20 → 30 min (ver §6.2 tabla TTLs)
  - Escribe `health:mode` y `health:failures` en Redis

- [x] **T-PUB-005** — Implementar `publisher/main.py`
  - Orquestador async: decide fuente → fetch → normaliza → PUBLISH a `energy-events` (Pub/Sub) + XADD a `energy:stream` (Stream con MAXLEN 1000)
  - XADDs `state:zone:<id>` (Hash) por cada evento
  - Loop con intervalo configurable (5s simulador, 300s real)

- [x] **T-PUB-006** — Tests unitarios de `normalizer.py` (TDD estándar)
- [x] **T-PUB-007** — Tests de `xm_client.py` con `respx` mockeando API XM
- [x] **T-PUB-008** — Tests de `source_selector.py` (3 fallos → switch)

---

## Alejandro — `subscriber/` + `api/` (Processor + API REST + SSE)

> **Refs técnicas útiles**: [`docs/README_TECNICO.md` §6.2 Keys Redis](docs/README_TECNICO.md#62-keys-redis-commonredis_keyspy), [§7 mapeo a rúbrica criterios "Procesamiento" y "Comunicación real-time"](docs/README_TECNICO.md#7-mapeo-a-la-rúbrica-del-taller-sección-27)

### Bloque Subscriber (Día 1-2)

- [ ] **T-SUB-001** — Implementar `subscriber/metrics.py`
  - **M1 (% renovable)**: `(solar + eólica + hidro) / generación_total * 100`
  - **M2 (balance demanda-generación)**: `demanda - generación_total`
  - **M3 (variación % demanda)**: `(demanda_actual - demanda_ventana_atras) / demanda_ventana_atras * 100`
  - Persiste en Hash `metrics` y ZSet `metrics:demand:history` (score = timestamp)

- [ ] **T-SUB-002** — Implementar `subscriber/alerts.py`
  - **A1 (déficit crítico)**: dispara si `demanda - generación > 800 MW` (severidad HIGH)
  - **A2 (% renovable bajo)**: dispara si `% renovable < 30%` (severidad MEDIUM)
  - Debounce de 2 ciclos (no spamear alertas repetidas)
  - Escribe en Pub/Sub con `type=alert` y en List `alerts:recent` (LPUSH + LTRIM 0 19)
  - Contador `alerts:total` con INCR

- [ ] **T-SUB-003** — Implementar `subscriber/processor.py`
  - SUBSCRIBE a `energy-events` con `get_message(timeout=1.0)` (permite housekeeping 60s)
  - Para cada evento: leer state anterior → calcular M1/M2/M3 → evaluar A1/A2 → escribir a Hashes y Stream
  - Discriminador `type=alert` vs `type=tick` (re-emite alertas por canal separado si querés)

- [ ] **T-SUB-004** — Implementar `subscriber/main.py`
  - Setup Redis async, registra signal handlers (SIGTERM → shutdown limpio)
  - Housekeeping cada 60s: `ZREMRANGEBYSCORE` en `metrics:demand:history` (mantener ventana 1h)
  - Healthcheck en `health:uptime` (timestamp de arranque)

- [ ] **T-SUB-005** — Tests unitarios de `metrics.py` (TDD estándar)
- [ ] **T-SUB-006** — Tests unitarios de `alerts.py` (TDD estándar)
- [ ] **T-SUB-007** — Tests de integración de `processor.py` con `fakeredis`

### Bloque API REST + SSE (Día 3)

- [ ] **T-API-001** — Implementar `api/server.py`
  - FastAPI app + `CORSMiddleware` con origins explícitos (localhost:5173 + dominio GH Pages)
  - Routers: state, metrics, alerts, stream, health, stress
  - Lifespan context para abrir/cerrar cliente Redis async

- [ ] **T-API-002** — `api/routers/state.py` — `GET /api/state` → estado actual por zona
- [ ] **T-API-003** — `api/routers/metrics.py` — `GET /api/metrics` → M1/M2/M3 actuales
- [ ] **T-API-004** — `api/routers/alerts.py` — `GET /api/alerts` → alertas recientes (lista) + activas (hash)
- [ ] **T-API-005** — `api/routers/stream.py` — `GET /api/stream` (SSE)
  - Emite eventos `tick`, `alert`, `source_switch` en formato SSE
  - Heartbeat cada 15s para mantener conexión
- [ ] **T-API-006** — `api/routers/health.py` — `GET /api/health` → `HealthStatus`
- [ ] **T-API-007** — `api/routers/stress.py` — `POST /api/stress/{event}`
  - Inyecta evento anómalo (demand_surge / hydro_drop / critical_deficit / recovery)
- [ ] **T-API-008** — Tests de los routers con `httpx` TestClient

---

## David — `dashboard/` (Frontend + Visualización)

> **Refs técnicas útiles**: [`docs/README_TECNICO.md` §3 estructura dashboard](docs/README_TECNICO.md#3-estructura-del-repositorio-fase-0), [§7 criterio "Dashboard"](docs/README_TECNICO.md#7-mapeo-a-la-rúbrica-del-taller-sección-27)

- [ ] **T-DASH-001** — Setup Vite completo (si no quedó hecho en Fase 0)
  - `vite.config.js` con server en `0.0.0.0:5173`, build a `dist/`
  - `index.html` con contenedor root y carga `src/main.js`

- [ ] **T-DASH-002** — Componente Banner de modo
  - Lee `GET /api/health` cada 5s
  - Muestra "MODO: REAL" (verde) / "MODO: SIMULADOR" (amarillo) / "MODO: DEGRADADO" (rojo)

- [ ] **T-DASH-003** — Componente KPI cards (4 KPIs)
  - Demanda actual (MW), Generación actual (MW), % renovable, Balance neto
  - Auto-refresh desde SSE o polling 5s

- [ ] **T-DASH-004** — Componente LineChart (Chart.js) — Demanda vs Generación en tiempo real
  - Línea demanda (azul), línea generación (verde), eje X = tiempo

- [ ] **T-DASH-005** — Componente LineChart histórico de demanda
  - Datos desde `GET /api/metrics` o vía SSE, ventana últimos 30 min

- [ ] **T-DASH-006** — Componente Mapa (Leaflet)
  - Mapa de Colombia centrado, 5 markers para ANT/VAL/ATL/BOG/SAN
  - Color del marker según balance (verde = generación > demanda, rojo = déficit)
  - Click → muestra detalles de la zona

- [ ] **T-DASH-007** — Componente Panel de alertas
  - Lista de alertas recientes desde `GET /api/alerts`
  - Color por severidad (HIGH rojo, MEDIUM amarillo, LOW gris)
  - Auto-refresh vía SSE cuando llega `type=alert`

- [ ] **T-DASH-008** — Componente Botones de stress test
  - 4 botones: demand_surge, hydro_drop, critical_deficit, recovery
  - `POST /api/stress/{event}` y feedback visual

- [ ] **T-DASH-009** — Cliente SSE
  - Conexión a `GET /api/stream`, parsea eventos, dispatch a componentes
  - Reconnect automático con backoff si se cae

- [ ] **T-DASH-010** — Estilos finales (CSS) — diseño limpio, paleta de colores consistente

---

## Jonathan — `infra/` + CI/CD

> **Refs técnicas útiles**: [`docs/README_TECNICO.md` §5 cómo arrancar](docs/README_TECNico.md#5-cómo-arrancar-fase-0), [§3 infra](docs/README_TECNICO.md#3-estructura-del-repositorio-fase-0)

- [ ] **T-INFRA-008** — `infra/Dockerfile.publisher` (Python 3.12-slim + deps runtime)
- [ ] **T-INFRA-009** — `infra/Dockerfile.subscriber`
- [ ] **T-INFRA-010** — `infra/Dockerfile.api`
- [ ] **T-INFRA-011** — Actualizar `infra/docker-compose.yml`
  - Servicios: redis, publisher, subscriber, api + dashboard-dev (opcional)
  - Red común `red-electrica-net`
  - Healthchecks por servicio
  - Volumen para datos Redis (opcional, debug)

- [ ] **T-INFRA-012** — `.github/workflows/ci.yml`
  - Job: install deps → `ruff check .` → `mypy .` → `pytest --cov`
  - Trigger en PRs y push a main

- [ ] **T-INFRA-013** — Configurar deploy del frontend
  - `.github/workflows/pages.yml` que buildea Vite y deploya `dist/` a GH Pages
  - Actualizar `vite.config.js` con `base: '/redis-red-electrica-xm/'`

---

## Sebastián — Integración + Documentación + Demo

> **Refs técnicas útiles**: [`docs/README_TECNICO.md`](docs/README_TECNICO.md) completo (sección §7 mapeo a rúbrica es la guía para el documento técnico final)

### Bloque Integración (Días 2-4)

- [ ] **T-INT-006** — Smoke test end-to-end local
  - `make up` (Redis + publisher + subscriber + api)
  - Verificar publisher PUBLISH → subscriber SUBSCRIBE → métricas → estado

- [ ] **T-INT-007** — Validar SSE end-to-end
  - `curl -N http://localhost:8000/api/stream` y ver eventos llegando

- [ ] **T-INT-008** — Validar fallback XM → simulador
  - Forzar 3 timeouts en XM (config `XM_TIMEOUT_SECONDS=0.001`), ver banner cambiar

- [ ] **T-INT-009** — Validar alertas
  - Forzar `demand_surge` desde botón del dashboard → ver alerta en panel

- [ ] **T-INT-010** — Validar stress tests desde dashboard
  - Cada uno de los 4 botones funciona y se refleja en KPI/charts

### Bloque Documentación (Día 4)

- [ ] **T-DOC-001** — Completar [`docs/README_TECNICO.md`](docs/README_TECNICO.md) con secciones finales
  - §8 Difficultades encontradas (consolidar lo que reportó cada uno)
  - §9 Próximos pasos (marcar como completado)
  - Agregar: §10 Conclusiones + §11 Referencias + §12 Lecciones aprendidas

- [ ] **T-DOC-002** — `docs/DEPLOY.md`
  - Setup local paso a paso (`make up` → abrir `localhost:5173`)
  - Deploy del frontend a GH Pages (manual + Actions)
  - Plan B con `cloudflared tunnel` para demo remota
  - Plan C: video de respaldo

- [ ] **T-DOC-003** — `docs/TESTING.md`
  - Cómo correr la suite (`make test`, `make test-cov`)
  - Qué testea cada archivo (`tests/unit/`, `tests/integration/`)
  - Cómo agregar nuevos tests (convenciones, fixtures disponibles en `conftest.py`)

- [ ] **T-DOC-004** — `docs/INTEGRANTES.md` (opcional pero recomendado)
  - Quién hizo qué, con links a commits
  - Reflexión individual sobre qué aprendieron

### Bloque Presentación (Día 4)

- [ ] **T-DEMO-001** — Slides de presentación
  - Diapositiva 1: problema (red eléctrica + por qué tiempo real)
  - Diapositiva 2: arquitectura (diagrama)
  - Diapositiva 3: Redis como bus (Pub/Sub + Streams + Hashes)
  - Diapositiva 4: Publisher (XM real + simulador + fallback)
  - Diapositiva 5: Métricas y alertas
  - Diapositiva 6: Dashboard (capturas + casos de uso)
  - Diapositiva 7: Demo en vivo (link al sistema)
  - Diapositiva 8: Conclusiones + futuras mejoras

- [ ] **T-DEMO-002** — Preparar guion de la demo (5-7 min)
  - 1) Mostrar publisher corriendo (logs)
  - 2) Generar evento manual / forzar XM
  - 3) Mostrar evento en Redis (`make redis-cli` + KEYS)
  - 4) Mostrar el dashboard actualizándose sin recargar
  - 5) Forzar alerta con stress test
  - 6) Mostrar fallback (banner cambia a "SIMULADOR")
  - 7) Cerrar con Q&A preparado

---

## Cómo reportar progreso

**Para tildar una tarea**:
1. Abrí este archivo
2. Buscá tu sección
3. Cambiá `- [ ]` por `- [x]` en la tarea que completaste
4. Commit directo a `main` con mensaje `chore(roadmap): marcar T-XYZ-123 como hecho`

**Si encontrás un blocker**:
1. NO marques la tarea como completa
2. Reportá a Sebastián por el canal que definieron
3. Yo te oriento o derivo a quien corresponda

**Si una tarea cambió de alcance**:
1. Avisá antes de implementar
2. Actualizamos la descripción del checkbox
3. Si es grande, abrimos una nota al final del roadmap

---

## Si te trabás — escalamiento rápido

| Problema | A quién escalar | Cómo |
|----------|----------------|------|
| No entendés una tarea del roadmap | Sebastián | Mensaje directo |
| Decisión técnica con varias opciones | Sebastián | "Opción A o B, te paso justificación" |
| Mi código no levanta / tira error raro | Sebastián | Pegame el stacktrace |
| Algo que ya estaba hecho se rompió | Sebastián (líder técnico) | `git log` para ver qué cambió |
| Conflicto entre dos personas | Sebastián | Mediación y decisión |
| Algo que necesita cambio en otra área | Sebastián | Coordino la sincronización |

> Cada integrante es dueño de su componente. Sebastián no escribe código de los demás — solo orienta, desbloquea y conecta las piezas al final.

---

## Checklist de cierre de proyecto (Sebastián, Día 4)

Antes de entregar, validar:

- [ ] `make up` levanta todo sin errores
- [ ] `make test` pasa todos los tests
- [ ] `make lint` y `make type-check` limpios
- [ ] Dashboard accesible en `localhost:5173`
- [ ] SSE stream conectado y actualizando en tiempo real
- [ ] Al menos 1 alerta probada en vivo
- [ ] Fallback XM→simulador probado
- [ ] `docs/README_TECNICO.md` completo
- [ ] `docs/DEPLOY.md` con instrucciones claras
- [ ] Slides de presentación listas
- [ ] Plan B de demo remota documentado
- [ ] Todos los integrantes pueden explicar la arquitectura completa

---

**Owner del roadmap**: Sebastián · **Próxima actualización**: al cierre del Día 2