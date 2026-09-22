# DEPLOY — Guía de despliegue y demo

> Documento vivo. Cubre los dos planes que el equipo valida antes del
> **demo del martes 22-sept-2026**: local (`Plan A`) y remoto vía
> `cloudflared` (`Plan B`). Cada plan es independiente: si uno falla,
> el otro es fallback.

## 1. Plan A — Demo local

El camino por defecto. Asume que el profesor ve el demo en una máquina
del equipo con todo instalado.

### Prerrequisitos

- Python 3.11 o superior.
- Node.js 18 o superior **solo si quieres hot-reload en el dashboard**. En ese
  caso necesitas pnpm: pruébalo con `corepack enable` (corepack viene con
  Node 16.9+, aunque algunas distribuciones lo empaquetan aparte y el comando
  puede no existir). Si no lo tienes, o si prefieres no instalar Node,
  **`make up-dev` levanta el dashboard dentro de Docker** y no hace falta
  nada de esto.

> Todos los comandos `make` se ejecutan **desde la raíz del repositorio**, no
> desde `dashboard/`. Si ves `make: *** No rule to make target`, estás en la
> carpeta equivocada: `cd` a la raíz y repite.
- Docker + docker compose (solo para levantar Redis).

### Opción 1 — todo en Docker (recomendada para la demo)

Dos comandos y dos terminales:

```bash
FORCE_SOURCE=simulator PUBLISHER_INTERVAL_SECONDS=3 make up
make dev-dashboard           # dashboard con hot-reload
```

> **Por qué las variables.** Sin ellas el publisher arranca contra XM real,
> que publica cada **5 minutos**: el dashboard parece congelado y da la
> impresión de estar roto. Con el simulador los datos se mueven cada 3 s.
> Para la demo con datos reales, simplemente `make up`.

Si no quieres instalar Node, un solo comando levanta también el dashboard:

```bash
FORCE_SOURCE=simulator PUBLISHER_INTERVAL_SECONDS=3 make up-dev
```

**Si el puerto 6379 ya está ocupado** (tienes un Redis instalado en tu
máquina), no hace falta apagarlo:

```bash
REDIS_PORT=6380 make up
```

Comprobar que el backend quedó arriba antes de abrir el navegador:

```bash
curl localhost:8000/api/health
make ps                      # los 4 contenedores en "healthy"
```

Si el dashboard también se quiere dentro de Docker:

```bash
make up-dev                  # añade el servicio dashboard-dev
```

### Opción 2 — procesos a mano (útil para depurar)

Deja Redis en Docker y corre el resto en terminales separadas, así se ven
los logs de cada componente por separado:

1. **Redis** (terminal 1):

   ```bash
   make up-redis
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

5. **Dashboard Vite** (terminal 5):

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

Subir `dashboard/dist/` a Netlify Drop, GitHub Pages, o cualquier CDN
estático.

#### GitHub Pages automático

El workflow `.github/workflows/pages.yml` publica `dashboard/dist/` en
Pages con cada push a `main` que toque `dashboard/`. Antes del primer run
hay que ir a **Settings → Pages → Source = GitHub Actions**.

Dos detalles que hacen la diferencia entre una página que funciona y una
que carga en blanco:

- El build usa `base: '/redis-red-electrica-xm/'` porque Pages sirve el
  sitio bajo la ruta del repositorio. Ya está en `vite.config.js`.
- **Pages solo aloja archivos estáticos**: no puede correr la API ni
  Redis. Sin un backend accesible desde internet, el dashboard carga pero
  se queda en «SIN CONEXIÓN». Hay que apuntarlo al tunnel de `cloudflared`
  definiendo la variable de repositorio `DASHBOARD_API_URL` (Settings →
  Secrets and variables → Actions → Variables), o lanzando el workflow a
  mano desde la pestaña Actions con el campo `api_url`.
- Ese dominio también tiene que estar en `CORS_ORIGINS` de la API, o el
  navegador bloqueará las respuestas aunque lleguen con 200.

### CORS

`common/config.py:CORS_ORIGINS` ya permite
`https://sebastianlizarazo.github.io/redis-red-electric-xm`. Si el
dominio del tunnel o del hosting del frontend es distinto, agregarlo
vía variable de entorno `CORS_ORIGINS=https://red.tudominio.com` en el
`.env` antes de levantar la API.

## 3. Troubleshooting común

- **Puerto 6379 ocupado** (`address already in use` al hacer `make up`): ya
  tienes un Redis corriendo. No hace falta apagarlo, basta con publicar el
  del contenedor en otro puerto del host: `REDIS_PORT=6380 make up`. Los
  servicios se siguen hablando por el 6379 dentro de la red de Docker, así
  que no cambia nada más. Lo mismo aplica a `API_PORT` y `DASHBOARD_PORT`.
- **`make: *** No rule to make target 'up'`**: estás dentro de una subcarpeta
  (normalmente `dashboard/`). Los `make` van desde la raíz del repo.
- **`pnpm: command not found` o `corepack: command not found`**: no necesitas
  ninguno de los dos. Usa `make up-dev`, que corre el dashboard en Docker.
  Si aun así quieres hot-reload en tu máquina, tienes dos salidas:
  `corepack enable` (si tu instalación de Node lo incluye) o
  `make dev-dashboard PNPM="npx pnpm@11"`, que descarga pnpm al vuelo.
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