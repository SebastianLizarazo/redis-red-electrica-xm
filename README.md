# Monitor de Red Eléctrica Colombiana en Tiempo Real con Redis

Sistema de monitoreo en tiempo real de la red eléctrica colombiana (operador XM) que captura, procesa, almacena y visualiza datos de demanda, generación y composición energética.

## Stack

- **Backend**: Python 3.11+ con FastAPI
- **Cache/Pub-Sub/Streams**: Redis (capa gratuita de Redis Cloud o local con Docker)
- **API externa**: XM Colombia — `DemandaTiempoReal` (sin auth) + librería `pydataxm` para SIMEM/SINERGOX
- **Visualización**: Chart.js + Leaflet
- **Frontend**: HTML/CSS/JavaScript vanilla

## Arquitectura

```
   API XM (real)            SIMULADOR (fallback)
         \                        /
          \                      /
           ───────► PUBLISHER ◄──
                    (Python)
                       │
                       ▼
                ┌─────────────┐
                │    REDIS    │
                │  Pub/Sub    │
                │  Streams    │
                │  Hashes     │
                │  TTL        │
                └──────┬──────┘
                       │
                       ▼
            SUBSCRIBER + PROCESSOR
              (Python / FastAPI)
                       │
                       ▼
                  DASHBOARD
              (HTML + Chart.js)
```

## Equipo y responsabilidades (5 integrantes)

| Persona      | Responsabilidad principal                                                | Componentes                                                                                 |
| ------------ | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| **Sebastián** | Líder técnico · Integración · Documentación · Presentación              | Une las piezas, escribe el documento técnico (entregable 25.2), arma la presentación, define contratos entre módulos |
| **Edwar**     | **Publisher** (API XM + simulador de respaldo)                           | Cliente HTTP a `DemandaTiempoReal`, generador de datos sintéticos realistas (patrones circadianos, eventos anómalos), normalización a JSON de evento |
| **Alejandro** | **Subscriber/Processor** (métricas + alertas + Redis)                    | Consumer del canal `energy-events`, cálculo de 2+ métricas (promedio móvil, % renovable, delta demanda/generación), reglas de alerta, escritura a Hashes y Stream |
| **David**     | **Dashboard frontend** (HTML + Chart.js + Leaflet + websockets)         | Web que se actualiza en tiempo real: KPIs, 2+ gráficas, vista geográfica, panel de alertas  |
| **Jonathan**  | **Infraestructura + Testing + Docker**                                   | `docker-compose.yml` con Redis, scripts de arranque, pruebas, control de TTL/Streams, logs  |

> Cada integrante debe poder explicar la arquitectura completa, no solo su parte. En la última semana se hace un repaso cruzado entre todos.

## Entregables del taller (mapeo a entregables 25.x)

- **25.1 Código fuente** → todo el repo
- **25.2 Documento técnico** → Sebastián (David y Alejandro aportan sus secciones)
- **25.3 Dashboard funcional** → David (deploy)
- **25.4 Presentación** → Sebastián (con aportes de todos)

## Estructura tentativa

```
.
├── publisher/
│   ├── xm_client.py       # cliente API real
│   ├── simulator.py       # generador sintético
│   └── publisher.py       # captura + normaliza + PUBLISH
├── subscriber/
│   ├── metrics.py         # cálculo de métricas derivadas
│   ├── alerts.py          # reglas de alerta
│   └── processor.py       # SUBSCRIBE + escribe a Redis
├── dashboard/
│   ├── index.html
│   ├── app.js
│   └── charts.js
├── infra/
│   ├── docker-compose.yml
│   └── redis.conf
├── tests/
├── docs/
│   └── README_TECNICO.md
└── README.md
```

## Trabajo académico

Actividad integradora de la materia **Analítica de Datos** — Sistemas de datos en tiempo real con Redis.