#!/usr/bin/env python3
"""
Bot de Telegram para monitorizar cargadores Iberdrola
Escanea periódicamente y notifica cambios de estado
"""

import os
import json
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import asyncio

from iberdrola_api import IberdrolaAPI


class MonitorCargadores:
    def __init__(self):
        # Variables de entorno
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.device_id = os.getenv('DEVICE_ID', '64efc6e8-009d-4701-9039-6e793fa95d39')
        self.latitude = float(os.getenv('LATITUDE', '36.696363'))
        self.longitude = float(os.getenv('LONGITUDE', '-6.162114'))
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '60'))
        
        # IDs de los cargadores (separados por coma en .env)
        charger_ids_str = os.getenv('CHARGER_IDS', '6103,6115')
        self.cupr_ids = [int(x.strip()) for x in charger_ids_str.split(',')]
        
        # Control de escaneo
        self.scanning_paused = False
        
        # API de Iberdrola
        self.api = IberdrolaAPI(device_id=self.device_id)
        
        # Base de datos SQLite
        self.db_path = '/app/data/monitor.db'
        self.init_database()
        
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
            [KeyboardButton("⏸️ Pausar/Reanudar"), KeyboardButton("⏱️ Cambiar Intervalo")],
            [KeyboardButton("ℹ️ Info")]
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
        
        conn.commit()
        conn.close()
        print("✅ Base de datos inicializada")
    
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
    
    def formatear_tabla_ascii(self, conectores):
        """Formatea una tabla ASCII con el estado de los 4 cargadores"""
        status_emoji = {
            'AVAILABLE': '✅',
            'OCCUPIED': '��',
            'RESERVED': '🟡',
            'OUT_OF_SERVICE': '⚠️',
            'UNKNOWN': '❓'
        }
        
        # Organizar conectores por cuprName y socketCode
        tabla = {}
        for con in conectores:
            cupr_name = con.get('cuprName', '')
            socket_code = con.get('socketCode', '')
            status = con.get('status', 'UNKNOWN')
            emoji = status_emoji.get(status, '❓')
            
            # Extraer número del cargador (001, 002, etc.)
            cupr_num = cupr_name.split()[-1] if cupr_name else '???'
            # Crear clave como "001-1", "001-2", etc.
            key = f"{cupr_num}-{socket_code}"
            tabla[key] = f"{emoji} {status}"
        
        # Crear tabla ASCII
        mensaje = "```\n"
        mensaje += "┌─────────────────────┬─────────────────────┐\n"
        
        # Ajustar el padding dinámicamente
        val_001_1 = tabla.get('001-1', '❓ UNKNOWN')
        val_002_1 = tabla.get('002-1', '❓ UNKNOWN')
        val_001_2 = tabla.get('001-2', '❓ UNKNOWN')
        val_002_2 = tabla.get('002-2', '❓ UNKNOWN')
        
        mensaje += f"│  001-1: {val_001_1:<12} │  002-1: {val_002_1:<12} │\n"
        mensaje += "├─────────────────────┼─────────────────────┤\n"
        mensaje += f"│  001-2: {val_001_2:<12} │  002-2: {val_002_2:<12} │\n"
        mensaje += "└─────────────────────┴─────────────────────┘\n"
        mensaje += "```"
        
        return mensaje
    
    def formatear_mensaje_estado(self, conectores):
        """Formatea el mensaje con el estado de los conectores"""
        status_emoji = {
            'AVAILABLE': '✅',
            'OCCUPIED': '🔴',
            'RESERVED': '🟡',
            'OUT_OF_SERVICE': '⚠️',
            'UNKNOWN': '❓'
        }
        
        mensaje = "🔌 *ESTADO DE CARGADORES IKEA JEREZ*\n\n"
        mensaje += f"📍 Lat: {self.latitude}, Lon: {self.longitude}\n"
        mensaje += f"🕐 Actualizado: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        
        # Tabla ASCII
        mensaje += self.formatear_tabla_ascii(conectores)
        mensaje += "\n"
        
        # Resumen
        estados_count = {}
        for con in conectores:
            estado = con['status']
            estados_count[estado] = estados_count.get(estado, 0) + 1
        
        mensaje += "\n📊 *RESUMEN*\n"
        for estado, count in estados_count.items():
            emoji = status_emoji.get(estado, '❓')
            mensaje += f"{emoji} {estado}: {count}\n"
        
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
        mensaje += self.formatear_tabla_ascii(todos_conectores)
        
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
            # Obtener estado actual
            conectores = self.api.obtener_estado_conectores(
                self.cupr_ids,
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
        mensaje += "⏸️ *Pausar/Reanudar* - Control de escaneos\n"
        mensaje += "⏱️ *Cambiar Intervalo* - Ajustar frecuencia\n"
        mensaje += "ℹ️ *Info* - Información del sistema"
        
        await update.message.reply_text(
            mensaje, 
            parse_mode='Markdown', 
            reply_markup=self.get_main_keyboard()
        )
    
    async def manejar_texto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja los mensajes de texto del teclado"""
        texto = update.message.text
        
        if texto == "🔌 Ver Estado":
            await self.ver_estado(update, context)
        
        elif texto == "🔄 Forzar Chequeo":
            await self.forzar_chequeo(update, context)
        
        elif texto == "⏸️ Pausar/Reanudar":
            await self.toggle_pausa(update, context)
        
        elif texto == "⏱️ Cambiar Intervalo":
            await self.cambiar_intervalo(update, context)
        
        elif texto == "ℹ️ Info":
            await self.mostrar_info(update, context)
        
        else:
            await update.message.reply_text(
                "Comando no reconocido. Usa los botones del teclado.",
                reply_markup=self.get_main_keyboard()
            )
    
    async def ver_estado(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Muestra el estado de los cargadores"""
        await update.message.reply_text("⏳ Consultando estado...")
        
        conectores = self.api.obtener_estado_conectores(
            self.cupr_ids,
            self.latitude,
            self.longitude
        )
        
        if conectores:
            mensaje = self.formatear_mensaje_estado(conectores)
            await update.message.reply_text(
                mensaje, 
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "❌ Error al obtener datos",
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
        mensaje += f"💾 Base de datos: {self.db_path}\n"
        
        await update.message.reply_text(
            mensaje, 
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
