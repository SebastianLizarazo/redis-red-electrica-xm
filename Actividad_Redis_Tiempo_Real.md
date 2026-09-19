# Actividad integradora: Sistemas de datos en tiempo real con Redis

**Curso:** Tendencias Modernas de Bases de Datos

---

## 1. Descripción de la actividad

En esta actividad se desarrollará un sistema de procesamiento y visualización de datos en tiempo real utilizando Redis como base de datos en memoria y plataforma de comunicación orientada a eventos.

El grupo de 4 estudiantes deberán diseñar una solución capaz de recibir continuamente información proveniente de múltiples fuentes, procesarla, almacenarla temporalmente en Redis y visualizarla mediante una aplicación web.

A diferencia de un sistema tradicional de almacenamiento de datos, el objetivo es experimentar con un escenario en el que los datos cambian constantemente y las aplicaciones necesitan conocer rápidamente el estado actual de las entidades monitoreadas.

El contexto a desarrollar será:

- Red eléctrica inteligente: Monitoreo de demanda, generación y composición energética

Para la solución se deberá implementar una arquitectura basada en Redis.

---

## 2. Objetivo general

Diseñar e implementar un sistema distribuido de monitoreo en tiempo real que permita capturar, procesar, almacenar temporalmente y visualizar información dinámica utilizando Redis y tecnologías web.

---

## 3. Objetivos específicos

Al finalizar la actividad, los estudiantes estará en capacidad de:

- Implementar Redis como base de datos en memoria.
- Utilizar mecanismos de comunicación orientados a eventos.
- Trabajar con Redis Pub/Sub y/o Redis Streams.
- Seleccionar estructuras de datos de Redis de acuerdo con las necesidades del problema.
- Consumir información proveniente de APIs públicas cuando estén disponibles.
- Diseñar simuladores de datos cuando no exista una fuente pública adecuada.
- Procesar información recibida en tiempo real.
- Construir un dashboard web para visualizar información dinámica.
- Generar métricas y alertas a partir de los datos recibidos.
- Explicar las ventajas y limitaciones de utilizar una base de datos en memoria.
- Diferenciar entre el estado actual de una entidad y su histórico reciente.
- Trabajar colaborativamente en el diseño e implementación de un sistema distribuido.

---

## 4. Organización del equipo de trabajo

El grupo de 4 estudiantes deberá distribuir internamente las responsabilidades, pero todos los integrantes deberán conocer y poder explicar la arquitectura completa.

---

## 5. Arquitectura general

El proyecto deberá implementar conceptualmente la siguiente arquitectura:

```
              FUENTE DE DATOS
     API pública / simulador / aplicación
                    │
                    ▼
        ┌───────────────────────┐
        │       PUBLISHER       │
        │                       │
        │  Captura              │
        │  Normaliza            │
        │  Publica              │
        └───────────┬───────────┘
                    │
                    ▼
        ┌───────────────────────┐
        │         REDIS         │
        │                       │
        │  Pub/Sub              │
        │  Streams              │
        │  Hashes               │
        │  Lists/Sets           │
        │  Sorted Sets          │
        │  TTL                  │
        └───────────┬───────────┘
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
   PROCESADOR              SUBSCRIBER
        └───────────┬───────────┘
                    ▼
            APLICACIÓN WEB
                    │
                    ▼
               DASHBOARD
```

No es obligatorio implementar todos los componentes como aplicaciones independientes, pero sí debe existir claramente la separación entre:

- fuente de datos;
- productor o Publisher;
- Redis;
- consumidor o Subscriber/Processor;
- aplicación web;
- visualización.

---

## 6. Fuente de datos

El grupo deberá utilizar una fuente de información que genere datos continuamente.

Se pueden utilizar dos mecanismos:

- **Modo real:** utilizar una API, servicio o aplicación que proporcione datos reales o telemetría. *(Esta opción concede más puntos a la valoración final de la actividad)*
- **Modo simulado:** desarrollar un programa que genere datos artificiales pero realistas.

La fuente de datos deberá producir nuevos eventos periódicamente. Por ejemplo:

- Cada 5 segundos
- Cada 10 segundos
- Cada 15 segundos

El intervalo deberá justificarse de acuerdo con el contexto.

No se exige la utilización de una API pública. Cuando no exista una fuente pública adecuada, el grupo deberá implementar un simulador.

---

## 7. Requisito de modo simulado

Todos los proyectos deberán contar con un modo de simulación, independientemente de que utilicen una API real.

Esto permitirá realizar la demostración incluso cuando una API externa no esté disponible.

La arquitectura deberá estar diseñada de manera que el resto del sistema no dependa directamente de la fuente de datos. Por ejemplo:

