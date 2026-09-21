# DEPLOY — Guía de despliegue y demo

> Documento vivo. Cubre los tres planes que el equipo valida antes del
> **demo del martes 22-sept-2026**: local (`Plan A`), remoto vía
> `cloudflared` (`Plan B`) y video de respaldo (`Plan C`). Cada plan es
> independiente: si uno falla, el siguiente es fallback.

## 1. Plan A — Demo local

El camino por defecto. Asume que el profesor ve el demo en una máquina
del equipo con todo instalado.

### Prerrequisitos

- Python 3.11 o superior.
- Node.js 18 o superior y pnpm 11+ (para el dashboard).
- Docker + docker compose (solo para levantar Redis).

### Pasos

1. **Levantar Redis** (en una terminal):

   ```bash
   make up
   ```

2. **Publisher** (terminal 2):

   ```bash
   python -m publisher.main
   ```

3. **Subscriber** (terminal 3):

   ```bash
   python -m subscriber.main
   ```

4. **API FastAPI** (terminal 4):

   ```bash
   python -m api.server
   ```

6. **Dashboard Vite** (terminal 5):

   ```bash
   cd dashboard && pnpm dev
   ```

### URLs

- API: <http://localhost:8000> (docs interactivas en `/docs`).
- Dashboard: <http://localhost:5173>.
- Redis: `localhost:6379` (sin auth por defecto).

### Inspección en vivo

```bash
make redis-cli
> KEYS '*'
> SUBSCRIBE energy-events
> XLEN energy:stream
> HGETALL state:zone:ANT
```

### Apagado limpio

```bash
make down        # detiene Redis (conserva volumen)
```

Y `Ctrl-C` en cada una de las 4 terminales (publisher, subscriber, API,
dashboard).

## 2. Plan B — Demo remota con `cloudflared`

Para que el profesor (o un jurado remoto) pueda ver la demo sin
instalar nada. Requiere un dominio bajo Cloudflare.

### Setup one-time (en la máquina que hace de host)

```bash
cloudflared tunnel login
cloudflared tunnel create red-electrica-demo
cloudflared tunnel route dns red-electrica-demo red.tudominio.com
```

### Run durante la demo

Levantar el stack completo (Plan A, pasos 1–5) y luego exponerlo:

```bash
cloudflared tunnel --url http://localhost:8000 run red-electrica-demo
# Salida tipo: https://red-electrica-demo.trycloudflare.com
```

### Frontend

Para el dashboard remoto, **build estático**:

```bash
cd dashboard && pnpm build        # genera dashboard/dist/
```

Subir `dashboard/dist/` a Netlify Drop, GitHub Pages (`gh-pages`
branch), o cualquier CDN estático.

### CORS

`common/config.py:CORS_ORIGINS` ya permite
`https://sebastianlizarazo.github.io/redis-red-electric-xm`. Si el
dominio del tunnel o del hosting del frontend es distinto, agregarlo
vía variable de entorno `CORS_ORIGINS=https://red.tudominio.com` en el
`.env` antes de levantar la API.

## 3. Plan C — Video de respaldo

Si Plan A y Plan B fallan (sin dominio Cloudflare, máquina del equipo
muerta, etc.), un screencast de 5 minutos es suficiente para mostrar el
sistema funcionando.

### Herramientas

- OBS Studio (desktop), Loom (browser), o `ffmpeg` con `x11grab` /
  `gdigrab` para captura silenciosa.

### Secuencia a grabar (~5 min)

1. Levantar stack completo (Plan A pasos 1–4).
2. Mostrar publisher corriendo (logs visibles, eventos publicando).
3. Forzar `demand_surge` desde el dashboard → alerta aparece en panel
   lateral.
4. Forzar 3 timeouts XM con `XM_TIMEOUT_SECONDS=0.001` en el `.env` →
   banner del dashboard cambia a **SIMULADOR** (caída + fallback).
5. Mostrar SSE actualizando KPIs en vivo sin recargar la página
   (`curl -N http://localhost:8000/api/stream` en otra terminal).

### Distribución

- Subir a YouTube como **unlisted** (no aparece en búsquedas pero el
  enlace funciona).
- Alternativa: Google Drive compartido solo con el profesor, permiso
  de lectura.

## 4. Troubleshooting común

- **Puerto 6379 ocupado**: en Linux/macOS `lsof -i :6379`; en Windows
  `netstat -ano | findstr :6379`. Matar el proceso o cambiar
  `REDIS_PORT` en `.env` y re-levantar.
- **Publisher no conecta**: ¿corriste `make up`? Verificar con
  `redis-cli ping` (debe responder `PONG`) y revisar `REDIS_URL` en
  `.env`.
- **SSE no actualiza en el dashboard**: abrir DevTools → Network y ver
  si el preflight CORS está fallando. Probar
  `curl http://localhost:8000/api/health` y verificar que la API está
  viva. Si hay error CORS, revisar `CORS_ORIGINS` en
  `common/config.py`.
- **Dashboard en blanco**: ¿`pnpm dev` está corriendo? Probar
  `curl http://localhost:5173/api/health` — el proxy de Vite debe
  reenviar al backend en `:8000`. Si el proxy no resuelve, revisar
  `dashboard/vite.config.js` (`server.proxy['/api']`).
- **Tests fallan con `Redis connection refused`**: ¿se importó algo de
  `api.*` antes de mockear `get_redis`? Verificar que el test use el
  fixture `app_client` (que monta `dependency_overrides` para
  `get_redis`); tests que importan routers directamente deben usar
  `fakeredis_async_client` antes de cualquier request.