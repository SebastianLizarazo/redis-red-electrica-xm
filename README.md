# Monitor de Red Eléctrica Colombiana en Tiempo Real con Redis

Sistema de monitoreo en tiempo real de la red eléctrica colombiana (operador XM) que captura, procesa, almacena y visualiza datos de demanda, generación y composición energética.

## Stack

- **Backend**: Python 3.11+ con FastAPI
- **Cache/Pub-Sub/Streams**: Redis (capa gratuita)
- **API externa**: XM Colombia (pydataxm + endpoint `DemandaTiempoReal`)
- **Visualización**: Chart.js + Leaflet (pendiente definir)
- **Frontend**: HTML/CSS/JavaScript vanilla o framework ligero (pendiente definir)

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

## Estructura del proyecto (pendiente de detalle en SDD)

```
.
├── publisher/         # Captura de API XM + simulador
├── subscriber/        # Consumer + métricas + alertas
├── dashboard/         # Frontend web
├── docs/              # Documentación técnica
├── docker-compose.yml # Redis + servicios
└── README.md
```

## Equipo

- Edwar
- Alejandro
- Sebastián (Líder)
- David
- Jonathan

## Trabajo académico

Actividad integradora de la materia **Analítica de Datos** — Sistemas de datos en tiempo real con Redis.