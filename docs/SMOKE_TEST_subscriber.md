# Smoke test end-to-end del subscriber

Esta guía verifica que el subscriber procesa eventos del publisher y emite
alertas correctamente. Requisito: Redis en `localhost:6379` y Python 3.11+ en `.venv`.

## Setup

```powershell
# Terminal 1: Redis
docker compose up -d redis
# O: redis-server (si lo tenés instalado localmente)

# Terminal 2: .venv + dependencias
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## 1. Levantar el publisher (modo simulador, intervalo 5s)

```powershell
# Terminal 3: publisher con XM deshabilitado (forzar sim)
$env:XM_FORCE_SOURCE="simulator"
$env:PUBLISHER_INTERVAL_SECONDS="5"
python -m publisher.main
```

## 2. Levantar el subscriber

```powershell
# Terminal 4: subscriber
python -m subscriber.main
```

Logs esperados en el subscriber:

```
subscriber: subscribed to energy-events
subscriber booted at 2026-09-20T...
```

## 3. Inspeccionar Redis

```powershell
redis-cli KEYS "*"
# Esperado: state:zone:*, state:sin, metrics:renewable, metrics:balance,
#           metrics:demand:variation, metrics:demand:history, alerts:recent,
#           alerts:total, health:*, alerts:active:*

redis-cli HGETALL metrics:renewable
# value=60 unit="% timestamp=2026-09-20T... zone_id=ANT fuente=simulator

redis-cli ZRANGE metrics:demand:history 0 -1 WITHSCORES
# demanda_mw=1100 score=<unix_ts>

redis-cli GET health:subscriber:uptime
# <unix_ts>  -- escrito al boot del subscriber

redis-cli GET health:subscriber:started_at
# 2026-09-20T...  -- escrito al boot del subscriber
```

## 4. Forzar alerta con stress event

```powershell
# Terminal 5: API de stress (cuando esté en Día 3)
curl -X POST http://localhost:8000/api/stress/demand_surge
```

O directamente vía Redis:

```powershell
redis-cli SET stress:demand_surge 1 EX 30
# El publisher (si lo lee) inyecta el evento anómalo.
# O esperar al simulador: el publisher genera `demand_surge` automáticamente
# cada N minutos.
```

Cuando el evento dispara una alerta (gap > 800 MW por 2 ciclos consecutivos):

```powershell
redis-cli XLEN alerts:stream
# 1

redis-cli GET alerts:total
# 1

redis-cli GET alerts:active:DEMAND_GENERATION_GAP
# 1
```

Esperado en el panel de logs del subscriber:

```
housekeep: removed N old history entries
# o no aparece nada (si no pasaron 60s todavía)
```

## 5. Apagar limpio

```powershell
# Terminal 4: Ctrl+C (SIGINT) — el subscriber cierra la pubsub + conexión Redis
# Log esperado:
# INFO subscriber: shutting down...
# INFO subscriber: redis connection closed cleanly
```

`subscriber.main` debe salir con código 0 (verificable con `echo $LASTEXITCODE`
en PowerShell).

## Checklist

- [ ] Subscriber arranca sin errores
- [ ] `metrics:renewable` se actualiza cada 5s
- [ ] `metrics:demand:history` crece hasta 60 min y luego se prunea
- [ ] `alerts:total` incrementa cuando se dispara A1 o A2
- [ ] `alerts:active:DEMAND_GENERATION_GAP` se incrementa/decrementa
- [ ] `health:subscriber:uptime` está seteado al boot
- [ ] `health:subscriber:started_at` está seteado al boot
- [ ] Ctrl+C termina con exit code 0