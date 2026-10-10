#!/usr/bin/env python3
"""
Bot de Telegram para monitorizar cargadores Iberdrola
Escanea periódicamente y notifica cambios de estado
"""

import os
import random
import signal
import sqlite3
from collections import deque
from datetime import datetime, timedelta

# Reap zombie child processes (Chromium headless_shell) automatically
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import asyncio

from iberdrola_api import IberdrolaAPI
from iberdrola_auth import IberdrolaAuth


# Estados que devuelve la API de Iberdrola. EV_CONNECTED es el coche enchufado pero sin
# cargar, y faltaba, asi que salia como interrogacion en las notificaciones.
STATUS_EMOJI = {
    'AVAILABLE': '✅',
    'OCCUPIED': '🔴',
    'EV_CONNECTED': '🔌',
    'RESERVED': '🟡',
    'OUT_OF_SERVICE': '⚠️',
    'UNKNOWN': '❓',
}


class MonitorCargadores:
    def __init__(self):
        # Variables de entorno
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.device_id = os.getenv('DEVICE_ID', 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx')
        self.latitude = float(os.getenv('LATITUDE', '40.4155'))
        self.longitude = float(os.getenv('LONGITUDE', '-3.7074'))
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '60'))

        # Jitter: desviación aleatoria sobre el intervalo para que los escaneos
        # no caigan siempre en el mismo segundo (evita un patrón regular obvio).
        # SCAN_JITTER_PCT: porcentaje del intervalo (0 = desactivado)
        # SCAN_JITTER_MIN: desviación mínima en segundos
        self.jitter_pct = float(os.getenv('SCAN_JITTER_PCT', '15')) / 100
        self.jitter_min_seconds = float(os.getenv('SCAN_JITTER_MIN', '5'))
        # Generador no determinista (no depende de la semilla global)
        self._rand = random.SystemRandom()

        # IDs de los cargadores (separados por coma en .env)
        charger_ids_str = os.getenv('CHARGER_IDS', '1234,5678')
        self.cupr_ids = [int(x.strip()) for x in charger_ids_str.split(',')]
        
        # Control de escaneo
        self._scan_lock = asyncio.Lock()
        self.scanning_paused = False
        self.next_scan_time = None  # Hora estimada del próximo escaneo
        self.last_scan_at = None  # Último escaneo con respuesta de la API
        self.last_conectores = None

        # Los favoritos cambian muy poco. Pedirlos en cada escaneo duplicaba las
        # peticiones a la API, asi que se guardan un rato.
        self.FAVORITES_TTL_SECONDS = 15 * 60
        self._fav_ids_cache = None
        self._fav_ids_at = None

        # Minutos de una reserva ajena en los que conviene mirar el cargador. A los
        # 13 min suele cancelarse quien renueva antes de que acabe la cancelación
        # gratuita, y a los 30 caduca la reserva.
        marks_str = os.getenv('RESERVATION_MARKS_MIN', '13,30')
        self.reservation_marks = [int(x) for x in marks_str.split(',') if x.strip()]

        # Caza: escanear hasta que quede un conector libre y reservarlo.
        # HUNT_MIN_INTERVAL es el suelo para no saturar la API, y HUNT_MAX_MINUTES
        # apaga la caza sola si nadie se acuerda de pararla.
        self.hunt_min_interval = int(os.getenv('HUNT_MIN_INTERVAL', '30'))
        self.hunt_max_minutes = int(os.getenv('HUNT_MAX_MINUTES', '180'))
        self.hunt_interval = max(self.hunt_min_interval, int(os.getenv('HUNT_INTERVAL', '60')))
        self.hunt_task = None
        self.hunt_paused = False
        self.hunt_until = None
        self.hunt_started = None
        self.hunt_next = None
        self.hunt_scans = 0
        self.hunt_failures = 0  # Intentos de reserva fallidos seguidos

        # Clientes conectados a /api/eventos, una cola por cliente
        self._suscriptores = set()
        # Avisos recientes (cambio, caza, reserva) para reenviarlos a quien reconecte
        # con Last-Event-ID. El id lleva el arranque del proceso delante, para que tras
        # un reinicio un id antiguo no se confunda con uno nuevo.
        self._arranque_id = format(int(datetime.now().timestamp()), 'x')
        self._contador_eventos = 0
        self._eventos_recientes = deque(maxlen=200)
        self.reserva_en_curso = False  # Pago de una reserva pedida por la API
        self.cancelacion_en_curso = False
        
        # API de Iberdrola (sin auth inicialmente)
        self.api = IberdrolaAPI(device_id=self.device_id)
        
        # Autenticación (se inicializa después de cargar tokens de DB)
        self.auth = None
        self.auth_enabled = bool(os.getenv('IBERDROLA_USER')) and bool(os.getenv('IBERDROLA_PASS'))
        
        # Base de datos SQLite (compatible con Docker y local)
        if os.path.exists('/app'):
            self.db_path = '/app/data/monitor.db'
        else:
            self.db_path = os.path.join(os.path.dirname(__file__), 'data', 'monitor.db')
        self.init_database()
        
        # Cargar tokens de autenticación si existen
        self._load_auth_from_db()
        
        # Auto-renovación de reservas
        self.auto_renew_active = False
        self.auto_renew_cupr_id = None
        self.auto_renew_socket_id = None
        self.auto_renew_task = None
        self.auto_renew_next_time = None  # Hora de próxima renovación
        self.RENEW_INTERVAL_MINUTES = 14  # Cancelar y renovar cada 14 minutos (antes de los 15 min gratis)

        # Versión de la app (cargar desde DB si disponible)
        self.app_version = os.environ.get('IBERDROLA_APP_VERSION', '4.36.7')
        self.waiting_for_version = False
        self._load_app_version()

        # Application de Telegram
        self.app = None
        
        print(f"✅ Monitor inicializado")
        print(f"📍 Ubicación: {self.latitude}, {self.longitude}")
        print(f"�� Monitorizando cargadores: {self.cupr_ids}")
        print(f"⏱️  Intervalo: {self.check_interval}s ({self.check_interval//60} minutos)")
    
    def get_main_keyboard(self):
        """Retorna el teclado principal persistente"""
        keyboard = [
            [KeyboardButton("🔌 Ver Estado"), KeyboardButton("🔄 Forzar Chequeo")],
            [KeyboardButton("📅 Reservar"), KeyboardButton("📋 Mi Reserva")],
            [KeyboardButton("🎯 Cazar Libre"), KeyboardButton("📜 Histórico")],
            [KeyboardButton("⏸️ Pausar/Reanudar"), KeyboardButton("⏱️ Cambiar Intervalo")],
            [KeyboardButton("⭐ Favoritos"), KeyboardButton("📱 Versión"), KeyboardButton("ℹ️ Info")]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    
    def init_database(self):
        """Inicializa la base de datos SQLite"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS estado_conectores (
                physicalSocketId INTEGER PRIMARY KEY,
                cuprId INTEGER,
                cuprName TEXT,
                cpId INTEGER,
                socketType TEXT,
                status TEXT,
                lastUpdate TEXT,
                lastCheck TEXT
            )
        ''')
        
        # Tabla para configuración
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS configuracion (
                clave TEXT PRIMARY KEY,
                valor TEXT
            )
        ''')
        
        # Tabla para tokens de autenticación
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS auth_tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT,
                refresh_token TEXT,
                id_token TEXT,
                token_expiry TEXT,
                updated_at TEXT
            )
        ''')
        
        # Histórico de cambios de estado de cada conector. La API no informa de
        # cuándo empezó un estado (updateDate llega vacío), así que la única forma
        # de saber cuándo se hizo una reserva ajena es anotarlo al detectarla.
        # visto_antes es el último escaneo en que el conector aún tenía el estado
        # anterior: el cambio ocurrió entre visto_antes y detectado.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS historial_estados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                physicalSocketId INTEGER,
                cuprId INTEGER,
                cuprName TEXT,
                socketCode TEXT,
                socketType TEXT,
                estado_anterior TEXT,
                estado_nuevo TEXT,
                visto_antes TEXT,
                detectado TEXT
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_historial_socket
            ON historial_estados (physicalSocketId, id)
        ''')
        
        conn.commit()
        conn.close()
        print("✅ Base de datos inicializada")
    
    def _load_auth_from_db(self):
        """Carga tokens de autenticación desde la base de datos"""
        if not self.auth_enabled:
            print("ℹ️ Autenticación no configurada (sin IBERDROLA_USER/PASS)")
            return
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT access_token, refresh_token, id_token, token_expiry FROM auth_tokens WHERE id = 1')
        row = cursor.fetchone()
        conn.close()
        
        # Crear instancia de IberdrolaAuth
        self.auth = IberdrolaAuth(tokens_file=None)  # No usar archivo
        
        if row and row[1]:  # Si hay refresh_token
            self.auth.access_token = row[0]
            self.auth.refresh_token = row[1]
            self.auth.id_token = row[2]
            if row[3]:
                self.auth.token_expiry = datetime.fromisoformat(row[3])
            
            # Actualizar API con auth manager y callback de auth failure
            self.api = IberdrolaAPI(
                device_id=self.device_id, 
                auth_manager=self.auth,
                on_auth_failure=self._on_auth_failure
            )
            print("✅ Tokens de autenticación cargados desde DB")
        else:
            print("ℹ️ No hay tokens guardados, se requerirá login")
    
    def _save_auth_to_db(self):
        """Guarda tokens de autenticación en la base de datos"""
        if not self.auth:
            return
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        expiry_str = self.auth.token_expiry.isoformat() if self.auth.token_expiry else None
        
        cursor.execute('''
            INSERT OR REPLACE INTO auth_tokens (id, access_token, refresh_token, id_token, token_expiry, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
        ''', (
            self.auth.access_token,
            self.auth.refresh_token,
            self.auth.id_token,
            expiry_str,
            datetime.now().isoformat()
        ))
        
        conn.commit()
        conn.close()
        print("💾 Tokens guardados en DB")
    
    async def ensure_authenticated(self):
        """Asegura que tenemos una sesión autenticada válida"""
        if not self.auth_enabled:
            return False, "Autenticación no configurada. Añade IBERDROLA_USER e IBERDROLA_PASS al .env"
        
        if not self.auth:
            self._load_auth_from_db()
        
        # Verificar si el token es válido
        if self.auth.is_token_valid():
            return True, None
        
        # Intentar renovar con refresh_token
        if self.auth.refresh_token:
            print("🔄 Renovando token...")
            if self.auth.refresh_access_token():
                self._save_auth_to_db()
                return True, None
        
        # Necesita login completo con MFA (ejecutar en thread para Playwright)
        print("🔐 Iniciando login con MFA...")
        username = os.getenv("IBERDROLA_USER")
        password = os.getenv("IBERDROLA_PASS")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: self.auth.start_login(username, password)
        )

        if not result:
            return False, "Error iniciando login"

        if result.get("status") == "mfa_required":
            # Intentar leer código del email automáticamente
            otp = None
            if os.getenv("IMAP_USER") and os.getenv("IMAP_PASS"):
                try:
                    from email_mfa_reader import get_mfa_code_from_email
                    print("📧 Leyendo código MFA del email...")
                    otp = await loop.run_in_executor(
                        None, lambda: get_mfa_code_from_email(max_wait_seconds=90)
                    )
                except Exception as e:
                    print(f"⚠️ Error leyendo email: {e}")

            if not otp:
                return False, "No se pudo obtener el código MFA automáticamente. Configura IMAP_USER e IMAP_PASS."

            result = await loop.run_in_executor(
                None, lambda: self.auth.submit_mfa_code(result["mfa_state"], otp)
            )
        
        if result and result.get("status") == "success":
            self._save_auth_to_db()
            # Actualizar API con auth manager y callback de auth failure
            self.api = IberdrolaAPI(
                device_id=self.device_id, 
                auth_manager=self.auth,
                on_auth_failure=self._on_auth_failure
            )
            return True, None
        
        return False, "Error en el proceso de autenticación"
    
    def _on_auth_failure(self):
        """
        Callback que se ejecuta cuando la API detecta un error de autenticación.
        Invalida los tokens para forzar un re-login en la próxima petición.
        """
        print("🔒 Sesión inválida detectada. Se requiere re-autenticación.")
        if self.auth:
            self.auth.access_token = None
            self.auth.token_expiry = None
            # Mantenemos refresh_token por si aún sirve
    
    def guardar_estado(self, conectores, notificado=True):
        """Guarda el estado actual en la base de datos.

        Con notificado=False se anota que el escaneo ocurrio, pero se conserva el
        `status` anterior, que es contra el que se comparan los cambios. Asi un cambio
        que no se ha podido avisar se vuelve a detectar en el siguiente escaneo en vez
        de darse por visto y desaparecer.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        anteriores = {} if notificado else self.obtener_estado_anterior()

        for con in conectores:
            socket_id = con['physicalSocketId']
            estado = anteriores.get(socket_id, con['status']) if not notificado else con['status']
            cursor.execute('''
                INSERT OR REPLACE INTO estado_conectores 
                (physicalSocketId, cuprId, cuprName, cpId, socketType, status, lastUpdate, lastCheck)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                socket_id,
                con['cuprId'],
                con['cuprName'],
                con['cpId'],
                con['socketType'],
                estado,
                con['statusUpdateDate'],
                datetime.now().isoformat()
            ))
        
        conn.commit()
        conn.close()
    
    def obtener_estado_anterior(self):
        """Obtiene el estado anterior de la base de datos"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT physicalSocketId, status FROM estado_conectores')
        estados = {row[0]: row[1] for row in cursor.fetchall()}
        
        conn.close()
        return estados
    
    def get_config(self, clave, default=None):
        """Obtiene un valor de configuración"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT valor FROM configuracion WHERE clave = ?', (clave,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else default
    
    def set_config(self, clave, valor):
        """Guarda un valor de configuración"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO configuracion (clave, valor) VALUES (?, ?)', 
                      (clave, str(valor)))
        conn.commit()
        conn.close()
    
    def detectar_cambios(self, conectores_nuevos):
        """Detecta cambios en el estado de los conectores"""
        estados_anteriores = self.obtener_estado_anterior()
        cambios = []
        
        for con in conectores_nuevos:
            socket_id = con['physicalSocketId']
            estado_nuevo = con['status']
            estado_anterior = estados_anteriores.get(socket_id)
            
            if estado_anterior and estado_anterior != estado_nuevo:
                cambios.append({
                    'conector': con,
                    'estado_anterior': estado_anterior,
                    'estado_nuevo': estado_nuevo
                })
        
        return cambios

    def registrar_historial(self, conectores):
        """Anota en historial_estados cada conector cuyo estado ha cambiado.

        Lleva su propia cuenta, independiente de la de avisos: compara contra el
        último estado que tiene el histórico, no contra estado_conectores, que se
        queda atrás a propósito cuando un aviso no sale. Así un cambio se anota una
        sola vez aunque el aviso se reintente varios escaneos.
        """
        ahora = datetime.now().isoformat()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        previos = {
            row[0]: (row[1], row[2])
            for row in cursor.execute('SELECT physicalSocketId, status, lastCheck FROM estado_conectores')
        }

        for con in conectores:
            socket_id = con['physicalSocketId']
            nuevo = con['status']
            row = cursor.execute(
                'SELECT estado_nuevo FROM historial_estados WHERE physicalSocketId = ? ORDER BY id DESC LIMIT 1',
                (socket_id,)
            ).fetchone()
            estado_prev, ultimo_check = previos.get(socket_id, (None, None))
            conocido = row[0] if row else estado_prev

            if row and conocido == nuevo:
                continue

            # Primera vez que el histórico ve este conector y no ha cambiado: se anota
            # como punto de partida, sin estado anterior, porque no se sabe desde cuándo
            # está así.
            anterior = None if (row is None and conocido == nuevo) else conocido

            cursor.execute('''
                INSERT INTO historial_estados
                (physicalSocketId, cuprId, cuprName, socketCode, socketType,
                 estado_anterior, estado_nuevo, visto_antes, detectado)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                socket_id,
                con.get('cuprId'),
                con.get('cuprName'),
                str(con.get('socketCode', '')),
                con.get('socketType'),
                anterior,
                nuevo,
                ultimo_check if anterior else None,
                ahora,
            ))

            if nuevo == 'RESERVED':
                # La API no da la hora de la reserva, al menos con lo visto hasta
                # ahora. Se deja el bloque crudo en el log por si trae algo útil.
                print(f"🟡 Reserva detectada en socket {socket_id}. Datos de la API: {con.get('raw')}")

        conn.commit()
        conn.close()

    @staticmethod
    def _parse_fecha(valor):
        """ISO de la API o de la DB a datetime local sin zona. None si no se puede."""
        if not valor:
            return None
        try:
            dt = datetime.fromisoformat(str(valor).replace('Z', '+00:00'))
        except ValueError:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt

    def _inicio_estimado(self, visto_antes, detectado):
        """Momento estimado de un cambio y si la estimación es fiable.

        El cambio ocurrió entre visto_antes y detectado. Se toma el extremo más
        temprano, porque para saber cuándo quedará libre un cargador es mejor
        llegar antes que tarde. Si entre ambos hay mucho hueco (el bot estuvo
        parado) la hora no es fiable.
        """
        det = self._parse_fecha(detectado)
        antes = self._parse_fecha(visto_antes)
        if antes and det and det - antes <= timedelta(minutes=10):
            return antes, True
        return det, False

    def reservas_en_curso(self, conectores):
        """Para cada conector en RESERVED, cuándo empezó la reserva.

        Devuelve {physicalSocketId: (inicio, fiable)}. inicio es None si el bot ya
        lo encontró reservado y no sabe desde cuándo.
        """
        reservados = [c for c in conectores if c.get('status') == 'RESERVED']
        if not reservados:
            return {}

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        resultado = {}
        for con in reservados:
            socket_id = con['physicalSocketId']

            # Si la API llega a dar la hora del estado, manda sobre la estimación
            api_fecha = self._parse_fecha(con.get('statusUpdateDate'))
            if api_fecha:
                resultado[socket_id] = (api_fecha, True)
                continue

            row = cursor.execute('''
                SELECT estado_anterior, visto_antes, detectado FROM historial_estados
                WHERE physicalSocketId = ? ORDER BY id DESC LIMIT 1
            ''', (socket_id,)).fetchone()
            if row and row[0]:
                resultado[socket_id] = self._inicio_estimado(row[1], row[2])
            else:
                resultado[socket_id] = (None, False)
        conn.close()
        return resultado

    def formatear_marcas_reserva(self, inicio, fiable):
        """Línea con la hora de inicio de una reserva ajena y sus marcas (13 y 30 min)."""
        if inicio is None:
            return "⏱️ Ya estaba reservado al arrancar el bot, hora de inicio desconocida"
        aprox = "" if fiable else "≈"
        transcurrido = int((datetime.now() - inicio).total_seconds() // 60)
        partes = [f"⏱️ Desde {aprox}{inicio.strftime('%H:%M')} ({transcurrido} min)"]
        for marca in self.reservation_marks:
            hora = inicio + timedelta(minutes=marca)
            check = " ✓" if datetime.now() >= hora else ""
            partes.append(f"{marca}′ {hora.strftime('%H:%M')}{check}")
        return " · ".join(partes)

    def historial_reservas(self, limite=10):
        """Reservas ajenas vistas, de la más reciente a la más antigua.

        Cada una con su inicio estimado, su fin (None si sigue en curso) y el estado
        en el que acabó, que dice si alguien llegó a cargar o se liberó sin usarse.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        filas = cursor.execute('''
            SELECT r.physicalSocketId, r.cuprName, r.socketCode, r.socketType,
                   r.visto_antes, r.detectado,
                   (SELECT n.estado_nuevo FROM historial_estados n
                    WHERE n.physicalSocketId = r.physicalSocketId AND n.id > r.id
                    ORDER BY n.id LIMIT 1),
                   (SELECT n.detectado FROM historial_estados n
                    WHERE n.physicalSocketId = r.physicalSocketId AND n.id > r.id
                    ORDER BY n.id LIMIT 1)
            FROM historial_estados r
            WHERE r.estado_nuevo = 'RESERVED' AND r.estado_anterior IS NOT NULL
            ORDER BY r.id DESC LIMIT ?
        ''', (limite,)).fetchall()
        conn.close()

        reservas = []
        for socket_id, nombre, code, tipo, visto_antes, detectado, fin_estado, fin_det in filas:
            inicio, fiable = self._inicio_estimado(visto_antes, detectado)
            fin = self._parse_fecha(fin_det)
            reservas.append({
                'socket_id': socket_id,
                'cuprName': nombre,
                'socketCode': code,
                'socketType': tipo,
                'inicio': inicio,
                'fiable': fiable,
                'fin': fin,
                'fin_estado': fin_estado,
                'minutos': int((fin - inicio).total_seconds() // 60) if fin and inicio else None,
            })
        return reservas

    def formatear_mensaje_estado(self, conectores):
        """Formatea el mensaje con el estado de los conectores, agrupados por cargador"""
        status_emoji = STATUS_EMOJI

        mensaje = f"🔌 *ESTADO DE CARGADORES*\n"
        mensaje += f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"

        # Agrupar conectores por cargador (cuprName)
        cargadores = {}
        for con in conectores:
            nombre = con.get('cuprName', 'Sin nombre')
            cargadores.setdefault(nombre, []).append(con)

        reservas = self.reservas_en_curso(conectores)

        estados_count = {}
        for nombre, sockets in cargadores.items():
            mensaje += f"📍 *{nombre}*\n"
            for con in sockets:
                status = con.get('status', 'UNKNOWN')
                emoji = status_emoji.get(status, '❓')
                socket_code = con.get('socketCode', '?')
                socket_type = con.get('socketType', '')
                mensaje += f"  {emoji} Socket {socket_code} ({socket_type}) — `{status}`\n"
                if con.get('physicalSocketId') in reservas:
                    inicio, fiable = reservas[con['physicalSocketId']]
                    mensaje += f"      {self.formatear_marcas_reserva(inicio, fiable)}\n"
                estados_count[status] = estados_count.get(status, 0) + 1
            mensaje += "\n"

        # Resumen
        mensaje += "📊 *RESUMEN:* "
        resumen_parts = []
        for estado, count in estados_count.items():
            emoji = status_emoji.get(estado, '❓')
            resumen_parts.append(f"{emoji}{count}")
        mensaje += " ".join(resumen_parts)

        return mensaje
    
    def formatear_mensaje_cambio(self, cambios, todos_conectores):
        """Formatea el mensaje de notificación de cambios incluyendo el estado de todos"""
        status_emoji = STATUS_EMOJI
        
        mensaje = "🔔 *¡CAMBIO DE ESTADO DETECTADO!*\n\n"
        mensaje += f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        
        # Mostrar cambios
        for cambio in cambios:
            con = cambio['conector']
            ant = cambio['estado_anterior']
            nue = cambio['estado_nuevo']
            
            emoji_ant = status_emoji.get(ant, '❓')
            emoji_nue = status_emoji.get(nue, '❓')
            
            # Formatear nombre del cargador
            cupr_name = con.get('cuprName', '')
            socket_code = con.get('socketCode', '')
            cupr_num = cupr_name.split()[-1] if cupr_name else '???'
            display_name = f"{cupr_num}-{socket_code}"
            
            # Los estados van entre acentos graves a proposito. Varios llevan guion
            # bajo (EV_CONNECTED, OUT_OF_SERVICE) y en el Markdown de Telegram un guion
            # bajo suelto abre una cursiva que nunca se cierra: el mensaje entero se
            # rechaza con "can't find end of the entity" y el aviso no llega.
            mensaje += f"🏪 *{con['cuprName']}*\n"
            mensaje += f"🔌 Socket {display_name} ({con['socketType']})\n"
            mensaje += f"{emoji_ant} `{ant}` → {emoji_nue} `{nue}`\n\n"
        
        # Mostrar estado de todos los cargadores
        mensaje += "─" * 30 + "\n"
        mensaje += "*ESTADO ACTUAL DE TODOS:*\n\n"
        mensaje += self.formatear_mensaje_estado(todos_conectores)
        
        return mensaje
    
    async def enviar_mensaje(self, mensaje, parse_mode='Markdown'):
        """Envía un mensaje a Telegram. Devuelve True si llegó.

        Si falla el formato, se reintenta en texto plano. Un aviso feo llega; un aviso
        que no sale no sirve de nada, y el estado del cargador ya habrá cambiado otra
        vez cuando alguien se de cuenta.
        """
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=mensaje,
                parse_mode=parse_mode
            )
            print(f"✅ Mensaje enviado a Telegram")
            return True
        except Exception as e:
            print(f"❌ Error al enviar mensaje: {e}")

        if parse_mode is None:
            return False

        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=mensaje,
                parse_mode=None
            )
            print("✅ Mensaje enviado sin formato")
            return True
        except Exception as e:
            print(f"❌ Tampoco se pudo enviar sin formato: {e}")
            return False
    
    async def ids_a_vigilar(self):
        """cuprIds a escanear: los favoritos si hay sesión, si no CHARGER_IDS.

        Los favoritos se guardan FAVORITES_TTL_SECONDS para no pedirlos en cada
        escaneo, que con la caza puede ser cada 30 segundos.
        """
        ahora = datetime.now()
        if (self._fav_ids_cache and self._fav_ids_at
                and (ahora - self._fav_ids_at).total_seconds() < self.FAVORITES_TTL_SECONDS):
            return list(self._fav_ids_cache)

        authenticated, _ = await self.ensure_authenticated()
        if not authenticated:
            return list(self.cupr_ids)

        favoritos = await asyncio.to_thread(
            self.api.obtener_favoritos, lat=self.latitude, lon=self.longitude
        )
        fav_ids = []
        for fav in favoritos or []:
            cupr_id = fav.get('locationData', {}).get('cuprId')
            if cupr_id and cupr_id not in fav_ids:
                fav_ids.append(cupr_id)

        if not fav_ids:
            return list(self.cupr_ids)

        self._fav_ids_cache = fav_ids
        self._fav_ids_at = ahora
        # Pausa corta y variable entre peticiones del mismo ciclo
        await asyncio.sleep(self._rand.uniform(0.4, 2.5))
        return list(fav_ids)

    async def chequear_cargadores(self, forzar=False):
        """Chequea el estado de los cargadores y notifica cambios.

        Devuelve la lista de conectores, o None si no hubo escaneo. Con forzar=True
        se escanea aunque los escaneos automáticos estén en pausa (lo usa la caza).
        """
        if self.scanning_paused and not forzar:
            return None

        if self._scan_lock.locked():
            # Ya hay un chequeo en marcha, normalmente por el boton de forzar mientras
            # corria el periodico. Dos a la vez se pisarian el estado guardado.
            print("⏭️  Ya hay un chequeo en curso, se omite este")
            return None

        async with self._scan_lock:
            return await self._chequear_cargadores()

    async def _chequear_cargadores(self):
        try:
            cupr_ids = await self.ids_a_vigilar()

            # Obtener estado actual
            conectores = await asyncio.to_thread(
                self.api.obtener_estado_conectores,
                cupr_ids,
                self.latitude,
                self.longitude,
            )
            
            if not conectores:
                print("❌ No se pudieron obtener datos")
                return None

            self.last_scan_at = datetime.now()
            self.last_conectores = conectores

            # El histórico va antes de guardar_estado, que pisa el lastCheck que se
            # usa para acotar cuándo ocurrió el cambio.
            try:
                self.registrar_historial(conectores)
            except Exception as e:
                print(f"⚠️ Error guardando histórico: {e}")
            
            # Detectar cambios
            cambios = self.detectar_cambios(conectores)

            notificado = True
            if cambios:
                mensaje = self.formatear_mensaje_cambio(cambios, conectores)
                notificado = await self.enviar_mensaje(mensaje)
                self.publicar('cambio', {'cambios': [
                    {**self._datos_conector(c['conector']),
                     'anterior': c['estado_anterior'], 'nuevo': c['estado_nuevo']}
                    for c in cambios
                ]})

            # Guardar nuevo estado. Si el aviso no ha salido, el estado nuevo NO se
            # graba: si se grabara, el siguiente ciclo ya no veria ninguna diferencia y
            # ese cambio no se avisaria nunca. Dejandolo, se reintenta cada ciclo hasta
            # que Telegram lo acepte.
            self.guardar_estado(conectores, notificado=notificado)
            if not notificado:
                print("⚠️  Cambio sin avisar, se reintenta en el proximo escaneo")

            self.publicar_estado()
            return conectores
            
        except Exception as e:
            print(f"❌ Error al chequear: {e}")
            return None
    
    async def comando_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start"""
        mensaje = "🤖 *Bot Monitor Iberdrola*\n\n"
        mensaje += "Usa los botones del teclado para interactuar:\n\n"
        mensaje += "🔌 *Ver Estado* - Estado actual\n"
        mensaje += "🔄 *Forzar Chequeo* - Escanear ahora\n"
        mensaje += "📅 *Reservar* - Reservar cargador\n"
        mensaje += "📋 *Mi Reserva* - Ver/cancelar reserva\n"
        mensaje += "🎯 *Cazar Libre* - Reservar en cuanto quede uno libre\n"
        mensaje += "📜 *Histórico* - Reservas ajenas y cuándo se liberan\n"
        mensaje += "⏸️ *Pausar/Reanudar* - Control de escaneos\n"
        mensaje += "⏱️ *Cambiar Intervalo* - Ajustar frecuencia\n"
        mensaje += "⭐ *Favoritos* - Cargadores favoritos\n"
        mensaje += "📱 *Versión* - Cambiar versión de la app\n"
        mensaje += "ℹ️ *Info* - Información del sistema"
        
        await update.message.reply_text(
            mensaje, 
            parse_mode='Markdown', 
            reply_markup=self.get_main_keyboard()
        )
    
    async def manejar_texto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los mensajes de texto del teclado"""
        texto = update.message.text

        # Interceptar input de versión
        if self.waiting_for_version:
            self.waiting_for_version = False
            await self._set_new_version(update, texto)
            return

        if texto == "🔌 Ver Estado":
            await self.ver_estado(update, context)
        
        elif texto == "🔄 Forzar Chequeo":
            await self.forzar_chequeo(update, context)
        
        elif texto == "⏸️ Pausar/Reanudar":
            await self.toggle_pausa(update, context)
        
        elif texto == "⏱️ Cambiar Intervalo":
            await self.cambiar_intervalo(update, context)
        
        elif texto == "⭐ Favoritos":
            await self.ver_favoritos(update, context)
        
        elif texto == "📅 Reservar":
            await self.iniciar_reserva(update, context)
        
        elif texto == "📋 Mi Reserva":
            await self.ver_mi_reserva(update, context)

        elif texto == "🎯 Cazar Libre":
            await self.menu_caza(update, context)

        elif texto == "📜 Histórico":
            await self.ver_historial(update, context)
        
        elif texto == "ℹ️ Info":
            await self.mostrar_info(update, context)

        elif texto == "📱 Versión":
            await self.ver_version(update, context)

        else:
            await update.message.reply_text(
                "Comando no reconocido. Usa los botones del teclado.",
                reply_markup=self.get_main_keyboard()
            )
    
    async def ver_estado(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra el estado de los cargadores (favoritos si hay auth, sino CHARGER_IDS)"""
        await update.message.reply_text("⏳ Consultando estado...")

        cupr_ids = await self.ids_a_vigilar()

        conectores = await asyncio.to_thread(
            self.api.obtener_estado_conectores, cupr_ids, self.latitude, self.longitude
        )

        if conectores:
            mensaje = self.formatear_mensaje_estado(conectores)
            try:
                await update.message.reply_text(
                    mensaje,
                    parse_mode='Markdown',
                    reply_markup=self.get_main_keyboard()
                )
            except Exception as e:
                print(f"⚠️ Error Markdown, enviando sin formato: {e}")
                await update.message.reply_text(
                    mensaje.replace('*', '').replace('`', '').replace('_', ''),
                    reply_markup=self.get_main_keyboard()
                )
        else:
            await update.message.reply_text(
                "❌ Error al obtener datos",
                reply_markup=self.get_main_keyboard()
            )
    
    async def ver_favoritos(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra los cargadores favoritos del usuario"""
        await update.message.reply_text("⏳ Autenticando y obteniendo favoritos...")
        
        # Verificar autenticación
        authenticated, error = await self.ensure_authenticated()
        
        if not authenticated:
            await update.message.reply_text(
                f"❌ *Error de autenticación*\n\n{error}",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
            return
        
        # Obtener favoritos
        favoritos = self.api.obtener_favoritos(lat=self.latitude, lon=self.longitude)
        
        if favoritos is None:
            await update.message.reply_text(
                "❌ Error al obtener favoritos. Puede que la sesión haya expirado.",
                reply_markup=self.get_main_keyboard()
            )
            return
        
        if len(favoritos) == 0:
            await update.message.reply_text(
                "📋 No tienes cargadores favoritos guardados en la app.",
                reply_markup=self.get_main_keyboard()
            )
            return
        
        # Formatear mensaje con favoritos
        status_emoji = STATUS_EMOJI
        
        mensaje = "⭐ *TUS CARGADORES FAVORITOS*\n\n"
        
        for fav in favoritos:
            location = fav.get('locationData', {})
            nombre = location.get('cuprName', 'Sin nombre')
            alias = fav.get('alias', '')
            status_code = fav.get('cpStatus', {}).get('statusCode', 'UNKNOWN')
            emoji = status_emoji.get(status_code, '❓')
            
            # Dirección
            address = location.get('supplyPointData', {}).get('cpAddress', {})
            calle = address.get('streetName', '')
            ciudad = address.get('townName', '')
            
            mensaje += f"{emoji} *{nombre}*\n"
            if alias:
                mensaje += f"   📝 Alias: {alias}\n"
            mensaje += f"   📍 {calle}, {ciudad}\n"
            mensaje += f"   🔋 Estado: `{status_code}`\n\n"
        
        mensaje += f"_Total: {len(favoritos)} favoritos_"
        
        await update.message.reply_text(
            mensaje,
            parse_mode='Markdown',
            reply_markup=self.get_main_keyboard()
        )
    
    async def ver_historial(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reservas ajenas vistas: cuándo empezaron, cuánto duraron y cómo acabaron."""
        reservas = self.historial_reservas(limite=12)
        if not reservas:
            await update.message.reply_text(
                "📜 *Histórico de reservas*\n\nTodavía no he visto ninguna reserva en tus cargadores.\n"
                "Se van anotando a medida que el escaneo las detecta.",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
            return

        mensaje = "📜 *HISTÓRICO DE RESERVAS*\n"
        mensaje += "_Horas estimadas con el escaneo, margen de un intervalo._\n\n"

        for r in reservas:
            inicio = r['inicio']
            aprox = "" if r['fiable'] else "≈"
            dia = inicio.strftime('%d/%m') if inicio else "?"
            mensaje += f"🟡 {dia} *{r['cuprName']}* · socket {r['socketCode']} ({r['socketType']})\n"
            if r['fin'] is None:
                mensaje += f"    En curso. {self.formatear_marcas_reserva(inicio, r['fiable'])}\n"
            else:
                fin_estado = r['fin_estado'] or 'UNKNOWN'
                emoji = STATUS_EMOJI.get(fin_estado, '❓')
                mensaje += (f"    {aprox}{inicio.strftime('%H:%M')} → {r['fin'].strftime('%H:%M')}, "
                            f"{r['minutos']} min, acabó en {emoji} `{fin_estado}`\n")

        cerradas = [r for r in reservas if r['minutos'] is not None]
        if cerradas:
            media = sum(r['minutos'] for r in cerradas) / len(cerradas)
            usadas = sum(1 for r in cerradas if r['fin_estado'] in ('OCCUPIED', 'EV_CONNECTED'))
            liberadas = sum(1 for r in cerradas if r['fin_estado'] == 'AVAILABLE')
            mensaje += f"\n📊 Duración media: *{media:.0f} min* ({len(cerradas)} reservas)\n"
            mensaje += f"🔌 Llegaron a cargar: {usadas} · ✅ Se liberaron sin usar: {liberadas}"

        await self._responder(update, mensaje)

    async def _responder(self, update, mensaje):
        """Responde con Markdown y, si Telegram lo rechaza, en texto plano."""
        try:
            await update.message.reply_text(mensaje, parse_mode='Markdown', reply_markup=self.get_main_keyboard())
        except Exception as e:
            print(f"⚠️ Error Markdown, enviando sin formato: {e}")
            await update.message.reply_text(
                mensaje.replace('*', '').replace('`', '').replace('_', ''),
                reply_markup=self.get_main_keyboard()
            )

    async def forzar_chequeo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fuerza un chequeo manual"""
        await update.message.reply_text("🔍 Forzando chequeo...")
        await self.chequear_cargadores()
        await update.message.reply_text(
            "✅ Chequeo completado",
            reply_markup=self.get_main_keyboard()
        )
    
    async def toggle_pausa(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pausa o reanuda los escaneos"""
        self.cambiar_pausa_escaneo(not self.scanning_paused)
        estado = "⏸️ PAUSADO" if self.scanning_paused else "▶️ ACTIVO"
        
        mensaje = f"🔄 *Estado del escaneo:* {estado}\n\n"
        if self.scanning_paused:
            mensaje += "Los escaneos automáticos están pausados.\n"
            mensaje += "Puedes realizar chequeos manuales cuando quieras."
        else:
            mensaje += f"Los escaneos automáticos están activos.\n"
            mensaje += f"Intervalo: {self.check_interval//60} minutos"
        
        await update.message.reply_text(
            mensaje, 
            parse_mode='Markdown',
            reply_markup=self.get_main_keyboard()
        )
    
    async def cambiar_intervalo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra opciones para cambiar el intervalo"""
        keyboard = [
            [InlineKeyboardButton("30 seg", callback_data='interval_30')],
            [InlineKeyboardButton("1 min", callback_data='interval_60')],
            [InlineKeyboardButton("2 min", callback_data='interval_120')],
            [InlineKeyboardButton("5 min", callback_data='interval_300')],
            [InlineKeyboardButton("10 min", callback_data='interval_600')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        mensaje = f"⏱️ *Cambiar Intervalo de Escaneo*\n\n"
        mensaje += f"Intervalo actual: *{self.check_interval}s* ({self.check_interval//60} min)\n"
        amplitud = self.jitter_amplitude()
        if amplitud > 0:
            mensaje += f"Variación aleatoria: *±{amplitud:.0f}s*\n"
        mensaje += "\nSelecciona el nuevo intervalo:"
        
        await update.message.reply_text(mensaje, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def mostrar_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra información del sistema"""
        mensaje = "ℹ️ *Información del Sistema*\n\n"
        mensaje += f"📍 Ubicación: {self.latitude}, {self.longitude}\n"
        mensaje += f"🔌 Cargadores: {', '.join(map(str, self.cupr_ids))}\n"
        mensaje += f"⏱️ Intervalo: {self.check_interval}s ({self.check_interval//60} min)\n"
        amplitud = self.jitter_amplitude()
        if amplitud > 0:
            mensaje += f"🎲 Variación: ±{amplitud:.0f}s\n"
        if self.next_scan_time and not self.scanning_paused:
            mensaje += f"⏭️ Próximo escaneo: {self.next_scan_time.strftime('%H:%M:%S')}\n"
        mensaje += f"🔄 Estado: {'⏸️ PAUSADO' if self.scanning_paused else '▶️ ACTIVO'}\n"
        if self.hunt_task:
            mensaje += f"🎯 Caza: {'⏸️ pausada' if self.hunt_paused else '🟢 activa'}, cada {self._formatear_segundos(self.hunt_interval)}\n"
        mensaje += f"📱 Versión app: `{self.app_version}`\n"
        mensaje += f"💾 Base de datos: {self.db_path}\n"
        
        await update.message.reply_text(
            mensaje, 
            parse_mode='Markdown',
            reply_markup=self.get_main_keyboard()
        )
    
    def _load_app_version(self):
        """Carga la versión de la app desde la DB (prioridad sobre env)"""
        saved = self.get_config('app_version')
        if saved:
            self._apply_version(saved)
            print(f"📱 Versión cargada desde DB: {saved}")
        else:
            print(f"📱 Versión desde env: {self.app_version}")

    def _apply_version(self, version):
        """Aplica una versión a los headers de la API"""
        self.app_version = version
        os.environ['IBERDROLA_APP_VERSION'] = version
        self.api.base_headers['versionApp'] = f'ANDROID-{version}'
        self.api.base_headers['User-Agent'] = f'Iberdrola/{version}/Dalvik/2.1.0 (Linux; U; Android 13; SM-G991B Build/TP1A.220624.014)'

    async def ver_version(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra la versión actual y permite cambiarla"""
        mensaje = f"📱 *Versión de la App Iberdrola*\n\n"
        mensaje += f"Versión actual: `{self.app_version}`\n\n"
        mensaje += "Envía la nueva versión (ej: `4.36.9`):"

        self.waiting_for_version = True
        await update.message.reply_text(mensaje, parse_mode='Markdown')

    async def _set_new_version(self, update: Update, version_str: str):
        """Establece una nueva versión de la app"""
        import re
        version_str = version_str.strip()

        if not re.match(r'^\d+\.\d+\.\d+$', version_str):
            await update.message.reply_text(
                f"❌ Formato inválido: `{version_str}`\n\nUsa formato X.Y.Z (ej: `4.36.9`)",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
            return

        # Guardar en DB (persiste entre reinicios del contenedor)
        self.set_config('app_version', version_str)

        # Aplicar en memoria
        self._apply_version(version_str)

        await update.message.reply_text(
            f"✅ *Versión actualizada a `{version_str}`*\n\n"
            f"Headers de API actualizados.\n"
            f"💾 Guardado en DB (persistente).",
            parse_mode='Markdown',
            reply_markup=self.get_main_keyboard()
        )

    async def boton_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los botones inline (para cambiar intervalo)"""
        query = update.callback_query
        await query.answer()
        
        if query.data.startswith('interval_'):
            nuevo_intervalo = int(query.data.split('_')[1])
            self.check_interval = nuevo_intervalo
            self.set_config('check_interval', nuevo_intervalo)
            
            mensaje = f"✅ *Intervalo actualizado*\n\n"
            mensaje += f"Nuevo intervalo: *{nuevo_intervalo}s* ({nuevo_intervalo//60} min)\n\n"
            mensaje += "⚠️ *Nota:* El cambio se aplicará en el próximo ciclo de escaneo."
            
            await query.edit_message_text(mensaje, parse_mode='Markdown')
            
            # Enviar mensaje con el teclado principal
            await self.app.bot.send_message(
                chat_id=query.message.chat_id,
                text="Usa los botones del teclado para continuar:",
                reply_markup=self.get_main_keyboard()
            )
        
        # === CALLBACKS DE RESERVA ===
        elif query.data.startswith('reserve_'):
            # reserve_CUPRID_SOCKETID
            parts = query.data.split('_')
            cupr_id = int(parts[1])
            socket_id = int(parts[2])
            await self._ejecutar_reserva(query, cupr_id, socket_id)
        
        elif query.data == 'cancel_reservation':
            await self._cancelar_reserva(query)
        
        elif query.data == 'stop_auto_renew':
            await self._stop_auto_renew(query)

        elif query.data.startswith('hunt_'):
            await self._callback_caza(query)
    
    async def iniciar_reserva(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra los favoritos disponibles para reservar"""
        await update.message.reply_text("⏳ Obteniendo cargadores favoritos disponibles...")
        
        # Verificar autenticación
        authenticated, error = await self.ensure_authenticated()
        if not authenticated:
            await update.message.reply_text(
                f"❌ *Error de autenticación*\n\n{error}",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
            return
        
        # Verificar si ya hay una reserva activa
        transaction = self.api.get_transaction_in_progress(lat=self.latitude, lon=self.longitude)
        if transaction and transaction.get('reservationInProgress'):
            await update.message.reply_text(
                "⚠️ *Ya tienes una reserva activa*\n\nUsa 📋 Mi Reserva para ver detalles o cancelar.",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
            return
        
        # Obtener favoritos
        favoritos = self.api.obtener_favoritos(lat=self.latitude, lon=self.longitude)
        
        if not favoritos:
            await update.message.reply_text(
                "❌ No tienes cargadores favoritos o error al obtenerlos.",
                reply_markup=self.get_main_keyboard()
            )
            return
        
        # Filtrar favoritos disponibles y obtener sus IDs
        buttons = []
        mensaje = "📅 *RESERVAR CARGADOR*\n\n"
        mensaje += "Selecciona un cargador disponible:\n\n"
        
        disponibles = 0
        cupr_ids_disponibles = []
        
        for fav in favoritos:
            location = fav.get('locationData', {})
            nombre = location.get('cuprName', 'Sin nombre')
            cupr_id = location.get('cuprId')
            status = fav.get('cpStatus', {}).get('statusCode', 'UNKNOWN')
            
            if status == 'AVAILABLE':
                disponibles += 1
                cupr_ids_disponibles.append(cupr_id)
            else:
                mensaje += f"❌ _{nombre}_ - {status}\n"
        
        if disponibles == 0:
            if self.hunt_task:
                await update.message.reply_text(
                    "❌ *No hay cargadores disponibles*\n\n🎯 Ya hay una caza en marcha, te aviso en cuanto reserve.",
                    parse_mode='Markdown',
                    reply_markup=self._botones_control_caza()
                )
                return
            await update.message.reply_text(
                "❌ *No hay cargadores disponibles*\n\nTodos tus favoritos están ocupados o fuera de servicio.\n\n"
                "🎯 ¿Lo reservo yo en cuanto quede uno libre? Elige cada cuánto escanear:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(self._botones_intervalo_caza("hunt_start_"))
            )
            return
        
        # Obtener estado de conectores de los cargadores disponibles
        conectores = self.api.obtener_estado_conectores(cupr_ids_disponibles, lat=self.latitude, lon=self.longitude)
        
        if conectores:
            preferred_type = self.tipo_preferido
            available = [c for c in conectores if c.get('status') == 'AVAILABLE']

            # Priorizar tipo preferido: primero los preferidos, luego el resto
            if preferred_type:
                available.sort(key=lambda c: 0 if c.get('socketType', '').lower() == preferred_type.lower() else 1)

            for c in available:
                cupr_id = c.get('cuprId')
                socket_id = c.get('physicalSocketId')
                nombre = c.get('cuprName', 'Sin nombre')
                socket_type = c.get('socketType', '')
                is_preferred = preferred_type and socket_type.lower() == preferred_type.lower()
                label_prefix = "⭐" if is_preferred else "🔌"

                mensaje += f"✅ *{nombre}*\n   Socket {socket_id} ({socket_type}){' ⭐' if is_preferred else ''}\n\n"
                buttons.append([
                    InlineKeyboardButton(
                        f"{label_prefix} {nombre[:25]} ({socket_type})",
                        callback_data=f"reserve_{cupr_id}_{socket_id}"
                    )
                ])
        
        if not buttons:
            await update.message.reply_text(
                "❌ *No se encontraron sockets disponibles*\n\nIntenta de nuevo en unos segundos.",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
            return
        
        mensaje += f"\n_Disponibles: {disponibles}/{len(favoritos)}_\n"
        mensaje += "\n⚠️ *Precio de reserva: 1€* (30 min)"
        
        await update.message.reply_text(
            mensaje,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    
    async def ver_mi_reserva(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra la reserva activa del usuario"""
        await update.message.reply_text("⏳ Consultando tu reserva...")
        
        # Verificar autenticación
        authenticated, error = await self.ensure_authenticated()
        if not authenticated:
            await update.message.reply_text(
                f"❌ *Error de autenticación*\n\n{error}",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
            return
        
        # Obtener reserva activa
        reservation = self.api.get_user_reservation(lat=self.latitude, lon=self.longitude)
        
        if not reservation or not reservation.get('reservationId'):
            await update.message.reply_text(
                "📋 *No tienes ninguna reserva activa*\n\nUsa 📅 Reservar para crear una.",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
            return
        
        # Formatear información de la reserva
        nombre = reservation.get('chargePointInfo', {}).get('foldedTitle', 'N/A')
        socket_id = reservation.get('physicalSocketId')
        socket_type = reservation.get('socketType', {}).get('socketName', 'N/A')
        start_date = reservation.get('startDate', 'N/A')
        end_date = reservation.get('endDate', 'N/A')
        price = reservation.get('reserve', {}).get('finalPrice', 'N/A')
        cancel_cost = reservation.get('cancelationCost', 'N/A')
        status = reservation.get('status', {}).get('description', 'N/A')
        
        # Formatear fechas
        try:
            start_dt = datetime.fromisoformat(start_date.replace('+00:00', '+00:00'))
            end_dt = datetime.fromisoformat(end_date.replace('+00:00', '+00:00'))
            start_str = start_dt.strftime('%H:%M')
            end_str = end_dt.strftime('%H:%M')
        except:
            start_str = start_date
            end_str = end_date
        
        mensaje = "📋 *TU RESERVA ACTIVA*\n\n"
        mensaje += f"📍 *{nombre}*\n"
        mensaje += f"🔌 Socket: {socket_id} ({socket_type})\n"
        mensaje += f"⏰ Horario: {start_str} - {end_str}\n"
        mensaje += f"💰 Precio: {price}€\n"
        mensaje += f"📊 Estado: {status}\n"
        
        # Mostrar estado de auto-renovación
        if self.auto_renew_active and self.auto_renew_next_time:
            next_time_str = self.auto_renew_next_time.strftime('%H:%M')
            mensaje += f"\n🔄 *Auto-renovación: ACTIVA*\n"
            mensaje += f"⏱️ Próxima: *{next_time_str}* (prepara 3DS)"
        else:
            mensaje += f"\n🔄 Auto-renovación: Inactiva"
        
        mensaje += f"\n\n💸 Coste cancelación: {cancel_cost}€"
        
        buttons = [
            [InlineKeyboardButton("❌ Cancelar Reserva", callback_data="cancel_reservation")]
        ]
        
        # Agregar botón de cancelar auto-renovación si está activa
        if self.auto_renew_active:
            buttons.append([InlineKeyboardButton("🔄 Desactivar Renovación Automática", callback_data="stop_auto_renew")])
        
        await update.message.reply_text(
            mensaje,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    
    async def _proceso_reserva(self, cupr_id: int, socket_id: int, progreso=None):
        """Pago y reserva de un conector. Lo comparten la reserva manual, la
        renovación automática y la caza.

        progreso es una corrutina opcional que recibe textos de avance.
        Devuelve (resultado, error, fase). resultado es la respuesta de reserveNow
        o None, y fase dice dónde falló: 'previo' (antes de pagar, sin coste),
        'pago' (3DS rechazado o sin aprobar) o 'reserva' (con el pago ya hecho).
        """
        async def avisar(texto):
            if progreso:
                try:
                    await progreso(texto)
                except Exception as e:
                    print(f"⚠️ No se pudo mostrar el progreso: {e}")

        try:
            from redsys_payment import process_reservation_payment

            # 1. Obtener método de pago
            payment = await asyncio.to_thread(
                self.api.get_payment_method, lat=self.latitude, lon=self.longitude
            )
            if not payment:
                return None, "No hay método de pago configurado en la app Iberdrola.", 'previo'

            # 2. Obtener orderId
            order = await asyncio.to_thread(
                self.api.get_order_id, cupr_id, socket_id, amount=1.0,
                lat=self.latitude, lon=self.longitude
            )
            if not order:
                return None, "Error al generar orden de pago.", 'previo'

            order_id = order.get('orderId')
            await avisar(
                f"💳 Procesando pago (Order: {order_id})...\n\n"
                "📱 Aprueba el pago en tu app bancaria si es necesario."
            )

            # 3. Procesar pago (en un hilo, puede tardar hasta que se apruebe el 3DS)
            payment_success = await asyncio.to_thread(
                process_reservation_payment,
                order_data=order,
                payment_token=payment['token'],
                amount_cents=100,
                use_3ds=True,
                timeout_seconds=120
            )
            if not payment_success:
                return None, "Error en el pago. La transacción no se completó.", 'pago'

            # 4. Ejecutar reserva
            result = await asyncio.to_thread(
                self.api.reserve_charger, cupr_id, socket_id, order_id,
                lat=self.latitude, lon=self.longitude
            )
            if not result:
                return None, "El pago se procesó pero la reserva falló.", 'reserva'

            return result, None, None

        except Exception as e:
            print(f"❌ Error durante la reserva: {e}")
            # No se sabe en qué punto falló: se trata como si ya se hubiera pagado
            return None, f"Error durante la reserva: {str(e)[:100]}", 'reserva'

    def _iniciar_auto_renovacion(self, cupr_id: int, socket_id: int):
        """Arranca la renovación automática de una reserva recién hecha."""
        self.auto_renew_active = True
        self.auto_renew_cupr_id = cupr_id
        self.auto_renew_socket_id = socket_id
        self.auto_renew_next_time = datetime.now() + timedelta(minutes=self.RENEW_INTERVAL_MINUTES)
        if self.auto_renew_task:
            self.auto_renew_task.cancel()
        self.auto_renew_task = asyncio.create_task(self._auto_renew_loop())
        self.publicar_estado()

    def _mensaje_reserva_ok(self, result, cupr_id: int, socket_id: int, cabecera: str):
        """Texto y botones de una reserva hecha, con la auto-renovación ya activa."""
        nombre = result.get('chargePointInfo', {}).get('foldedTitle', 'Cargador')
        if not nombre or nombre == 'Cargador':
            nombre = f"Cargador {cupr_id}"

        end_date = result.get('endDate', 'N/A')
        end_dt = self._parse_fecha(end_date)
        end_str = end_dt.strftime('%H:%M') if end_dt else end_date

        next_renew_str = self.auto_renew_next_time.strftime('%H:%M')

        mensaje = f"{cabecera}\n\n"
        mensaje += f"📍 {nombre}\n"
        mensaje += f"🔌 Socket: {socket_id}\n"
        mensaje += f"⏰ Válida hasta: {end_str}\n"
        mensaje += f"💰 Precio: 1€\n\n"
        mensaje += "🔄 *Auto-renovación ACTIVA*\n"
        mensaje += f"⏱️ Próxima renovación: *{next_renew_str}*\n"
        mensaje += f"(cada {self.RENEW_INTERVAL_MINUTES} min)\n\n"
        mensaje += "📱 Prepara tu app bancaria para aprobar el 3DS."

        # Botón para activar timer, 1 min antes para prepararse
        timer_minutes = self.RENEW_INTERVAL_MINUTES - 1
        timer_url = f"https://www.google.com/search?q=set+timer+{timer_minutes}+minutes"
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton(f"⏱️ Poner timer {timer_minutes} min", url=timer_url)]])
        return mensaje, buttons

    async def _ejecutar_reserva(self, query, cupr_id: int, socket_id: int):
        """Ejecuta la reserva de un cargador desde el botón de Reservar"""
        await query.edit_message_text("⏳ Procesando reserva...")

        result, error, _ = await self._proceso_reserva(
            cupr_id, socket_id, progreso=query.edit_message_text
        )
        if not result:
            await query.edit_message_text(f"❌ {error}")
            return False

        # Una reserva manual deja sin sentido una caza en marcha
        if self.hunt_task:
            await self._parar_caza("reserva manual")

        self._iniciar_auto_renovacion(cupr_id, socket_id)
        mensaje, buttons = self._mensaje_reserva_ok(result, cupr_id, socket_id, "🎉 *¡RESERVA EXITOSA!*")
        await query.edit_message_text(mensaje, parse_mode='Markdown', reply_markup=buttons)
        asyncio.create_task(self.reescanear_tras_accion())
        return True
    
    async def _cancelar_reserva(self, query):
        """Cancela la reserva activa y detiene auto-renovación"""
        await query.edit_message_text("⏳ Cancelando reserva...")
        
        # Detener auto-renovación
        self.auto_renew_active = False
        if self.auto_renew_task:
            self.auto_renew_task.cancel()
            self.auto_renew_task = None
        
        # Obtener datos de la reserva activa
        transaction = self.api.get_transaction_in_progress(lat=self.latitude, lon=self.longitude)
        
        if not transaction or not transaction.get('reservationInProgress'):
            await query.edit_message_text("ℹ️ No hay reserva activa para cancelar.")
            return
        
        cupr_id = transaction.get('cuprId')
        socket_id = transaction.get('physicalSocketId')

        # Cancelar y confirmar con Iberdrola (su respuesta no es fiable, ver _cancelar_y_confirmar)
        cancelada = await self._cancelar_y_confirmar(cupr_id, socket_id)
        asyncio.create_task(self.reescanear_tras_accion())

        if cancelada:
            self.publicar('reserva', {'tipo': 'cancelada', 'mensaje': 'Reserva cancelada'})
            self.publicar_estado()
            await query.edit_message_text("✅ *Reserva cancelada correctamente*\n\n🔄 Auto-renovación detenida.", parse_mode='Markdown')
        else:
            await query.edit_message_text(
                "❌ Iberdrola sigue mostrando la reserva tras 3 intentos de cancelarla. "
                "La auto-renovación está parada, así que caducará sola."
            )
    
    async def _stop_auto_renew(self, query):
        """Detiene la auto-renovación sin cancelar la reserva activa"""
        await query.edit_message_text("⏳ Desactivando renovación automática...")
        
        # Detener auto-renovación
        self.auto_renew_active = False
        if self.auto_renew_task:
            self.auto_renew_task.cancel()
            self.auto_renew_task = None
        
        await query.edit_message_text(
            "✅ *Renovación automática desactivada*\n\n"
            "Tu reserva actual permanece activa.\n"
            "Usa 📋 Mi Reserva para ver detalles.",
            parse_mode='Markdown'
        )
    
    async def _auto_renew_loop(self):
        """Loop de auto-renovación de reservas"""
        print(f"🔄 Auto-renovación iniciada (cada {self.RENEW_INTERVAL_MINUTES} minutos)")
        
        reintento = None  # Segundos hasta reintentar si Iberdrola no respondió
        while self.auto_renew_active:
            # Esperar el intervalo de renovación. El jitter aquí solo resta
            # tiempo: adelantarse es seguro, pasarse de los 15 min gratis no.
            await asyncio.sleep(reintento or (self.RENEW_INTERVAL_MINUTES * 60 - self._rand.uniform(0, 30)))
            reintento = None
            
            if not self.auto_renew_active:
                break
            
            print(f"🔄 Iniciando renovación automática...")
            
            # Verificar si aún hay reserva activa (podría haber empezado a cargar). Una
            # consulta fallida (None) no significa que la reserva haya caducado: se
            # reintenta antes de concluir nada, o un fallo puntual pararía la renovación.
            transaction = None
            for _ in range(3):
                transaction = await asyncio.to_thread(
                    self.api.get_transaction_in_progress, lat=self.latitude, lon=self.longitude
                )
                if transaction is not None:
                    break
                await asyncio.sleep(5)
            if transaction is None:
                print("⚠️ Iberdrola no responde. Se reintenta la renovación en 30 s.")
                reintento = 30
                continue
            
            if transaction and transaction.get('chargeInProgress'):
                # Ya está cargando, detener auto-renovación
                print("🔌 Carga en progreso detectada. Deteniendo auto-renovación.")
                self.auto_renew_active = False
                await self._send_notification(
                    "🔌 *Carga iniciada*\n\n"
                    "Auto-renovación detenida porque el vehículo está cargando.",
                    evento=('reserva', 'carga_iniciada')
                )
                break
            
            if not transaction or not transaction.get('reservationInProgress'):
                # No hay reserva activa (expiró o se canceló externamente)
                print("⚠️ No hay reserva activa. Deteniendo auto-renovación.")
                self.auto_renew_active = False
                await self._send_notification(
                    "⚠️ *Reserva expirada*\n\n"
                    "La reserva ya no está activa. Auto-renovación detenida.",
                    evento=('reserva', 'expirada')
                )
                break
            
            # Cancelar reserva actual
            cupr_id = self.auto_renew_cupr_id
            socket_id = self.auto_renew_socket_id
            
            print(f"   Cancelando reserva actual (cupr:{cupr_id}, socket:{socket_id})...")
            # Solo 2 intentos: cada uno puede llevar 15 s y hay que volver a reservar antes
            # de que el hueco lo coja otro.
            if not await self._cancelar_y_confirmar(cupr_id, socket_id, intentos=2):
                print("❌ No se pudo confirmar la cancelación. La reserva sigue, sin renovar.")
                self.auto_renew_active = False
                await self._send_notification(
                    "⚠️ *No se pudo renovar*\n\n"
                    "Iberdrola no confirmó la cancelación de la reserva actual, así que sigue activa "
                    "pero ya no se renovará. Caducará a los 30 minutos de hacerla.",
                    evento=('reserva', 'renovacion_fallida')
                )
                break

            # El estado del conector también va con retraso: se mira hasta tres veces
            socket_available = False
            for _ in range(3):
                conectores = await asyncio.to_thread(
                    self.api.obtener_estado_conectores, [cupr_id], self.latitude, self.longitude
                )
                if any(c.get('physicalSocketId') == socket_id and c.get('status') == 'AVAILABLE'
                       for c in conectores or []):
                    socket_available = True
                    break
                await asyncio.sleep(3)
            
            if not socket_available:
                print("❌ Socket ya no disponible. Deteniendo auto-renovación.")
                self.auto_renew_active = False
                await self._send_notification(
                    "❌ *Socket no disponible*\n\n"
                    "El cargador ya no está disponible. Auto-renovación detenida.",
                    evento=('reserva', 'renovacion_fallida')
                )
                break
            
            # Re-reservar
            print(f"   Creando nueva reserva...")
            success = await self._ejecutar_reserva_silenciosa(cupr_id, socket_id)
            
            if success:
                print("   ✅ Reserva renovada correctamente")
                self.auto_renew_next_time = datetime.now() + timedelta(minutes=self.RENEW_INTERVAL_MINUTES)
                next_renew_str = self.auto_renew_next_time.strftime('%H:%M')
                
                # Crear botón de timer
                timer_minutes = self.RENEW_INTERVAL_MINUTES - 1
                timer_url = f"https://www.google.com/search?q=set+timer+{timer_minutes}+minutes"
                timer_buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"⏱️ Timer {timer_minutes} min", url=timer_url)
                ]])
                
                await self._send_notification(
                    "🔄 *Reserva renovada*\n\n"
                    f"Cargador {cupr_id}, Socket {socket_id}\n"
                    f"⏱️ Próxima renovación: *{next_renew_str}*\n\n"
                    "📱 Prepara tu app bancaria.",
                    reply_markup=timer_buttons,
                    evento=('reserva', 'renovada', {
                        'cuprId': cupr_id, 'physicalSocketId': socket_id,
                        'proxima_renovacion': self._iso(self.auto_renew_next_time),
                    })
                )
                asyncio.create_task(self.reescanear_tras_accion())
            else:
                print("   ❌ Error al renovar reserva")
                self.auto_renew_active = False
                await self._send_notification(
                    "❌ *Error al renovar reserva*\n\n"
                    "No se pudo renovar. Auto-renovación detenida.",
                    evento=('reserva', 'renovacion_fallida')
                )
                break
        
        print("🔄 Auto-renovación finalizada")
        self.publicar_estado()
    
    async def _ejecutar_reserva_silenciosa(self, cupr_id: int, socket_id: int):
        """Ejecuta una reserva sin interacción de usuario (para renovaciones)"""
        result, error, _ = await self._proceso_reserva(cupr_id, socket_id)
        if not result:
            print(f"❌ Error en reserva silenciosa: {error}")
        return result is not None
    
    async def _send_notification(self, message: str, reply_markup=None, evento=None):
        """Envía una notificación al usuario. Devuelve True si llegó.

        Como enviar_mensaje, si el Markdown falla se reintenta en texto plano.
        evento=(canal, tipo[, extra]) la publica además en /api/eventos, con el
        mismo texto sin formato, y emite el estado actualizado.
        """
        if evento:
            canal, tipo = evento[0], evento[1]
            extra = evento[2] if len(evento) > 2 else {}
            self.publicar(canal, {'tipo': tipo, 'mensaje': self._texto_plano(message), **extra})
            self.publicar_estado()
        markup = reply_markup or self.get_main_keyboard()
        for parse_mode in ('Markdown', None):
            try:
                await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    parse_mode=parse_mode,
                    reply_markup=markup
                )
                return True
            except Exception as e:
                print(f"❌ Error enviando notificación ({parse_mode or 'texto plano'}): {e}")
        return False

    # ==================== EVENTOS Y DATOS PARA LA API ====================

    def suscribir(self):
        """Cola de eventos para un cliente de /api/eventos."""
        cola = asyncio.Queue(maxsize=100)
        self._suscriptores.add(cola)
        return cola

    def desuscribir(self, cola):
        self._suscriptores.discard(cola)

    def publicar(self, evento, datos):
        """Envía un evento a todos los clientes conectados.

        Un cliente que no lee pierde los eventos más viejos en vez de hacer crecer
        la memoria: al reconectar recibe el estado completo y los avisos que se
        perdió (eventos_desde). El estado no lleva id: siempre se manda entero.
        """
        id_evento = None
        if evento != 'estado':
            self._contador_eventos += 1
            id_evento = f"{self._arranque_id}.{self._contador_eventos}"
            self._eventos_recientes.append((id_evento, evento, datos))
        for cola in list(self._suscriptores):
            if cola.full():
                try:
                    cola.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            cola.put_nowait((id_evento, evento, datos))

    def eventos_desde(self, ultimo_id):
        """Avisos posteriores a ultimo_id (el Last-Event-ID de un cliente).

        Si el id es de un arranque anterior del proceso, todo lo que hay en memoria es
        posterior. Si es de este arranque pero ya salió del buffer, se manda lo que
        queda: es lo más que se puede recuperar.
        """
        if not ultimo_id:
            return []
        arranque, _, numero = ultimo_id.partition('.')
        if arranque != self._arranque_id:
            return list(self._eventos_recientes)
        try:
            numero = int(numero)
        except ValueError:
            return []
        return [e for e in self._eventos_recientes if int(e[0].split('.')[1]) > numero]

    def publicar_estado(self):
        if self._suscriptores:
            self.publicar('estado', self.estado_dict())

    @staticmethod
    def _iso(dt):
        return dt.replace(microsecond=0).isoformat() if dt else None

    @staticmethod
    def _texto_plano(mensaje):
        return mensaje.replace('*', '').replace('`', '').replace('_', ' ').strip()

    @staticmethod
    def _datos_conector(con):
        return {
            'cuprId': con.get('cuprId'),
            'cuprName': con.get('cuprName'),
            'physicalSocketId': con.get('physicalSocketId'),
            'socketCode': str(con.get('socketCode', '')),
            'socketType': con.get('socketType'),
        }

    def caza_dict(self):
        if not self.hunt_task:
            estado = 'inactiva'
        else:
            estado = 'pausada' if self.hunt_paused else 'activa'
        return {
            'estado': estado,
            'intervalo': self.hunt_interval,
            'intervalo_min': self.hunt_min_interval,
            'opciones': [s for s in self.HUNT_INTERVAL_OPTIONS if s >= self.hunt_min_interval],
            'escaneos': self.hunt_scans,
            'proximo': self._iso(self.hunt_next) if self.hunt_task and not self.hunt_paused else None,
            'hasta': self._iso(self.hunt_until) if self.hunt_task else None,
            'fallos': self.hunt_failures,
        }

    def estado_dict(self):
        conectores = self.last_conectores or []
        reservas = self.reservas_en_curso(conectores)
        ahora = datetime.now()
        lista = []
        for con in conectores:
            item = {**self._datos_conector(con), 'maxPower': con.get('maxPower'), 'status': con.get('status')}
            if con['physicalSocketId'] in reservas:
                inicio, fiable = reservas[con['physicalSocketId']]
                marcas = []
                if inicio:
                    for minutos in self.reservation_marks:
                        hora = inicio + timedelta(minutes=minutos)
                        marcas.append({'minutos': minutos, 'hora': self._iso(hora), 'cumplida': ahora >= hora})
                item['reserva'] = {'inicio': self._iso(inicio), 'fiable': fiable, 'marcas': marcas}
            lista.append(item)

        return {
            'ahora': self._iso(ahora),
            'escaneo': {
                'pausado': self.scanning_paused,
                'intervalo': self.check_interval,
                'jitter': round(self.jitter_amplitude(), 1),
                'ultimo': self._iso(self.last_scan_at),
                'proximo': None if self.scanning_paused else self._iso(self.next_scan_time),
            },
            'conectores': lista,
            'tipo_preferido': self.tipo_preferido or None,
            'caza': self.caza_dict(),
            'mi_reserva': {
                'auto_renovacion': self.auto_renew_active,
                'cuprId': self.auto_renew_cupr_id if self.auto_renew_active else None,
                'physicalSocketId': self.auto_renew_socket_id if self.auto_renew_active else None,
                'proxima_renovacion': self._iso(self.auto_renew_next_time) if self.auto_renew_active else None,
                'en_curso': self.reserva_en_curso,
            },
        }

    def historial_dict(self, limite=20):
        reservas = self.historial_reservas(limite=limite)
        lista = [{
            'physicalSocketId': r['socket_id'],
            'cuprName': r['cuprName'],
            'socketCode': r['socketCode'],
            'socketType': r['socketType'],
            'inicio': self._iso(r['inicio']),
            'fiable': r['fiable'],
            'fin': self._iso(r['fin']),
            'fin_estado': r['fin_estado'],
            'minutos': r['minutos'],
        } for r in reservas]
        cerradas = [r for r in reservas if r['minutos'] is not None]
        return {
            'reservas': lista,
            'resumen': {
                'total': len(cerradas),
                'media_minutos': round(sum(r['minutos'] for r in cerradas) / len(cerradas), 1) if cerradas else None,
                'usadas': sum(1 for r in cerradas if r['fin_estado'] in ('OCCUPIED', 'EV_CONNECTED')),
                'liberadas': sum(1 for r in cerradas if r['fin_estado'] == 'AVAILABLE'),
            },
        }

    def cambios_list(self, limite=200, desde=None):
        """Cambios de estado reales (sin las filas de partida), del más nuevo al más viejo."""
        consulta = (
            'SELECT physicalSocketId, cuprName, socketCode, socketType, '
            'estado_anterior, estado_nuevo, detectado '
            'FROM historial_estados WHERE estado_anterior IS NOT NULL'
        )
        params = []
        if desde:
            consulta += ' AND detectado >= ?'
            params.append(desde.isoformat())
        consulta += ' ORDER BY id DESC LIMIT ?'
        params.append(limite)
        conn = sqlite3.connect(self.db_path)
        filas = conn.execute(consulta, params).fetchall()
        conn.close()
        return [{
            'physicalSocketId': f[0], 'cuprName': f[1], 'socketCode': f[2], 'socketType': f[3],
            'anterior': f[4], 'nuevo': f[5], 'detectado': self._iso(self._parse_fecha(f[6])),
        } for f in filas]

    @property
    def tipo_preferido(self):
        """Tipo de conector preferido al reservar. Lo que se haya guardado desde la app
        manda sobre PREFERRED_CONNECTOR_TYPE. Cadena vacía si no hay preferencia."""
        guardado = self.get_config('preferred_connector_type')
        valor = guardado if guardado is not None else os.getenv('PREFERRED_CONNECTOR_TYPE', '')
        return valor.strip()

    def cambiar_tipo_preferido(self, tipo):
        self.set_config('preferred_connector_type', (tipo or '').strip())
        self.publicar_estado()

    def ajustes_dict(self):
        return {
            'tipo_preferido': self.tipo_preferido or None,
            'intervalo_escaneo': self.check_interval,
            'escaneo_pausado': self.scanning_paused,
            'marcas_reserva': self.reservation_marks,
            'renovacion_minutos': self.RENEW_INTERVAL_MINUTES,
        }

    def cambiar_intervalo_escaneo(self, segundos):
        self.check_interval = segundos
        self.set_config('check_interval', segundos)
        self.publicar_estado()

    def cambiar_pausa_escaneo(self, pausado):
        self.scanning_paused = pausado
        # Se guarda: si no, cada reinicio o despliegue reanudaba el escaneo sin avisar
        self.set_config('scanning_paused', '1' if pausado else '0')
        self.publicar_estado()

    def reanudar_caza(self):
        self.hunt_paused = False
        self.hunt_failures = 0
        self._guardar_estado_caza()
        self.publicar('caza', {'tipo': 'reanudada', 'mensaje': 'Caza reanudada'})
        self.publicar_estado()

    def cambiar_intervalo_caza(self, segundos):
        self.hunt_interval = max(self.hunt_min_interval, segundos)
        self.set_config('hunt_interval', self.hunt_interval)
        self.publicar_estado()

    async def hay_reserva_activa(self):
        """Pregunta a Iberdrola si hay reserva propia. None si no hay sesión."""
        authenticated, _ = await self.ensure_authenticated()
        if not authenticated:
            return None
        transaction = await asyncio.to_thread(
            self.api.get_transaction_in_progress, lat=self.latitude, lon=self.longitude
        )
        return bool(transaction and transaction.get('reservationInProgress'))

    async def empezar_caza_api(self, intervalo=None):
        self.hunt_interval = max(self.hunt_min_interval, intervalo or self.hunt_interval)
        self.set_config('hunt_interval', self.hunt_interval)
        self._arrancar_caza(datetime.now() + timedelta(minutes=self.hunt_max_minutes), pausada=False)
        texto = f"Caza iniciada desde la app, cada {self._formatear_segundos(self.hunt_interval)}"
        self.publicar('caza', {'tipo': 'iniciada', 'mensaje': texto})
        await self.enviar_mensaje(f"🎯 {texto}")

    async def pausar_caza_api(self):
        await self._pausar_caza("app")
        self.publicar('caza', {'tipo': 'pausada', 'mensaje': 'Caza pausada'})

    async def cancelar_caza_api(self):
        await self._parar_caza("app")
        self.publicar('caza', {'tipo': 'cancelada', 'mensaje': 'Caza cancelada'})

    async def reserva_detalle(self):
        """Reserva propia según Iberdrola, ya en el formato de la API."""
        reservation = await asyncio.to_thread(
            self.api.get_user_reservation, lat=self.latitude, lon=self.longitude
        )
        if not reservation or not reservation.get('reservationId'):
            return {'activa': False}
        return {
            'activa': True,
            'nombre': reservation.get('chargePointInfo', {}).get('foldedTitle'),
            'cuprId': reservation.get('cuprId') or self.auto_renew_cupr_id,
            'physicalSocketId': reservation.get('physicalSocketId'),
            'socketType': reservation.get('socketType', {}).get('socketName'),
            'inicio': self._iso(self._parse_fecha(reservation.get('startDate'))),
            'fin': self._iso(self._parse_fecha(reservation.get('endDate'))),
            'precio': reservation.get('reserve', {}).get('finalPrice'),
            'coste_cancelacion': reservation.get('cancelationCost'),
            'estado': reservation.get('status', {}).get('description'),
            'auto_renovacion': self.auto_renew_active,
            'proxima_renovacion': self._iso(self.auto_renew_next_time) if self.auto_renew_active else None,
        }

    async def reservar_api(self, cupr_id, socket_id):
        """Reserva pedida desde la app. Corre en segundo plano y avisa por eventos."""
        self.reserva_en_curso = True
        self.publicar_estado()
        datos = {'cuprId': cupr_id, 'physicalSocketId': socket_id}
        for con in self.last_conectores or []:
            if con.get('physicalSocketId') == socket_id:
                datos = self._datos_conector(con)

        async def progreso(texto):
            self.publicar('reserva', {'tipo': 'progreso', 'mensaje': self._texto_plano(texto), **datos})

        try:
            await progreso("⏳ Procesando reserva...")
            result, error, _ = await self._proceso_reserva(cupr_id, socket_id, progreso=progreso)
        finally:
            self.reserva_en_curso = False

        if not result:
            await self._send_notification(
                f"❌ *No se pudo reservar desde la app*\n\n{error}",
                evento=('reserva', 'error', datos)
            )
            await self.reescanear_tras_accion()
            return

        if self.hunt_task:
            await self._parar_caza("reserva desde la app")
        self._iniciar_auto_renovacion(cupr_id, socket_id)
        mensaje, buttons = self._mensaje_reserva_ok(
            result, cupr_id, socket_id, "🎉 *¡RESERVA EXITOSA!* (desde la app)"
        )
        await self._send_notification(mensaje, reply_markup=buttons, evento=('reserva', 'ok', datos))
        await self.reescanear_tras_accion()

    async def reescanear_tras_accion(self):
        """Escaneo inmediato tras reservar, cancelar o renovar.

        El estado de los conectores solo cambia al escanear. Sin esto, tras una reserva
        el cargador seguía saliendo libre hasta el siguiente escaneo, y nunca si el
        escaneo automático estaba en pausa.

        Se mira dos veces porque el estado del cargador en Iberdrola va con retraso
        respecto a la reserva: a los pocos segundos puede seguir saliendo el anterior.
        """
        for espera in (3, 17):
            await asyncio.sleep(espera)
            await self.chequear_cargadores(forzar=True)

    async def _cancelar_y_confirmar(self, cupr_id, socket_id, intentos=3):
        """Cancela una reserva propia y no da nada por hecho hasta confirmarlo.

        La respuesta de cancelReservation no es fiable: a veces devuelve un 500 y cancela
        igualmente, y a veces tarda unos segundos en reflejarse. Así que se ignora y se
        pregunta a Iberdrola si la reserva sigue ahí, varias veces. Si sigue, se vuelve a
        cancelar. Devuelve True solo cuando Iberdrola confirma que ya no hay reserva.
        """
        for intento in range(1, intentos + 1):
            await asyncio.to_thread(
                self.api.cancel_reservation, cupr_id, socket_id, lat=self.latitude, lon=self.longitude
            )
            for _ in range(5):
                await asyncio.sleep(3)
                transaction = await asyncio.to_thread(
                    self.api.get_transaction_in_progress, lat=self.latitude, lon=self.longitude
                )
                # None es que la consulta falló: eso no confirma nada, se sigue mirando
                if transaction is not None and not transaction.get('reservationInProgress'):
                    return True
            print(f"⚠️ La reserva sigue activa tras el intento {intento} de cancelarla")
        return False

    def parar_auto_renovacion(self):
        self.auto_renew_active = False
        if self.auto_renew_task:
            self.auto_renew_task.cancel()
            self.auto_renew_task = None
        self.publicar_estado()

    async def cancelar_reserva_api(self):
        """Cancelación pedida desde la app. Corre en segundo plano y avisa por eventos:
        con la confirmación y los reintentos puede tardar más de lo que espera una
        petición HTTP."""
        self.cancelacion_en_curso = True
        try:
            self.parar_auto_renovacion()
            transaction = await asyncio.to_thread(
                self.api.get_transaction_in_progress, lat=self.latitude, lon=self.longitude
            )
            if transaction is not None and not transaction.get('reservationInProgress'):
                self.publicar('reserva', {'tipo': 'cancelada', 'mensaje': 'No había ninguna reserva activa'})
                return
            if transaction is None:
                await self._send_notification(
                    "❌ *No se pudo cancelar desde la app*\n\nIberdrola no responde. Inténtalo de nuevo.",
                    evento=('reserva', 'error')
                )
                return

            fin = self._parse_fecha(transaction.get('reservationEndDate'))
            if await self._cancelar_y_confirmar(transaction.get('cuprId'), transaction.get('physicalSocketId')):
                await self._send_notification(
                    "✅ *Reserva cancelada desde la app*\n\n🔄 Auto-renovación detenida.",
                    evento=('reserva', 'cancelada')
                )
            else:
                caduca = f" Caducará sola a las {fin.strftime('%H:%M')}." if fin else ""
                await self._send_notification(
                    "❌ *No se pudo confirmar la cancelación*\n\n"
                    f"Iberdrola sigue mostrando la reserva tras 3 intentos. La renovación automática está parada.{caduca}",
                    evento=('reserva', 'error')
                )
        finally:
            self.cancelacion_en_curso = False
            asyncio.create_task(self.reescanear_tras_accion())

    # ==================== CAZA DE CARGADOR LIBRE ====================
    #
    # Escanea a un intervalo propio hasta que un conector queda libre y lo reserva.
    # Para no cargar la API:
    #   * cada vuelta es el mismo escaneo que el periódico (una sola petición
    #     pública, los favoritos van en caché) y el periódico se salta mientras
    #     la caza lo cubre, así que no se suman,
    #   * el intervalo no baja de HUNT_MIN_INTERVAL y lleva el mismo jitter,
    #   * si la API falla, la espera se duplica hasta 10 minutos,
    #   * las peticiones autenticadas (comprobar reservas, pagar) solo se hacen
    #     cuando ya hay un conector libre,
    #   * se apaga sola a los HUNT_MAX_MINUTES.

    HUNT_INTERVAL_OPTIONS = (30, 45, 60, 120, 300)
    HUNT_MAX_BACKOFF = 600

    @staticmethod
    def _formatear_segundos(segundos):
        segundos = int(segundos)
        if segundos < 60:
            return f"{segundos} s"
        if segundos % 60 == 0:
            return f"{segundos // 60} min"
        return f"{segundos // 60} min {segundos % 60} s"

    def _botones_intervalo_caza(self, prefijo):
        opciones = [s for s in self.HUNT_INTERVAL_OPTIONS if s >= self.hunt_min_interval]
        if not opciones:
            opciones = [self.hunt_min_interval]
        fila = [
            InlineKeyboardButton(self._formatear_segundos(seg), callback_data=f"{prefijo}{seg}")
            for seg in opciones
        ]
        return [fila[:3], fila[3:]] if len(fila) > 3 else [fila]

    def _botones_control_caza(self):
        pausa = (InlineKeyboardButton("▶️ Reanudar", callback_data="hunt_resume") if self.hunt_paused
                 else InlineKeyboardButton("⏸️ Pausar", callback_data="hunt_pause"))
        return InlineKeyboardMarkup([
            [pausa, InlineKeyboardButton("⏱️ Intervalo", callback_data="hunt_interval")],
            [InlineKeyboardButton("🛑 Cancelar caza", callback_data="hunt_stop")],
        ])

    def _guardar_estado_caza(self):
        """Persiste la caza para retomarla si el contenedor se reinicia."""
        if self.hunt_task:
            self.set_config('hunt_state', 'paused' if self.hunt_paused else 'active')
            self.set_config('hunt_until', self.hunt_until.isoformat())
        else:
            self.set_config('hunt_state', '')

    def _mensaje_estado_caza(self):
        if self.hunt_task:
            estado = "⏸️ *PAUSADA*" if self.hunt_paused else "🟢 *ACTIVA*"
        else:
            estado = "⚪ Inactiva"
        mensaje = f"🎯 *CAZA DE CARGADOR LIBRE*\n\nEstado: {estado}\n"
        if self.hunt_task:
            mensaje += f"⏱️ Intervalo: {self._formatear_segundos(self.hunt_interval)} (±{self._jitter(self.hunt_interval):.0f} s)\n"
            mensaje += f"🔍 Escaneos: {self.hunt_scans}\n"
            if self.hunt_next and not self.hunt_paused:
                mensaje += f"⏭️ Próximo: {self.hunt_next.strftime('%H:%M:%S')}\n"
            mensaje += f"⌛ Se apaga sola a las {self.hunt_until.strftime('%H:%M')}\n"

        reservas = self.reservas_en_curso(self.last_conectores or [])
        if reservas:
            mensaje += "\n🟡 *Reservas ajenas en curso:*\n"
            for con in self.last_conectores:
                if con['physicalSocketId'] in reservas:
                    inicio, fiable = reservas[con['physicalSocketId']]
                    mensaje += f"  Socket {con.get('socketCode', '?')} ({con.get('socketType', '')})\n"
                    mensaje += f"    {self.formatear_marcas_reserva(inicio, fiable)}\n"
        return mensaje

    async def menu_caza(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Botón 🎯 Cazar Libre: controles si hay caza, o elegir intervalo para empezar."""
        if self.hunt_task:
            await update.message.reply_text(
                self._mensaje_estado_caza(),
                parse_mode='Markdown',
                reply_markup=self._botones_control_caza()
            )
            return

        mensaje = self._mensaje_estado_caza()
        mensaje += "\nEscanea tus cargadores y, en cuanto un conector quede libre, "
        mensaje += "lo reserva (1€, aprueba el 3DS) y te avisa.\n\n"
        mensaje += "Elige cada cuánto escanear:"
        await update.message.reply_text(
            mensaje,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(self._botones_intervalo_caza("hunt_start_"))
        )

    async def _empezar_caza(self, query, intervalo):
        await query.edit_message_text("⏳ Comprobando que no tengas ya una reserva...")

        authenticated, error = await self.ensure_authenticated()
        if not authenticated:
            await query.edit_message_text(f"❌ Error de autenticación\n\n{error}")
            return

        transaction = await asyncio.to_thread(
            self.api.get_transaction_in_progress, lat=self.latitude, lon=self.longitude
        )
        if transaction and transaction.get('reservationInProgress'):
            await query.edit_message_text("⚠️ Ya tienes una reserva activa, no hace falta cazar.")
            return

        self.hunt_interval = max(self.hunt_min_interval, intervalo)
        self.set_config('hunt_interval', self.hunt_interval)
        self._arrancar_caza(datetime.now() + timedelta(minutes=self.hunt_max_minutes), pausada=False)

        await query.edit_message_text(
            self._mensaje_estado_caza() + "\nTe aviso en cuanto reserve uno.",
            parse_mode='Markdown',
            reply_markup=self._botones_control_caza()
        )

    def _arrancar_caza(self, hasta, pausada):
        if self.hunt_task:
            self.hunt_task.cancel()
        self.hunt_paused = pausada
        self.hunt_until = hasta
        self.hunt_started = datetime.now()
        self.hunt_scans = 0
        self.hunt_failures = 0
        self.hunt_next = None
        self.hunt_task = asyncio.create_task(self._bucle_caza())
        self._guardar_estado_caza()
        self.publicar_estado()
        print(f"🎯 Caza iniciada (cada {self.hunt_interval}s, hasta {hasta.strftime('%H:%M')})")

    async def _parar_caza(self, motivo):
        task = self.hunt_task
        self.hunt_task = None
        self.hunt_paused = False
        self.hunt_next = None
        self._guardar_estado_caza()
        # Si se para desde dentro del propio bucle (ha reservado), no se cancela a
        # sí misma, simplemente termina.
        if task and task is not asyncio.current_task():
            task.cancel()
        print(f"🎯 Caza detenida: {motivo}")
        self.publicar_estado()

    async def _pausar_caza(self, motivo=None):
        self.hunt_paused = True
        self.hunt_next = None
        self._guardar_estado_caza()
        if motivo:
            print(f"🎯 Caza pausada: {motivo}")
        self.publicar_estado()

    async def _bucle_caza(self):
        espera_error = None
        try:
            while self.hunt_task:
                if datetime.now() >= self.hunt_until:
                    await self._parar_caza("tiempo máximo")
                    await self._send_notification(
                        "⌛ *Caza terminada*\n\n"
                        f"Han pasado {self.hunt_max_minutes} min sin encontrar un conector libre. "
                        "Vuelve a activarla con 🎯 Cazar Libre si sigues necesitándolo.",
                        evento=('caza', 'terminada')
                    )
                    return

                if self.hunt_paused:
                    # En pausa no se hace ninguna petición, solo se mira cada poco si
                    # se ha reanudado.
                    await asyncio.sleep(5)
                    continue

                conectores = await self._escaneo_caza()

                if conectores is None:
                    # Fallo de la API (o escaneo en curso): retroceder en vez de
                    # insistir al mismo ritmo.
                    espera_error = min(self.HUNT_MAX_BACKOFF, (espera_error or self.hunt_interval) * 2)
                    espera = espera_error
                else:
                    espera_error = None
                    libres = [c for c in conectores if c.get('status') == 'AVAILABLE']
                    if libres:
                        if await self._caza_reservar(libres):
                            return
                    espera = self._espera_con_jitter(self.hunt_interval)

                if not self.hunt_task or self.hunt_paused:
                    continue
                self.hunt_next = datetime.now() + timedelta(seconds=espera)
                await self._dormir_caza(espera)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"❌ Error en la caza: {e}")
            await self._parar_caza(f"error: {e}")
            await self._send_notification(
                f"❌ *Caza detenida por un error*\n\n{str(e)[:200]}", evento=('caza', 'fallo')
            )

    async def _escaneo_caza(self):
        """Estado de los conectores para una vuelta de la caza.

        Si el escaneo periódico acaba de pasar, se reutiliza su resultado en vez
        de pedirlo otra vez: así caza y escaneo periódico no suman peticiones.
        """
        if (self.last_conectores and self.last_scan_at
                and (datetime.now() - self.last_scan_at).total_seconds() < self.hunt_interval * 0.8):
            return self.last_conectores
        self.hunt_scans += 1
        return await self.chequear_cargadores(forzar=True)

    async def _dormir_caza(self, segundos):
        """Duerme hasta el próximo escaneo, despertando antes si se pausa o se para."""
        fin = datetime.now() + timedelta(seconds=segundos)
        while self.hunt_task and not self.hunt_paused:
            restante = (fin - datetime.now()).total_seconds()
            if restante <= 0:
                return
            await asyncio.sleep(min(restante, 5))

    async def _caza_reservar(self, libres):
        """Intenta reservar el mejor conector libre. True si la caza ha terminado."""
        preferred_type = self.tipo_preferido.lower()
        if preferred_type:
            libres.sort(key=lambda c: 0 if c.get('socketType', '').lower() == preferred_type else 1)
        con = libres[0]
        cupr_id = con['cuprId']
        socket_id = con['physicalSocketId']
        nombre = con.get('cuprName', f"Cargador {cupr_id}")

        # Puede que se haya reservado a mano (desde la app) mientras se cazaba
        transaction = await asyncio.to_thread(
            self.api.get_transaction_in_progress, lat=self.latitude, lon=self.longitude
        )
        if transaction and transaction.get('reservationInProgress'):
            await self._parar_caza("ya hay una reserva")
            await self._send_notification(
                "ℹ️ *Caza detenida*\n\nYa tienes una reserva activa. Usa 📋 Mi Reserva para verla.",
                evento=('caza', 'cancelada')
            )
            return True

        await self._send_notification(
            f"🎯 *¡Conector libre!*\n\n📍 {nombre}\n🔌 Socket {con.get('socketCode', socket_id)} "
            f"({con.get('socketType', '')})\n\n⏳ Reservando...\n📱 Aprueba el pago en tu app bancaria.",
            evento=('caza', 'libre', self._datos_conector(con))
        )

        result, error, fase = await self._proceso_reserva(cupr_id, socket_id)

        if result:
            await self._parar_caza("reservado")
            self._iniciar_auto_renovacion(cupr_id, socket_id)
            mensaje, buttons = self._mensaje_reserva_ok(
                result, cupr_id, socket_id, "🎯🎉 *¡CAZADO! Cargador RESERVADO*"
            )
            await self._send_notification(
                mensaje, reply_markup=buttons, evento=('caza', 'reservado', self._datos_conector(con))
            )
            # La caza ya no escanea (ha terminado): este escaneo enseña el conector reservado
            asyncio.create_task(self.reescanear_tras_accion())
            return True

        self.hunt_failures += 1
        if fase in ('pago', 'reserva') or self.hunt_failures >= 3:
            # Con el pago de por medio no se reintenta sola: o no estás pendiente
            # del 3DS, o algo ha ido mal tras pagar. Que decidas tú.
            await self._pausar_caza(error)
            await self._send_notification(
                f"⚠️ *No se pudo reservar*\n\n{error}\n\n"
                "Caza *pausada* para no repetir el pago. Reanúdala o cancélala:",
                reply_markup=self._botones_control_caza(),
                evento=('caza', 'fallo', self._datos_conector(con))
            )
        else:
            await self._send_notification(
                f"⚠️ No se pudo reservar: {error}\n\nSigo buscando.",
                evento=('caza', 'fallo', self._datos_conector(con))
            )
        return False

    async def _callback_caza(self, query):
        data = query.data

        if data.startswith('hunt_start_'):
            await self._empezar_caza(query, int(data.rsplit('_', 1)[1]))
            return

        if data.startswith('hunt_set_'):
            self.hunt_interval = max(self.hunt_min_interval, int(data.rsplit('_', 1)[1]))
            self.set_config('hunt_interval', self.hunt_interval)
            await query.edit_message_text(
                self._mensaje_estado_caza(),
                parse_mode='Markdown',
                reply_markup=self._botones_control_caza() if self.hunt_task else None
            )
            return

        if not self.hunt_task:
            await query.edit_message_text("ℹ️ No hay ninguna caza en marcha.")
            return

        if data == 'hunt_pause':
            await self._pausar_caza("usuario")
        elif data == 'hunt_resume':
            self.reanudar_caza()
        elif data == 'hunt_stop':
            await self._parar_caza("usuario")
            await query.edit_message_text("🛑 *Caza cancelada*", parse_mode='Markdown')
            return
        elif data == 'hunt_interval':
            await query.edit_message_text(
                self._mensaje_estado_caza() + "\nNuevo intervalo:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(self._botones_intervalo_caza("hunt_set_"))
            )
            return

        await query.edit_message_text(
            self._mensaje_estado_caza(),
            parse_mode='Markdown',
            reply_markup=self._botones_control_caza()
        )

    async def _retomar_caza(self):
        """Al arrancar, retoma una caza que estaba en marcha antes del reinicio."""
        estado = self.get_config('hunt_state')
        hasta = self._parse_fecha(self.get_config('hunt_until'))
        if estado not in ('active', 'paused') or not hasta:
            return
        if hasta <= datetime.now():
            self.set_config('hunt_state', '')
            return
        self._arrancar_caza(hasta, pausada=(estado == 'paused'))
        await self._send_notification(
            "🎯 *Caza retomada tras el reinicio*\n\n" + self._mensaje_estado_caza(),
            reply_markup=self._botones_control_caza(),
            evento=('caza', 'retomada')
        )

    def _jitter(self, intervalo):
        """Amplitud (en segundos) de la desviación aleatoria de un intervalo"""
        if self.jitter_pct <= 0 and self.jitter_min_seconds <= 0:
            return 0.0
        amplitud = max(intervalo * self.jitter_pct, self.jitter_min_seconds)
        # Nunca desviar más de la mitad del intervalo (evita esperas ~0 o dobles)
        return min(amplitud, intervalo / 2)

    def _espera_con_jitter(self, intervalo):
        """Intervalo + desviación aleatoria, en segundos (float)"""
        amplitud = self._jitter(intervalo)
        if amplitud <= 0:
            return float(intervalo)
        # Uniforme en [-amplitud, +amplitud]: la media sigue siendo el intervalo
        # y el resultado es fraccionario, así que tampoco se repite el mismo segundo.
        return max(5.0, intervalo + self._rand.uniform(-amplitud, amplitud))

    def jitter_amplitude(self):
        """Amplitud de la desviación del intervalo de escaneo periódico"""
        return self._jitter(self.check_interval)

    def proxima_espera(self):
        """Espera hasta el próximo escaneo periódico"""
        return self._espera_con_jitter(self.check_interval)

    async def run_schedule_loop(self):
        """Ejecuta el schedule de forma asíncrona"""
        # Primer chequeo tras un arranque aleatorio (no siempre a los 10s)
        await asyncio.sleep(self._rand.uniform(5, 20))
        # El primer escaneo se hace aunque el escaneo esté en pausa: es una sola petición y
        # sin él la app y el bot no tendrían ningún estado que enseñar hasta reanudarlo.
        print("🚀 Ejecutando primer chequeo...")
        await self.chequear_cargadores(forzar=True)

        amplitud = self.jitter_amplitude()
        print(f"⏰ Scheduler iniciado (cada {self.check_interval}s ±{amplitud:.0f}s)")

        # Loop infinito para chequeos periódicos
        espera = self.proxima_espera()
        self.next_scan_time = datetime.now() + timedelta(seconds=espera)
        while True:
            await asyncio.sleep(max(0.0, (self.next_scan_time - datetime.now()).total_seconds()))
            # La hora del siguiente se fija antes de escanear: el escaneo publica el
            # estado, y si no saldría como próximo el escaneo que está en curso.
            anterior = espera
            espera = self.proxima_espera()
            self.next_scan_time = datetime.now() + timedelta(seconds=espera)
            # Si la caza acaba de escanear, este escaneo sobra: los cambios ya se
            # han avisado desde allí.
            if self.last_scan_at and (datetime.now() - self.last_scan_at).total_seconds() < anterior * 0.8:
                continue
            await self.chequear_cargadores()
    
    async def _iniciar_api(self):
        """Arranca la API HTTP si hay API_TOKEN. Ver API.md."""
        token = os.getenv('API_TOKEN', '').strip()
        if not token:
            print("ℹ️ API HTTP desactivada (sin API_TOKEN)")
            return
        if len(token) < 32:
            # Un token corto se adivina. La API puede pagar reservas, no se arranca.
            print("❌ API_TOKEN demasiado corto (mínimo 32 caracteres). API HTTP desactivada")
            return
        from api_server import ServidorAPI
        self.api_server = ServidorAPI(self, token, puerto=int(os.getenv('API_PORT', '8090')))
        await self.api_server.iniciar()

    async def run(self):
        """Inicia el bot"""
        # Cargar configuración guardada
        intervalo_guardado = self.get_config('check_interval')
        if intervalo_guardado:
            self.check_interval = int(intervalo_guardado)
        if self.get_config('scanning_paused') == '1':
            self.scanning_paused = True
            print("⏸️ Escaneo automático en pausa (guardado)")
        caza_guardada = self.get_config('hunt_interval')
        if caza_guardada:
            self.hunt_interval = max(self.hunt_min_interval, int(caza_guardada))
        
        # Crear aplicación
        self.app = Application.builder().token(self.bot_token).build()
        
        # Configurar comandos del bot (opcional, para menú de comandos /)
        comandos = [
            BotCommand("start", "Mostrar menú principal")
        ]
        await self.app.bot.set_my_commands(comandos)
        
        # Registrar handlers
        self.app.add_handler(CommandHandler("start", self.comando_start))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.manejar_texto))
        self.app.add_handler(CallbackQueryHandler(self.boton_callback))
        
        # Enviar mensaje de inicio
        await self.app.initialize()
        await self.app.bot.send_message(
            chat_id=self.chat_id,
            text="✅ *Bot Monitor Iberdrola iniciado*\n\nUsa los botones del teclado para interactuar.",
            parse_mode='Markdown',
            reply_markup=self.get_main_keyboard()
        )
        
        # Iniciar scheduler como tarea asíncrona
        asyncio.create_task(self.run_schedule_loop())
        await self._retomar_caza()
        await self._iniciar_api()
        
        # Iniciar bot
        print("🤖 Bot de Telegram iniciado")
        await self.app.start()
        await self.app.updater.start_polling()
        
        # Mantener el bot corriendo
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            print("\n⚠️  Deteniendo bot...")
            await self.app.stop()
            await self.app.shutdown()


def main():
    print("="*60)
    print("🔌 BOT MONITOR CARGADORES IBERDROLA")
    print("="*60)
    
    monitor = MonitorCargadores()
    
    # Ejecutar bot
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