```
   ┌──────────────────────────┐
   │                          │
 API REAL               SIMULADOR
   │                          │
   └────────────┬─────────────┘
                ▼
            PUBLISHER
                ▼
              REDIS
                ▼
            DASHBOARD
```

El Publisher deberá transformar ambas fuentes a un formato de eventos consistente.

---

## 8. Uso obligatorio de Redis (Utilizar la capa gratuita de Redis)

Redis deberá desempeñar un papel relevante dentro de la solución.

No será suficiente utilizar Redis únicamente como una base de datos donde se almacenan algunos registros.

El proyecto deberá utilizar Redis para resolver al menos las siguientes necesidades:

- comunicación de eventos;
- almacenamiento rápido del estado actual;
- almacenamiento de información temporal o histórica reciente;
- generación o consulta de al menos una métrica derivada.

El grupo deberá utilizar como mínimo **dos estructuras o mecanismos diferentes de Redis**.

Se recomienda utilizar:

- Pub/Sub;
- Streams;
- Hashes;
- Lists;
- Sets;
- Sorted Sets;
- Strings;
- TTL.

Durante la presentación, el grupo deberá justificar por qué seleccionó cada estructura.

---

## 9. Redis Pub/Sub

Todos los grupos deberán implementar comunicación mediante Redis Pub/Sub. Por ejemplo:

- canal: `traffic-events`
- canal: `energy-events`
- canal: `application-events`
- canal: `parking-events`

El Publisher publicará eventos y uno o más Subscribers deberán recibirlos.

Ejemplo conceptual:

```
Publisher
    │
    │ PUBLISH traffic-events
    ▼
  Redis
    │
    ├──────────► Subscriber
    │
    └──────────► Processor
```

---

## 10. Redis Streams

Se recomienda que los grupos utilicen Redis Streams para representar eventos que deban conservarse durante un período de tiempo. Por ejemplo:

- `traffic:stream`
- `energy:stream`
- `application:stream`
- `parking:stream`

El uso de Streams permitirá comparar conceptualmente dos mecanismos:

- **Pub/Sub:** comunicación inmediata de eventos.
- **Streams:** eventos que pueden permanecer almacenados y ser procesados posteriormente.

El grupo que utilice Streams deberá explicar esta diferencia durante la presentación.

---

## 11. Estado actual e histórico

El sistema deberá diferenciar entre:

- **Estado actual:** información más reciente de cada entidad.
- **Histórico reciente:** conjunto de eventos o mediciones ocurridos durante un período determinado.

Por ejemplo, `sensor:traffic:001` puede representar el estado actual de un sensor, mientras que `traffic:stream` puede contener sus eventos recientes.

El histórico no necesita ser permanente. Se recomienda utilizar mecanismos de expiración o limitar la cantidad de información almacenada.

---

## 12. Formato general de los eventos

El grupo deberá utilizar eventos estructurados en JSON.

Cada evento deberá contener como mínimo:

```json
{
  "entity_id": "ID",
  "timestamp": "2026-09-19T10:30:00Z",
  "location": {
    "latitude": 5.7,
    "longitude": -72.9
  },
  "data": {}
}
```

Los campos específicos dentro de `data` dependerán del proyecto. Por ejemplo:

```json
{
  "entity_id": "TRAFFIC-01",
  "timestamp": "2026-09-19T10:30:00Z",
  "location": {
    "latitude": 5.72,
    "longitude": -72.93
  },
  "data": {
    "vehicles_per_minute": 52,
    "average_speed": 31.5,
    "occupancy": 68
  }
}
```

---

## 13. Procesamiento de los datos

El sistema deberá realizar algún procesamiento sobre los datos recibidos. No será suficiente con almacenar y mostrar directamente los valores.

El proyecto deberá calcular al menos **dos métricas derivadas**. Ejemplos:

- promedios;
- porcentajes;
- tasas;
- diferencias;
- variaciones;
- acumulados;
- rankings;
- niveles de ocupación;
- indicadores de estado.

También deberá implementar al menos **una regla de alerta**. Ejemplo:

```
SI ocupación > 90%
ENTONCES generar alerta de saturación
```

---

## 14. Dashboard web

El grupo deberá desarrollar una aplicación web que muestre la información procesada.

El dashboard deberá actualizarse automáticamente sin necesidad de recargar la página.

Se recomienda utilizar tecnologías como:

- HTML;
- CSS;
- JavaScript;
- Node.js;
- Chart.js;
- D3.js;
- Leaflet;
- otras tecnologías equivalentes.

El uso de un framework frontend es opcional.

---

## 15. Elementos mínimos del dashboard

TEl proyecto deberá incluir:

