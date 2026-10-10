#!/usr/bin/env python3
"""
API HTTP del monitor para clientes propios (app móvil).

Corre en el mismo bucle de asyncio que el bot y trabaja sobre su estado. El contrato
está en API.md. No pone TLS: se publica detrás de un proxy inverso que lo ponga y que
además exija certificado de cliente.
"""

import asyncio
import hmac
import json
from datetime import datetime

from aiohttp import web


API_VERSION = "1"
PULSO_SEGUNDOS = 15
ESCANEO_MINIMO_SEGUNDOS = 10  # POST /api/escanear no llama a Iberdrola más a menudo
INTERVALO_ESCANEO_MINIMO = 30


class PeticionInvalida(Exception):
    """Cuerpo o parámetros incorrectos. El middleware la convierte en un 400."""


def _error(status, codigo, mensaje):
    return web.json_response({'error': codigo, 'mensaje': mensaje}, status=status)


class ServidorAPI:
    def __init__(self, monitor, token, puerto=8090, host='0.0.0.0'):
        self.monitor = monitor
        self.token = token.encode()
        self.puerto = puerto
        self.host = host
        self.runner = None
        self.app = web.Application(middlewares=[self._errores, self._autenticar])
        self._rutas()

    def _rutas(self):
        r = self.app.router
        r.add_get('/api/salud', self.salud)
        r.add_get('/api/estado', self.estado)
        r.add_post('/api/escanear', self.escanear)
        r.add_get('/api/caza', self.caza)
        r.add_post('/api/caza', self.caza_empezar)
        r.add_put('/api/caza', self.caza_intervalo)
        r.add_delete('/api/caza', self.caza_cancelar)
        r.add_post('/api/caza/pausar', self.caza_pausar)
        r.add_post('/api/caza/reanudar', self.caza_reanudar)
        r.add_get('/api/reserva', self.reserva)
        r.add_post('/api/reserva', self.reserva_crear)
        r.add_delete('/api/reserva', self.reserva_cancelar)
        r.add_delete('/api/reserva/renovacion', self.renovacion_parar)
        r.add_get('/api/historial', self.historial)
        r.add_get('/api/cambios', self.cambios)
        r.add_get('/api/ajustes', self.ajustes)
        r.add_put('/api/ajustes', self.ajustes_cambiar)
        r.add_get('/api/eventos', self.eventos)

    async def iniciar(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.puerto).start()
        print(f"🌐 API HTTP escuchando en {self.host}:{self.puerto}")

    async def parar(self):
        if self.runner:
            await self.runner.cleanup()

    # ---------- middlewares ----------

    @web.middleware
    async def _errores(self, request, handler):
        try:
            return await handler(request)
        except PeticionInvalida as e:
            return _error(400, 'peticion_invalida', str(e))
        except web.HTTPException as e:
            if e.status == 404:
                return _error(404, 'no_encontrado', 'Ruta desconocida')
            if e.status == 405:
                return _error(405, 'metodo_no_permitido', 'Método no permitido en esta ruta')
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"❌ Error en la API ({request.method} {request.path}): {e}")
            return _error(500, 'error_interno', 'Error interno del servidor')

    @web.middleware
    async def _autenticar(self, request, handler):
        cabecera = request.headers.get('Authorization', '')
        token = cabecera[7:].encode() if cabecera.startswith('Bearer ') else b''
        # Comparación en tiempo constante, para no filtrar el token por tiempos
        if not hmac.compare_digest(token, self.token):
            return _error(401, 'no_autorizado', 'Token ausente o incorrecto')
        return await handler(request)

    # ---------- utilidades ----------

    @staticmethod
    async def _json(request) -> dict:
        if not request.can_read_body:
            return {}
        try:
            datos = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise PeticionInvalida('El cuerpo tiene que ser JSON')
        if not isinstance(datos, dict):
            raise PeticionInvalida('El cuerpo tiene que ser un objeto JSON')
        return datos

    @staticmethod
    def _entero(valor, nombre) -> int:
        if isinstance(valor, bool) or not isinstance(valor, int):
            raise PeticionInvalida(f"'{nombre}' tiene que ser un entero")
        return valor

    # ---------- rutas ----------

    async def salud(self, request):
        return web.json_response({'ok': True, 'version': API_VERSION})

    async def estado(self, request):
        return web.json_response(self.monitor.estado_dict())

    async def escanear(self, request):
        m = self.monitor
        reciente = m.last_scan_at and (datetime.now() - m.last_scan_at).total_seconds() < ESCANEO_MINIMO_SEGUNDOS
        if not reciente:
            conectores = await m.chequear_cargadores(forzar=True)
            if conectores is None and not m.last_conectores:
                return _error(502, 'error_iberdrola', 'No se pudo consultar el estado de los cargadores')
        return web.json_response(m.estado_dict())

    async def caza(self, request):
        return web.json_response(self.monitor.caza_dict())

    async def caza_empezar(self, request):
        datos = await self._json(request)
        intervalo = datos.get('intervalo')
        if intervalo is not None:
            intervalo = self._entero(intervalo, 'intervalo')

        activa = await self.monitor.hay_reserva_activa()
        if activa is None:
            return _error(503, 'sin_sesion', 'No hay sesión de Iberdrola')
        if activa:
            return _error(409, 'reserva_activa', 'Ya tienes una reserva activa, no hace falta cazar.')

        await self.monitor.empezar_caza_api(intervalo)
        return web.json_response(self.monitor.caza_dict())

    async def caza_intervalo(self, request):
        datos = await self._json(request)
        intervalo = self._entero(datos.get('intervalo'), 'intervalo')
        if not self.monitor.hunt_task:
            return _error(409, 'caza_inactiva', 'No hay ninguna caza en marcha')
        self.monitor.cambiar_intervalo_caza(intervalo)
        return web.json_response(self.monitor.caza_dict())

    async def caza_pausar(self, request):
        if not self.monitor.hunt_task:
            return _error(409, 'caza_inactiva', 'No hay ninguna caza en marcha')
        await self.monitor.pausar_caza_api()
        return web.json_response(self.monitor.caza_dict())

    async def caza_reanudar(self, request):
        if not self.monitor.hunt_task:
            return _error(409, 'caza_inactiva', 'No hay ninguna caza en marcha')
        self.monitor.reanudar_caza()
        return web.json_response(self.monitor.caza_dict())

    async def caza_cancelar(self, request):
        if self.monitor.hunt_task:
            await self.monitor.cancelar_caza_api()
        return web.json_response(self.monitor.caza_dict())

    async def reserva(self, request):
        authenticated, _ = await self.monitor.ensure_authenticated()
        if not authenticated:
            return _error(503, 'sin_sesion', 'No hay sesión de Iberdrola')
        return web.json_response(await self.monitor.reserva_detalle())

    async def reserva_crear(self, request):
        datos = await self._json(request)
        cupr_id = self._entero(datos.get('cuprId'), 'cuprId')
        socket_id = self._entero(datos.get('physicalSocketId'), 'physicalSocketId')

        m = self.monitor
        if m.reserva_en_curso:
            return _error(409, 'reserva_en_curso', 'Ya se está procesando una reserva')
        activa = await m.hay_reserva_activa()
        if activa is None:
            return _error(503, 'sin_sesion', 'No hay sesión de Iberdrola')
        if activa:
            return _error(409, 'reserva_activa', 'Ya tienes una reserva activa.')

        # Se marca antes de lanzar la tarea, para que dos peticiones seguidas no
        # pasen las dos el control de arriba.
        m.reserva_en_curso = True
        asyncio.create_task(m.reservar_api(cupr_id, socket_id))
        return web.json_response({'aceptada': True}, status=202)

    async def reserva_cancelar(self, request):
        authenticated, _ = await self.monitor.ensure_authenticated()
        if not authenticated:
            return _error(503, 'sin_sesion', 'No hay sesión de Iberdrola')
        if not await self.monitor.cancelar_reserva_api():
            return _error(502, 'error_iberdrola', 'Iberdrola no confirmó la cancelación')
        return web.json_response({'ok': True})

    async def renovacion_parar(self, request):
        self.monitor.parar_auto_renovacion()
        return web.json_response({'ok': True})

    async def historial(self, request):
        try:
            limite = max(1, min(200, int(request.query.get('limite', 20))))
        except ValueError:
            raise PeticionInvalida("'limite' tiene que ser un entero")
        return web.json_response(self.monitor.historial_dict(limite))

    async def cambios(self, request):
        try:
            limite = max(1, min(5000, int(request.query.get('limite', 200))))
            desde = request.query.get('desde')
            desde = datetime.fromisoformat(desde) if desde else None
        except ValueError:
            raise PeticionInvalida("'limite' o 'desde' no son válidos")
        return web.json_response({'cambios': self.monitor.cambios_list(limite, desde)})

    async def ajustes(self, request):
        return web.json_response(self.monitor.ajustes_dict())

    async def ajustes_cambiar(self, request):
        datos = await self._json(request)
        intervalo = pausado = None
        # Se valida todo antes de aplicar nada, para no dejar un cambio a medias
        if 'intervalo_escaneo' in datos:
            intervalo = self._entero(datos['intervalo_escaneo'], 'intervalo_escaneo')
            if intervalo < INTERVALO_ESCANEO_MINIMO:
                raise PeticionInvalida(f"'intervalo_escaneo' no puede bajar de {INTERVALO_ESCANEO_MINIMO}")
        if 'escaneo_pausado' in datos:
            pausado = datos['escaneo_pausado']
            if not isinstance(pausado, bool):
                raise PeticionInvalida("'escaneo_pausado' tiene que ser true o false")

        m = self.monitor
        if intervalo is not None:
            m.cambiar_intervalo_escaneo(intervalo)
        if pausado is not None:
            m.cambiar_pausa_escaneo(pausado)
        return web.json_response(m.ajustes_dict())

    async def eventos(self, request):
        respuesta = web.StreamResponse(headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            # Para nginx: que no acumule el flujo antes de mandarlo
            'X-Accel-Buffering': 'no',
        })
        await respuesta.prepare(request)

        cola = self.monitor.suscribir()
        print(f"📡 Cliente de eventos conectado ({len(self.monitor._suscriptores)} en total)")
        try:
            await self._enviar(respuesta, 'estado', self.monitor.estado_dict())
            while True:
                try:
                    evento, datos = await asyncio.wait_for(cola.get(), timeout=PULSO_SEGUNDOS)
                except asyncio.TimeoutError:
                    evento, datos = 'pulso', {'ahora': self.monitor._iso(datetime.now())}
                await self._enviar(respuesta, evento, datos)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.monitor.desuscribir(cola)
            print(f"📡 Cliente de eventos desconectado ({len(self.monitor._suscriptores)} quedan)")
        return respuesta

    @staticmethod
    async def _enviar(respuesta, evento, datos):
        texto = json.dumps(datos, ensure_ascii=False)
        await respuesta.write(f"event: {evento}\ndata: {texto}\n\n".encode())
