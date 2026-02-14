#!/usr/bin/env python3
"""
Bot de Telegram para monitorizar cargadores Iberdrola
Escanea periódicamente y notifica cambios de estado
"""

import os
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import asyncio

from iberdrola_api import IberdrolaAPI
from iberdrola_auth import IberdrolaAuth


class MonitorCargadores:
    def __init__(self):
        # Variables de entorno
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.device_id = os.getenv('DEVICE_ID', 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx')
        self.latitude = float(os.getenv('LATITUDE', '40.4155'))
        self.longitude = float(os.getenv('LONGITUDE', '-3.7074'))
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '60'))
        
        # IDs de los cargadores (separados por coma en .env)
        charger_ids_str = os.getenv('CHARGER_IDS', '1234,5678')
        self.cupr_ids = [int(x.strip()) for x in charger_ids_str.split(',')]
        
        # Control de escaneo
        self.scanning_paused = False
        
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
    
    def guardar_estado(self, conectores):
        """Guarda el estado actual en la base de datos"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for con in conectores:
            cursor.execute('''
                INSERT OR REPLACE INTO estado_conectores 
                (physicalSocketId, cuprId, cuprName, cpId, socketType, status, lastUpdate, lastCheck)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                con['physicalSocketId'],
                con['cuprId'],
                con['cuprName'],
                con['cpId'],
                con['socketType'],
                con['status'],
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
    
    def formatear_mensaje_estado(self, conectores):
        """Formatea el mensaje con el estado de los conectores, agrupados por cargador"""
        status_emoji = {
            'AVAILABLE': '✅',
            'OCCUPIED': '🔴',
            'RESERVED': '🟡',
            'OUT_OF_SERVICE': '⚠️',
            'UNKNOWN': '❓'
        }

        mensaje = f"🔌 *ESTADO DE CARGADORES*\n"
        mensaje += f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"

        # Agrupar conectores por cargador (cuprName)
        cargadores = {}
        for con in conectores:
            nombre = con.get('cuprName', 'Sin nombre')
            cargadores.setdefault(nombre, []).append(con)

        estados_count = {}
        for nombre, sockets in cargadores.items():
            mensaje += f"📍 *{nombre}*\n"
            for con in sockets:
                status = con.get('status', 'UNKNOWN')
                emoji = status_emoji.get(status, '❓')
                socket_code = con.get('socketCode', '?')
                socket_type = con.get('socketType', '')
                mensaje += f"  {emoji} Socket {socket_code} ({socket_type}) — `{status}`\n"
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
        status_emoji = {
            'AVAILABLE': '✅',
            'OCCUPIED': '🔴',
            'RESERVED': '🟡',
            'OUT_OF_SERVICE': '⚠️',
            'UNKNOWN': '❓'
        }
        
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
            
            mensaje += f"🏪 *{con['cuprName']}*\n"
            mensaje += f"🔌 Socket {display_name} ({con['socketType']})\n"
            mensaje += f"{emoji_ant} ~~{ant}~~ → {emoji_nue} *{nue}*\n\n"
        
        # Mostrar estado de todos los cargadores
        mensaje += "─" * 30 + "\n"
        mensaje += "*ESTADO ACTUAL DE TODOS:*\n\n"
        mensaje += self.formatear_mensaje_estado(todos_conectores)
        
        return mensaje
    
    async def enviar_mensaje(self, mensaje, parse_mode='Markdown'):
        """Envía un mensaje a Telegram"""
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=mensaje,
                parse_mode=parse_mode
            )
            print(f"✅ Mensaje enviado a Telegram")
        except Exception as e:
            print(f"❌ Error al enviar mensaje: {e}")
    
    async def chequear_cargadores(self):
        """Chequea el estado de los cargadores y notifica cambios"""
        if self.scanning_paused:
            return

        try:
            # Usar favoritos si hay autenticación, sino CHARGER_IDS
            cupr_ids = list(self.cupr_ids)
            authenticated, _ = await self.ensure_authenticated()
            if authenticated:
                favoritos = self.api.obtener_favoritos(lat=self.latitude, lon=self.longitude)
                if favoritos:
                    fav_ids = []
                    for fav in favoritos:
                        cupr_id = fav.get('locationData', {}).get('cuprId')
                        if cupr_id and cupr_id not in fav_ids:
                            fav_ids.append(cupr_id)
                    if fav_ids:
                        cupr_ids = fav_ids

            # Obtener estado actual
            conectores = self.api.obtener_estado_conectores(
                cupr_ids,
                self.latitude,
                self.longitude
            )
            
            if not conectores:
                print("❌ No se pudieron obtener datos")
                return
            
            # Detectar cambios
            cambios = self.detectar_cambios(conectores)
            
            if cambios:
                mensaje = self.formatear_mensaje_cambio(cambios, conectores)
                await self.enviar_mensaje(mensaje)
            
            # Guardar nuevo estado
            self.guardar_estado(conectores)
            
        except Exception as e:
            print(f"❌ Error al chequear: {e}")
    
    async def comando_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start"""
        mensaje = "🤖 *Bot Monitor Iberdrola*\n\n"
        mensaje += "Usa los botones del teclado para interactuar:\n\n"
        mensaje += "🔌 *Ver Estado* - Estado actual\n"
        mensaje += "🔄 *Forzar Chequeo* - Escanear ahora\n"
        mensaje += "📅 *Reservar* - Reservar cargador\n"
        mensaje += "📋 *Mi Reserva* - Ver/cancelar reserva\n"
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

        cupr_ids = list(self.cupr_ids)

        # Intentar obtener IDs de favoritos si hay autenticación
        authenticated, _ = await self.ensure_authenticated()
        if authenticated:
            favoritos = self.api.obtener_favoritos(lat=self.latitude, lon=self.longitude)
            if favoritos:
                fav_ids = []
                for fav in favoritos:
                    cupr_id = fav.get('locationData', {}).get('cuprId')
                    if cupr_id and cupr_id not in fav_ids:
                        fav_ids.append(cupr_id)
                if fav_ids:
                    cupr_ids = fav_ids

        conectores = self.api.obtener_estado_conectores(
            cupr_ids, self.latitude, self.longitude
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
        status_emoji = {
            'AVAILABLE': '✅',
            'OCCUPIED': '🔴',
            'RESERVED': '🟡',
            'OUT_OF_SERVICE': '⚠️',
            'UNKNOWN': '❓'
        }
        
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
        self.scanning_paused = not self.scanning_paused
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
        mensaje += f"Intervalo actual: *{self.check_interval}s* ({self.check_interval//60} min)\n\n"
        mensaje += "Selecciona el nuevo intervalo:"
        
        await update.message.reply_text(mensaje, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def mostrar_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra información del sistema"""
        mensaje = "ℹ️ *Información del Sistema*\n\n"
        mensaje += f"📍 Ubicación: {self.latitude}, {self.longitude}\n"
        mensaje += f"🔌 Cargadores: {', '.join(map(str, self.cupr_ids))}\n"
        mensaje += f"⏱️ Intervalo: {self.check_interval}s ({self.check_interval//60} min)\n"
        mensaje += f"🔄 Estado: {'⏸️ PAUSADO' if self.scanning_paused else '▶️ ACTIVO'}\n"
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
            await update.message.reply_text(
                "❌ *No hay cargadores disponibles*\n\nTodos tus favoritos están ocupados o fuera de servicio.",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
            return
        
        # Obtener estado de conectores de los cargadores disponibles
        conectores = self.api.obtener_estado_conectores(cupr_ids_disponibles, lat=self.latitude, lon=self.longitude)
        
        if conectores:
            for c in conectores:
                if c.get('status') == 'AVAILABLE':
                    cupr_id = c.get('cuprId')
                    socket_id = c.get('physicalSocketId')
                    nombre = c.get('cuprName', 'Sin nombre')
                    socket_type = c.get('socketType', '')
                    
                    mensaje += f"✅ *{nombre}*\n   Socket {socket_id} ({socket_type})\n\n"
                    buttons.append([
                        InlineKeyboardButton(
                            f"🔌 {nombre[:25]}",
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
    
    async def _ejecutar_reserva(self, query, cupr_id: int, socket_id: int, is_renewal: bool = False):
        """Ejecuta la reserva de un cargador"""
        if not is_renewal:
            await query.edit_message_text("⏳ Procesando reserva...")
        
        try:
            from redsys_payment import process_reservation_payment
            
            # 1. Obtener método de pago
            payment = self.api.get_payment_method(lat=self.latitude, lon=self.longitude)
            if not payment:
                if not is_renewal:
                    await query.edit_message_text("❌ No hay método de pago configurado en la app Iberdrola.")
                return False
            
            # 2. Obtener orderId
            order = self.api.get_order_id(cupr_id, socket_id, amount=1.0, lat=self.latitude, lon=self.longitude)
            if not order:
                if not is_renewal:
                    await query.edit_message_text("❌ Error al generar orden de pago.")
                return False
            
            order_id = order.get('orderId')
            if not is_renewal:
                await query.edit_message_text(f"💳 Procesando pago (Order: {order_id})...\n\n📱 Aprueba el pago en tu app bancaria si es necesario.")
            
            # 3. Procesar pago (ejecutar en thread para no bloquear)
            loop = asyncio.get_event_loop()
            payment_success = await loop.run_in_executor(
                None,
                lambda: process_reservation_payment(
                    order_data=order,
                    payment_token=payment['token'],
                    amount_cents=100,
                    use_3ds=True,
                    timeout_seconds=120
                )
            )
            
            if not payment_success:
                if not is_renewal:
                    await query.edit_message_text("❌ Error en el pago. La transacción no se completó.")
                return False
            
            # 4. Ejecutar reserva
            result = self.api.reserve_charger(cupr_id, socket_id, order_id, lat=self.latitude, lon=self.longitude)
            
            if result:
                nombre = result.get('chargePointInfo', {}).get('foldedTitle', 'Cargador')
                if not nombre or nombre == 'Cargador':
                    nombre = f"Cargador {cupr_id}"
                
                end_date = result.get('endDate', 'N/A')
                try:
                    end_dt = datetime.fromisoformat(end_date.replace('+00:00', '+00:00'))
                    end_str = end_dt.strftime('%H:%M')
                except:
                    end_str = end_date
                
                # Iniciar auto-renovación si no es ya una renovación
                if not is_renewal:
                    self.auto_renew_active = True
                    self.auto_renew_cupr_id = cupr_id
                    self.auto_renew_socket_id = socket_id
                    
                    # Calcular próxima renovación
                    self.auto_renew_next_time = datetime.now() + timedelta(minutes=self.RENEW_INTERVAL_MINUTES)
                    next_renew_str = self.auto_renew_next_time.strftime('%H:%M')
                    
                    # Iniciar tarea de auto-renovación
                    if self.auto_renew_task:
                        self.auto_renew_task.cancel()
                    self.auto_renew_task = asyncio.create_task(self._auto_renew_loop())
                    
                    mensaje = "🎉 *¡RESERVA EXITOSA!*\n\n"
                    mensaje += f"📍 {nombre}\n"
                    mensaje += f"🔌 Socket: {socket_id}\n"
                    mensaje += f"⏰ Válida hasta: {end_str}\n"
                    mensaje += f"💰 Precio: 1€\n\n"
                    mensaje += "🔄 *Auto-renovación ACTIVA*\n"
                    mensaje += f"⏱️ Próxima renovación: *{next_renew_str}*\n"
                    mensaje += f"(cada {self.RENEW_INTERVAL_MINUTES} min)\n\n"
                    mensaje += "📱 Prepara tu app bancaria para aprobar el 3DS."
                    
                    # Botón para activar timer de 13 minutos
                    timer_minutes = self.RENEW_INTERVAL_MINUTES - 1  # 1 min antes para prepararse
                    timer_url = f"https://www.google.com/search?q=set+timer+{timer_minutes}+minutes"
                    buttons = [[InlineKeyboardButton(f"⏱️ Poner timer {timer_minutes} min", url=timer_url)]]
                    
                    await query.edit_message_text(
                        mensaje, 
                        parse_mode='Markdown',
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                
                return True
            else:
                if not is_renewal:
                    await query.edit_message_text("❌ Error al crear la reserva. El pago se procesó pero la reserva falló.")
                return False
                
        except Exception as e:
            if not is_renewal:
                await query.edit_message_text(f"❌ Error durante la reserva: {str(e)[:100]}")
            return False
    
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
        
        # Cancelar
        result = self.api.cancel_reservation(cupr_id, socket_id, lat=self.latitude, lon=self.longitude)
        
        if result:
            await query.edit_message_text("✅ *Reserva cancelada correctamente*\n\n🔄 Auto-renovación detenida.", parse_mode='Markdown')
        else:
            # Verificar si realmente se canceló
            transaction2 = self.api.get_transaction_in_progress(lat=self.latitude, lon=self.longitude)
            if not transaction2.get('reservationInProgress'):
                await query.edit_message_text("✅ *Reserva cancelada correctamente*\n\n🔄 Auto-renovación detenida.", parse_mode='Markdown')
            else:
                await query.edit_message_text("❌ Error al cancelar la reserva. Inténtalo de nuevo.")
    
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
        
        while self.auto_renew_active:
            # Esperar el intervalo de renovación
            await asyncio.sleep(self.RENEW_INTERVAL_MINUTES * 60)
            
            if not self.auto_renew_active:
                break
            
            print(f"🔄 Iniciando renovación automática...")
            
            # Verificar si aún hay reserva activa (podría haber empezado a cargar)
            transaction = self.api.get_transaction_in_progress(lat=self.latitude, lon=self.longitude)
            
            if transaction and transaction.get('chargeInProgress'):
                # Ya está cargando, detener auto-renovación
                print("🔌 Carga en progreso detectada. Deteniendo auto-renovación.")
                self.auto_renew_active = False
                await self._send_notification(
                    "🔌 *Carga iniciada*\n\n"
                    "Auto-renovación detenida porque el vehículo está cargando."
                )
                break
            
            if not transaction or not transaction.get('reservationInProgress'):
                # No hay reserva activa (expiró o se canceló externamente)
                print("⚠️ No hay reserva activa. Deteniendo auto-renovación.")
                self.auto_renew_active = False
                await self._send_notification(
                    "⚠️ *Reserva expirada*\n\n"
                    "La reserva ya no está activa. Auto-renovación detenida."
                )
                break
            
            # Cancelar reserva actual
            cupr_id = self.auto_renew_cupr_id
            socket_id = self.auto_renew_socket_id
            
            print(f"   Cancelando reserva actual (cupr:{cupr_id}, socket:{socket_id})...")
            self.api.cancel_reservation(cupr_id, socket_id, lat=self.latitude, lon=self.longitude)
            
            # Esperar un momento para que se procese
            await asyncio.sleep(2)
            
            # Verificar que el socket sigue disponible
            conectores = self.api.obtener_estado_conectores([cupr_id], lat=self.latitude, lon=self.longitude)
            socket_available = False
            for c in conectores or []:
                if c.get('physicalSocketId') == socket_id and c.get('status') == 'AVAILABLE':
                    socket_available = True
                    break
            
            if not socket_available:
                print("❌ Socket ya no disponible. Deteniendo auto-renovación.")
                self.auto_renew_active = False
                await self._send_notification(
                    "❌ *Socket no disponible*\n\n"
                    "El cargador ya no está disponible. Auto-renovación detenida."
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
                    reply_markup=timer_buttons
                )
            else:
                print("   ❌ Error al renovar reserva")
                self.auto_renew_active = False
                await self._send_notification(
                    "❌ *Error al renovar reserva*\n\n"
                    "No se pudo renovar. Auto-renovación detenida."
                )
                break
        
        print("🔄 Auto-renovación finalizada")
    
    async def _ejecutar_reserva_silenciosa(self, cupr_id: int, socket_id: int):
        """Ejecuta una reserva sin interacción de usuario (para renovaciones)"""
        try:
            from redsys_payment import process_reservation_payment
            
            payment = self.api.get_payment_method(lat=self.latitude, lon=self.longitude)
            if not payment:
                return False
            
            order = self.api.get_order_id(cupr_id, socket_id, amount=1.0, lat=self.latitude, lon=self.longitude)
            if not order:
                return False
            
            order_id = order.get('orderId')
            
            loop = asyncio.get_event_loop()
            payment_success = await loop.run_in_executor(
                None,
                lambda: process_reservation_payment(
                    order_data=order,
                    payment_token=payment['token'],
                    amount_cents=100,
                    use_3ds=True,
                    timeout_seconds=120
                )
            )
            
            if not payment_success:
                return False
            
            result = self.api.reserve_charger(cupr_id, socket_id, order_id, lat=self.latitude, lon=self.longitude)
            return result is not None
            
        except Exception as e:
            print(f"❌ Error en reserva silenciosa: {e}")
            return False
    
    async def _send_notification(self, message: str, reply_markup=None):
        """Envía una notificación al usuario"""
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup or self.get_main_keyboard()
            )
        except Exception as e:
            print(f"❌ Error enviando notificación: {e}")

    async def run_schedule_loop(self):
        """Ejecuta el schedule de forma asíncrona"""
        # Primer chequeo después de 10 segundos
        await asyncio.sleep(10)
        print("🚀 Ejecutando primer chequeo...")
        await self.chequear_cargadores()
        
        print(f"⏰ Scheduler iniciado (cada {self.check_interval//60} minutos)")
        
        # Loop infinito para chequeos periódicos
        while True:
            await asyncio.sleep(self.check_interval)
            await self.chequear_cargadores()
    
    async def run(self):
        """Inicia el bot"""
        # Cargar configuración guardada
        intervalo_guardado = self.get_config('check_interval')
        if intervalo_guardado:
            self.check_interval = int(intervalo_guardado)
        
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