- indicadores numéricos o KPIs;
- al menos dos gráficas temporales;
- información del estado actual;
- visualización de eventos o alertas;
- actualización en tiempo real.

Cuando el contexto lo permita, deberá incluirse también una visualización geográfica.

---



## 16. Red eléctrica inteligente

El grupo desarrollará un sistema de monitoreo de variables relacionadas con generación y demanda eléctrica. El sistema puede representar diferentes zonas o regiones de una red eléctrica.

### Variables sugeridas

- demanda eléctrica;
- generación total;
- generación solar;
- generación eólica;
- generación hidráulica;
- otras fuentes de generación;
- importación;
- exportación;
- porcentaje de generación renovable;
- intensidad de carbono.

### Fuente de datos

Una alternativa para este proyecto es **Electricity Maps**, que ofrece datos relacionados con generación, consumo, flujos eléctricos, intensidad de carbono y composición energética. El acceso a determinados datos y endpoints depende del tipo de cuenta y de las condiciones de uso de su API.

Si la API no está disponible para el grupo, deberá utilizarse el modo simulado.

### Métricas sugeridas

- demanda promedio;
- generación total;
- porcentaje renovable;
- diferencia entre demanda y generación;
- fuente de generación predominante;
- variación de la demanda;
- intensidad de carbono.

### Alertas sugeridas

- demanda elevada;
- reducción significativa de generación;
- baja participación de energías renovables;
- diferencia significativa entre generación y demanda.

### Visualización

El dashboard puede incluir:

- demanda actual;
- generación actual;
- composición de generación;
- demanda vs. generación;
- evolución temporal;
- porcentaje renovable;
- intensidad de carbono;
- comparación entre zonas.

---

## 17. Simuladores

Si el grupo llega a utilizar simulador deberá evitar generar números completamente aleatorios sin relación entre sí. Los datos deberán presentar un comportamiento razonablemente realista.

Por ejemplo, si un estacionamiento tiene `capacidad = 100` y `ocupados = 95`, el simulador no debería generar inmediatamente `ocupados = 130`. Deberán respetarse las restricciones propias del contexto.

También se recomienda introducir eventos especiales para probar el sistema:

- congestión;
- saturación;
- aumento de demanda;
- disminución de demanda;
- caída de un servicio;
- aumento de errores;
- cambios bruscos;
- recuperación de una condición anormal.

---

## 18. Requisito de tiempo real

El sistema deberá demostrar que un nuevo evento generado por el Publisher produce un cambio observable en el dashboard sin necesidad de recargar manualmente la página.

La demostración deberá mostrar al menos:

```
Fuente
   ↓
Publisher
   ↓
Redis
   ↓
Subscriber/Processor
   ↓
Dashboard
```

El grupo deberá explicar cuánto tiempo transcurre aproximadamente entre la generación de un evento y su visualización.

---

## 19. Persistencia y datos históricos

Redis se utilizará principalmente como mecanismo de almacenamiento en memoria y procesamiento rápido. No es necesario implementar una base de datos relacional tradicional.

Sin embargo, cada grupo deberá definir qué información:

- debe permanecer únicamente como estado actual;
- debe mantenerse como histórico reciente;
- podría requerir almacenamiento permanente en un sistema externo.

El grupo deberá justificar esta decisión.

---

## 20. Expiración de información

Se recomienda utilizar TTL o mecanismos equivalentes para evitar almacenar indefinidamente información que solamente sea necesaria durante un período corto. Por ejemplo:

- Estado actual → mantener mientras exista la entidad
- Eventos recientes → conservar últimos N eventos
- Datos temporales → expirar después de X minutos

El grupo deberá explicar cómo controla el crecimiento de la información almacenada en Redis.

---

## 21. Tecnologías

Redis (su capa gratuita) es obligatorio. Las demás tecnologías pueden ser seleccionadas por cada grupo. Se recomienda:

**Backend**

- Node.js;
- Python;
- Java;
- otra tecnología equivalente.

**Frontend**

- HTML;
- CSS;
- JavaScript;
- React;
- Vue;
- Angular;
- otra tecnología equivalente.

**Visualización**

- Chart.js;
- D3.js;
- Leaflet;
- u otra biblioteca equivalente.

---

## 22. Requisitos mínimos de implementación

El proyecto deberá cumplir como mínimo:

- Redis funcionando correctamente.
- Publisher funcionando.
- Subscriber o Processor funcionando.
- Comunicación mediante Redis Pub/Sub.
- Uso de al menos dos estructuras o mecanismos de Redis.
- Generación continua de datos.
- Procesamiento de datos.
- Al menos dos métricas derivadas.
- Al menos una alerta.
- Dashboard web.
- Actualización en tiempo real.
- Al menos dos gráficas.
- Estado actual de las entidades.
- Histórico reciente.
- Modo de simulación.
- Documentación de la arquitectura.

