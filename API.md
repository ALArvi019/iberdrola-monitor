# API HTTP

El monitor expone una API HTTP para clientes propios (por ejemplo una app móvil). Corre en
el mismo proceso que el bot, así que comparte su estado: escaneo, caza, reserva y
histórico. Telegram sigue funcionando igual.

Está desactivada salvo que se defina `API_TOKEN`. Escucha en `API_PORT` (8090 por defecto)
dentro del contenedor, sin TLS: está pensada para publicarse detrás de un proxy inverso que
ponga el TLS, y conviene que ese proxy exija además certificado de cliente (mTLS). Ver
[Publicación detrás de un proxy](#publicación-detrás-de-un-proxy).

## Autenticación

Todas las rutas bajo `/api/` exigen la cabecera:

```
Authorization: Bearer <API_TOKEN>
```

Sin ella, o con un token distinto, la respuesta es `401 {"error": "no_autorizado"}`.

## Errores

Siempre JSON con un código estable:

```json
{"error": "reserva_activa", "mensaje": "Ya tienes una reserva activa."}
```

| Código | HTTP | Cuándo |
|---|---|---|
| `no_autorizado` | 401 | Falta el token o no coincide |
| `peticion_invalida` | 400 | Cuerpo o parámetros incorrectos |
| `no_encontrado` | 404 | Ruta desconocida |
| `reserva_activa` | 409 | Ya hay una reserva, no se puede cazar ni reservar |
| `reserva_en_curso` | 409 | Ya se está procesando una reserva (pago 3DS pendiente) |
| `caza_inactiva` | 409 | Se pide pausar, reanudar o cambiar una caza que no existe |
| `sin_sesion` | 503 | No hay sesión de Iberdrola (falta usuario o el login falló) |
| `error_iberdrola` | 502 | La API de Iberdrola no respondió |

## Fechas

ISO 8601 en hora local del servidor, sin zona (`2026-10-10T11:02:31`). El contenedor
corre con `TZ=Europe/Madrid`.

## Rutas

### `GET /api/salud`

```json
{"ok": true, "version": "1"}
```

### `GET /api/estado`

Estado completo, sin llamar a Iberdrola: devuelve lo del último escaneo.

```json
{
  "ahora": "2026-10-10T11:02:31",
  "escaneo": {
    "pausado": false,
    "intervalo": 30,
    "jitter": 5.0,
    "ultimo": "2026-10-10T11:02:01",
    "proximo": "2026-10-10T11:02:33"
  },
  "tipo_preferido": "Mennekes",
  "conectores": [
    {
      "cuprId": 1234,
      "cuprName": "Parking Centro 01",
      "physicalSocketId": 5678,
      "socketCode": "1",
      "socketType": "Mennekes",
      "maxPower": 22,
      "status": "RESERVED",
      "reserva": {
        "inicio": "2026-10-10T10:56:11",
        "fiable": true,
        "marcas": [
          {"minutos": 13, "hora": "2026-10-10T11:09:11", "cumplida": false},
          {"minutos": 30, "hora": "2026-10-10T11:26:11", "cumplida": false}
        ]
      }
    }
  ],
  "caza": { "...": "igual que GET /api/caza" },
  "mi_reserva": {
    "auto_renovacion": true,
    "cuprId": 1234,
    "physicalSocketId": 5678,
    "proxima_renovacion": "2026-10-10T11:16:00",
    "en_curso": false
  }
}
```

* `tipo_preferido`: tipo de conector que se prefiere al reservar (la caza lo elige
  primero). `null` si no hay preferencia.
* `status`: `AVAILABLE`, `OCCUPIED`, `EV_CONNECTED`, `RESERVED`, `OUT_OF_SERVICE` o `UNKNOWN`.
* `reserva` solo aparece en conectores `RESERVED`. `inicio` es `null` si el bot ya lo
  encontró reservado y no sabe desde cuándo. `fiable` es `false` si el bot estuvo parado
  entre medias y la hora es aproximada.
* `mi_reserva` sale del estado local del bot, sin consultar a Iberdrola. `en_curso` es
  `true` mientras se procesa una reserva (pago pendiente). Para los detalles de la reserva
  real, `GET /api/reserva`.

### `POST /api/escanear`

Fuerza un escaneo y devuelve el estado como `GET /api/estado`. Si el último escaneo tiene
menos de 10 segundos, devuelve ese sin llamar a Iberdrola.

### `GET /api/caza`

```json
{
  "estado": "activa",
  "intervalo": 60,
  "intervalo_min": 30,
  "opciones": [30, 45, 60, 120, 300],
  "escaneos": 12,
  "proximo": "2026-10-10T11:03:30",
  "hasta": "2026-10-10T14:00:00",
  "fallos": 0
}
```

`estado`: `inactiva`, `activa` o `pausada`.

### `POST /api/caza`

Empieza una caza. Cuerpo `{"intervalo": 60}` (opcional, segundos, no baja de
`intervalo_min`). Devuelve la caza. `409 reserva_activa` si ya hay una reserva.

### `PUT /api/caza`

Cambia el intervalo de la caza en marcha: `{"intervalo": 120}`. `409 caza_inactiva` si no hay.

### `POST /api/caza/pausar` · `POST /api/caza/reanudar`

Devuelven la caza. `409 caza_inactiva` si no hay.

### `DELETE /api/caza`

Cancela la caza. Devuelve la caza (ya `inactiva`).

### `GET /api/reserva`

Consulta a Iberdrola la reserva propia.

```json
{"activa": false}
```

```json
{
  "activa": true,
  "nombre": "Parking Centro 01",
  "cuprId": 1234,
  "physicalSocketId": 5678,
  "socketType": "Mennekes",
  "inicio": "2026-10-10T11:00:00",
  "fin": "2026-10-10T11:30:00",
  "precio": 1.0,
  "coste_cancelacion": 0.0,
  "estado": "Reservado",
  "auto_renovacion": true,
  "proxima_renovacion": "2026-10-10T11:14:00"
}
```

### `POST /api/reserva`

Reserva un conector: `{"cuprId": 1234, "physicalSocketId": 5678}`.

El pago pide aprobar el 3DS en la app del banco y puede tardar hasta dos minutos, así que
la respuesta es inmediata (`202 {"aceptada": true}`) y el resultado llega por
`/api/eventos` como eventos `reserva` (`progreso`, `ok`, `error`). Al salir bien se activa
la renovación automática y se cancela cualquier caza.

`409 reserva_activa` o `409 reserva_en_curso` si no procede.

### `DELETE /api/reserva`

Cancela la reserva propia y para la renovación automática. `{"ok": true}`.

### `DELETE /api/reserva/renovacion`

Para la renovación automática sin cancelar la reserva. `{"ok": true}`.

### `GET /api/historial?limite=20`

Reservas ajenas vistas, de la más reciente a la más antigua.

```json
{
  "reservas": [
    {
      "physicalSocketId": 5678,
      "cuprName": "Parking Centro 01",
      "socketCode": "1",
      "socketType": "Mennekes",
      "inicio": "2026-10-10T10:02:00",
      "fiable": true,
      "fin": "2026-10-10T10:31:00",
      "fin_estado": "AVAILABLE",
      "minutos": 29
    }
  ],
  "resumen": {"total": 8, "media_minutos": 21.4, "usadas": 3, "liberadas": 5}
}
```

`fin` y `minutos` son `null` en una reserva en curso. `usadas` cuenta las que acabaron en
`OCCUPIED` o `EV_CONNECTED` (alguien llegó a cargar) y `liberadas` las que acabaron en
`AVAILABLE`.

### `GET /api/cambios?limite=200&desde=2026-10-01T00:00:00`

Cambios de estado en bruto, para gráficas (por ejemplo, a qué horas se libera un cargador).
`limite` va de 1 a 5000 (200 por defecto). `desde` es opcional.

```json
{
  "cambios": [
    {
      "physicalSocketId": 5678,
      "cuprName": "Parking Centro 01",
      "socketCode": "1",
      "socketType": "Mennekes",
      "anterior": "OCCUPIED",
      "nuevo": "AVAILABLE",
      "detectado": "2026-10-10T10:31:00"
    }
  ]
}
```

### `GET /api/ajustes` · `PUT /api/ajustes`

```json
{
  "tipo_preferido": "Mennekes",
  "intervalo_escaneo": 30,
  "escaneo_pausado": false,
  "marcas_reserva": [13, 30],
  "renovacion_minutos": 14
}
```

`PUT` acepta `intervalo_escaneo` (mínimo 30), `escaneo_pausado` y `tipo_preferido` (texto o
`null` para quitar la preferencia, y manda sobre `PREFERRED_CONNECTOR_TYPE`). Devuelve los
ajustes.

### `GET /api/eventos`

Flujo SSE (`text/event-stream`). Cada evento lleva `event:` y `data:` en JSON.

| Evento | Cuándo | `data` |
|---|---|---|
| `pulso` | Cada 15 s | `{"ahora": "..."}` |
| `estado` | Al conectar y tras cada escaneo o cambio de caza o reserva | Igual que `GET /api/estado` |
| `cambio` | Un conector cambia de estado | `{"cambios": [{"physicalSocketId", "cuprName", "socketCode", "socketType", "anterior", "nuevo"}]}` |
| `caza` | Pasa algo en la caza | `{"tipo", "mensaje", ...}` |
| `reserva` | Pasa algo con la reserva propia | `{"tipo", "mensaje", ...}` |

Tipos de `caza`: `iniciada`, `pausada`, `reanudada`, `libre` (conector libre, se va a
pagar: hay que aprobar el 3DS), `reservado`, `fallo`, `terminada` (tiempo máximo),
`cancelada`, `retomada` (tras un reinicio).

Tipos de `reserva`: `progreso`, `ok`, `error`, `renovada`, `renovacion_fallida`,
`expirada`, `carga_iniciada`, `cancelada`.

`mensaje` es un texto en español listo para mostrar en una notificación. Los eventos `caza`
y `reserva` pueden llevar `cuprId`, `physicalSocketId`, `cuprName` y `socketType`.

Los eventos `cambio`, `caza` y `reserva` llevan `id:`. Al reconectar, el cliente manda el
último que recibió en la cabecera `Last-Event-ID` y el servidor le reenvía primero los que
se perdió (guarda los 200 últimos en memoria), y después el `estado`. `estado` y `pulso` no
llevan id. Tras un reinicio del servidor se reenvían todos los que tenga, que serán pocos o
ninguno.

Si en 25 segundos no llega nada (ni siquiera `pulso`), la conexión está muerta y hay que
reconectar.

## Publicación detrás de un proxy

El contenedor no publica el puerto de la API. Para que el proxy llegue a él, lo normal es
unirlo a la red Docker del proxy con un `docker-compose.override.yml` junto al compose (no
está en el repo porque depende de cada instalación):

```yaml
services:
  iberdrola-monitor:
    networks: [default, proxy]
networks:
  proxy:
    external: true
    name: nombre-de-la-red-del-proxy
```

Y en el proxy, para que el SSE funcione:

```nginx
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 1h;
proxy_set_header Connection "";
proxy_http_version 1.1;
```

Para exigir certificado de cliente (mTLS) en nginx:

```nginx
ssl_client_certificate /ruta/a/ca.crt;
ssl_verify_client on;
```