---

## 23. Requisitos adicionales para obtener una implementación avanzada

Los grupos que quieran ampliar el proyecto pueden implementar:

- Redis Streams;
- Consumer Groups;
- Sorted Sets;
- TTL;
- múltiples Subscribers;
- procesamiento de ventanas temporales;
- detección de anomalías;
- ranking de entidades;
- autenticación;
- Docker;
- despliegue en la nube;
- persistencia complementaria;
- múltiples instancias de Publisher;
- tolerancia a desconexiones;
- reconexión automática;
- registro de logs;
- métricas del propio sistema.

Estas características son opcionales y no reemplazan los requisitos mínimos.

---

## 24. Preguntas que el grupo debe poder responder

Durante la presentación, todos los integrantes deberán poder responder preguntas como:

1. ¿Por qué utilizar Redis en este proyecto?
2. ¿Qué información se mantiene en memoria?
3. ¿Qué información representa el estado actual?
4. ¿Qué información representa el histórico?
5. ¿Por qué seleccionaron las estructuras de Redis utilizadas?
6. ¿Cuál es la función del Publisher?
7. ¿Cuál es la función del Subscriber?
8. ¿Qué información se transmite mediante Pub/Sub?
9. ¿Qué sucedería si el Subscriber se desconecta?
10. ¿Qué sucede con los datos cuando Redis se reinicia?
11. ¿Cómo controlan el crecimiento de los datos?
12. ¿Cómo generan los eventos?
13. ¿Qué métricas se calculan?
14. ¿Cómo se generan las alertas?
15. ¿Qué parte del sistema funciona en tiempo real?
16. ¿Qué ventajas tiene utilizar una base de datos en memoria?
17. ¿Qué limitaciones tiene Redis frente a una base de datos tradicional?
18. ¿Qué ocurriría si el número de sensores o eventos aumentara significativamente?
19. ¿Cómo modificarían la arquitectura para soportar múltiples Publishers y Subscribers?
20. ¿Qué componentes podrían escalar horizontalmente?

---

## 25. Entregables

El grupo deberá entregar:

### 25.1 Código fuente

Repositorio Git que contenga:

- Publisher;
- Subscriber/Processor;
- configuración de Redis;
- aplicación web;
- simulador, cuando corresponda;
- archivos de configuración;
- instrucciones de ejecución.

### 25.2 Documento técnico

El documento deberá contener:

- descripción del problema;
- arquitectura;
- tecnologías utilizadas;
- fuente de datos;
- estructura de los eventos;
- estructuras Redis utilizadas;
- canales Pub/Sub;
- Streams, si fueron utilizados;
- explicación del procesamiento;
- métricas;
- reglas de alerta;
- diseño del dashboard;
- instrucciones de instalación;
- instrucciones de ejecución;
- dificultades encontradas;
- conclusiones.

### 25.3 Dashboard funcional

La aplicación deberá estar disponible para ser demostrada.

### 25.4 Presentación

El grupo realizará una demostración del sistema funcionando.

---

## 26. Demostración

La presentación deberá incluir una demostración en vivo. Se recomienda seguir el siguiente flujo:

1. Mostrar el Publisher
2. Generar un nuevo evento
3. Mostrar el evento en Redis
4. Mostrar el procesamiento
5. Mostrar la actualización del dashboard
6. Generar una condición de alerta
7. Mostrar cómo cambia el dashboard
8. Consultar el estado actual
9. Consultar el histórico reciente

No se recomienda realizar una presentación basada únicamente en capturas de pantalla.

---

## 27. Criterios de evaluación

| Criterio | Porcentaje |
|----------|-----------|
| Arquitectura y diseño de la solución | 15% |
| Uso correcto de Redis | 20% |
| Publisher y generación/captura de datos | 10% |
| Procesamiento y generación de métricas | 15% |
| Comunicación en tiempo real | 10% |
| Dashboard y visualización | 15% |
| Alertas y comportamiento dinámico | 5% |
| Documentación | 5% |
| Presentación y dominio técnico | 5% |
| **Total** | **100%** |

---

## 28. Consideraciones sobre las APIs

El uso de una API pública no es obligatorio. La API debe considerarse un mecanismo para obtener datos, no el objetivo principal de la actividad.

Si una API:

- requiere una clave;
- tiene límites de uso;
- deja de estar disponible;
- no ofrece suficientes datos;
- no proporciona información con la frecuencia requerida;

el grupo deberá utilizar el simulador. La solución deberá continuar funcionando correctamente en modo simulado.

La disponibilidad, límites y condiciones de las APIs deberán verificarse al momento de desarrollar el proyecto.
