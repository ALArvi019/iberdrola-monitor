# Iberdrola EV Charger Monitor Bot

Bot de Telegram para monitorizar, **reservar** y gestionar cargadores eléctricos de Iberdrola con soporte para **acceso autenticado** (favoritos, historial, reservas) y **acceso público** (estado de cualquier cargador).

## ✨ Características

### Modo Público (Sin Login)
- 🔌 Monitorización en tiempo real de cargadores
- 📊 Vista dinámica agrupada por cargador con estado de cada socket
- 🔔 Notificaciones automáticas cuando cambia el estado (incluye favoritos si autenticado)
- ⏸️ Pausar/reanudar monitorización
- ⏱️ Intervalo de escaneo configurable (30s a 10min)
- 💾 Base de datos SQLite para persistencia

### Modo Autenticado (Con Login)
- 🔐 Login con OAuth2 + PKCE + MFA por email (via Playwright + Cloudflare Turnstile)
- 🔌 Ver estado muestra automáticamente todos tus cargadores favoritos
- ⭐ Consultar tus cargadores favoritos con dirección y alias
- 🔄 Renovación automática de tokens (sin repetir MFA)
- 📱 Cambiar versión de la app Iberdrola en runtime (persiste en DB)

### 🆕 Reservas de Cargadores
- 📅 **Reservar cargador** desde Telegram
- 📋 **Ver reserva activa** con detalles completos
- ❌ **Cancelar reserva** con un toque
- 💳 **Pago con 3D Secure** (headless, aprueba en la app del banco)
- 🔄 Login automático con MFA para reservar

### 🔄 Auto-Renovación de Reservas (Nuevo!)
- ⏱️ **Renovación automática cada 14 minutos** (antes del límite gratis de 15 min)
- 📱 **Botón para poner timer** en tu móvil (13 min antes de cada renovación)
- 🔔 **Notificación en cada renovación** con hora de la próxima
- 🛑 **Se detiene automáticamente** cuando empiezas a cargar o cancelas
- ⏰ Mantén tu reserva indefinidamente hasta llegar al cargador

## 🚀 Guía de Configuración Rápida

### 1. Clonar el repositorio

```bash
git clone https://github.com/yourusername/iberdrola-monitor.git
cd iberdrola-monitor
```

### 2. Crear archivo de configuración

```bash
cp .env.example .env
```

### 3. Buscar cargadores cerca de tu ubicación

```bash
# Buscar cargadores en Madrid (coordenadas de ejemplo)
python3 find_chargers.py 40.4168 -3.7038

# O usar las coordenadas del .env
python3 find_chargers.py
```

**Salida de ejemplo:**
```
🔍 BUSCADOR DE CARGADORES IBERDROLA
======================================================================
📍 Coordenadas: 40.4168, -3.7038
📏 Radio de búsqueda: ~2.2 km

✅ Se encontraron 8 cargadores:

----------------------------------------------------------------------
ID       NOMBRE                              TIPO            DIST    
----------------------------------------------------------------------
4521     Centro Comercial ABC P-1 01         🔌 Público      0.45 km 
4522     Centro Comercial ABC P-1 02         🔌 Público      0.45 km 
3891     Parking Norte 001                   🔌 Público      1.23 km 
----------------------------------------------------------------------

📝 Para monitorizar estos cargadores, añade sus IDs a tu .env:

   CHARGER_IDS=4521,4522,3891
```

### 4. Configurar tu .env

Edita el archivo `.env` con tus datos:

```env
# Telegram (obligatorio)
TELEGRAM_BOT_TOKEN=tu_token_de_botfather
TELEGRAM_CHAT_ID=tu_chat_id

# Dispositivo
DEVICE_ID=genera-un-uuid-aqui

# Cargadores a monitorizar (obtenidos con find_chargers.py)
CHARGER_IDS=4521,4522,3891

# Tu ubicación
LATITUDE=40.4168
LONGITUDE=-3.7038

# Intervalo de escaneo (segundos)
CHECK_INTERVAL=60

# Variación aleatoria del intervalo (anti-patrón)
SCAN_JITTER_PCT=15
SCAN_JITTER_MIN=5

# Autenticación (para favoritos y reservas)
IBERDROLA_USER=tu_email@example.com
IBERDROLA_PASS=tu_contraseña

# MFA automático (lectura de código por email)
IMAP_USER=tu_email@gmail.com
IMAP_PASS=tu_app_password_de_google

# Versión de la app Iberdrola (también se puede cambiar desde el bot)
IBERDROLA_APP_VERSION=4.36.8

# Redsys (para pagos de reservas)
REDSYS_ANDROID_LICENSE=your_redsys_android_license
```

### 5. Ejecutar con Docker

```bash
docker-compose up -d
docker-compose logs -f
```

## 📁 Estructura del Proyecto

```
iberdrola-monitor/
├── bot_monitor.py          # Bot principal de Telegram (estado, reservas, versión)
├── iberdrola_api.py        # Cliente API (público + autenticado + reservas)
├── iberdrola_auth.py       # Autenticación OAuth2+PKCE+MFA con Playwright (Turnstile)
├── email_mfa_reader.py     # Lector automático de códigos MFA del email
├── redsys_payment.py       # 🆕 Procesador de pagos Redsys con 3D Secure
├── reservar_cargador.py    # 🆕 Script CLI para reservar/cancelar
├── find_chargers.py        # Buscador de cargadores por coordenadas
├── test_api.py             # Test básico de la API pública
├── test_auth_api.py        # Test completo de autenticación
├── test_reservation.py     # 🆕 Test del flujo de reservas
├── deploy.sh               # Script de despliegue
├── requirements.txt        # Dependencias Python
├── Dockerfile              # Imagen Docker (con Playwright)
├── docker-compose.yml      # Docker Compose config
├── .env.example            # Plantilla de configuración
├── AUTH_REVERSE_ENGINEERING.md  # Documentación técnica
└── data/                   # Datos persistentes
    └── monitor.db          # Base de datos SQLite (estados, tokens, configuración)
```

## 🛠️ Scripts

### `reservar_cargador.py` (🆕 Nuevo)
Script CLI para gestionar reservas de cargadores.

```bash
# Reservar (usa cargadores del .env)
python3 reservar_cargador.py

# Reservar cargador específico
python3 reservar_cargador.py 1234

# Ver estado de reservas
python3 reservar_cargador.py status

# Cancelar reserva activa
python3 reservar_cargador.py cancel
```

### `redsys_payment.py` (🆕 Nuevo)
Procesador de pagos Redsys con soporte para 3D Secure via Playwright.

**Características:**
- Generación de firma SHA256 compatible con Iberdrola
- Navegador headless para servidores sin GUI
- Detección automática de redirect de pago exitoso
- Timeout configurable para aprobación 3DS

### `email_mfa_reader.py`
Lee automáticamente los códigos MFA de Iberdrola desde tu email Gmail.

**Requisitos:**
1. Activar IMAP en Gmail: Settings > Forwarding and POP/IMAP
2. Crear App Password: https://myaccount.google.com/apppasswords

**Configuración en .env:**
```env
IMAP_USER=tu_email@gmail.com
IMAP_PASS=tu_app_password_de_google
```

Con esto configurado, el login será **100% automático** (sin intervención humana).

### `find_chargers.py`
Busca cargadores Iberdrola cerca de unas coordenadas y muestra sus IDs.

```bash
# Buscar cerca de coordenadas específicas
python3 find_chargers.py 40.4168 -3.7038

# Usar coordenadas del .env
python3 find_chargers.py

# Ampliar radio de búsqueda (~5km)
python3 find_chargers.py --radius 0.05
```

### `bot_monitor.py`
Bot principal de Telegram. Monitoriza cargadores, gestiona reservas con auto-renovación y permite cambiar la versión de la app en runtime. Si hay autenticación, tanto "Ver Estado" como el escaneo periódico usan automáticamente los cargadores favoritos; si no, usan los IDs de `CHARGER_IDS`.

### `iberdrola_api.py`
Cliente API con soporte para:
- Modo anónimo (consulta pública de cargadores)
- Modo autenticado (favoritos, historial, reservas)
- Métodos de reserva: `reserve_charger`, `cancel_reservation`, `get_user_reservation`

### `iberdrola_auth.py`
Módulo de autenticación OAuth2+PKCE+MFA. Gestiona:
- Login via Playwright headless (resuelve captcha Cloudflare Turnstile)
- Fallback a login por requests si Playwright no está disponible
- Renovación automática de tokens
- Tokens almacenados en SQLite (tabla `auth_tokens`)

### `test_auth_api.py`
Test interactivo del flujo de autenticación:
```bash
python3 test_auth_api.py
```

### `deploy.sh`
Despliega cambios al servidor de producción.

## ⚙️ Variables de Entorno

| Variable | Descripción | Requerido |
|----------|-------------|-----------|
| `TELEGRAM_BOT_TOKEN` | Token del bot (de @BotFather) | ✅ |
| `TELEGRAM_CHAT_ID` | Tu ID de chat de Telegram | ✅ |
| `DEVICE_ID` | UUID para identificar el dispositivo | ✅ |
| `CHARGER_IDS` | IDs de cargadores separados por coma | ✅ |
| `LATITUDE` | Latitud de tu ubicación | ✅ |
| `LONGITUDE` | Longitud de tu ubicación | ✅ |
| `CHECK_INTERVAL` | Intervalo de escaneo (segundos) | ❌ (60) |
| `SCAN_JITTER_PCT` | Variación aleatoria del intervalo, en % (0 = desactivado) | ❌ (15) |
| `SCAN_JITTER_MIN` | Variación mínima en segundos | ❌ (5) |
| `IBERDROLA_USER` | Email de Iberdrola (para auth) | ❌ |
| `IBERDROLA_PASS` | Contraseña de Iberdrola | ❌ |
| `IMAP_USER` | Email para leer MFA automático | ❌ |
| `IMAP_PASS` | App Password de Gmail | ❌ |
| `IBERDROLA_APP_VERSION` | Versión de la app a emular (modificable desde el bot) | ❌ (4.36.8) |
| `REDSYS_ANDROID_LICENSE` | Licencia para pagos Redsys | ❌ |

## 📱 Comandos del Bot

| Botón | Función |
|-------|---------|
| 🔌 Ver Estado | Ver estado de cargadores (favoritos si autenticado, CHARGER_IDS si no) |
| 🔄 Forzar Chequeo | Forzar escaneo inmediato |
| 📅 Reservar | Reservar cargador de favoritos |
| 📋 Mi Reserva | Ver/cancelar reserva activa |
| ⏸️ Pausar/Reanudar | Pausar o reanudar escaneo automático |
| ⏱️ Cambiar Intervalo | Cambiar intervalo de escaneo |
| ⭐ Favoritos | Ver cargadores favoritos con dirección y alias |
| 📱 Versión | Cambiar versión de la app Iberdrola en runtime |
| ℹ️ Info | Ver información del sistema |

## 📅 Flujo de Reserva

1. **Pulsa 📅 Reservar** en el bot
2. Te muestra tus cargadores favoritos disponibles
3. **Selecciona un cargador** con el botón
4. El bot procesa el pago (1€) via 3D Secure
5. **Aprueba en tu app bancaria** (notificación push)
6. ¡Reserva confirmada! Con **auto-renovación activa**

### Auto-Renovación

Tras reservar, el bot mantiene tu reserva activa indefinidamente:

- ⏱️ **Cada 14 minutos** el bot cancela y vuelve a reservar automáticamente
- 📱 Recibes **notificación con hora exacta** de la próxima renovación
- 🔔 **Botón "Poner timer 13 min"** para recordarte aprobar el 3DS
- 💳 Debes **aprobar cada pago 3DS** en tu app bancaria

**La auto-renovación se detiene cuando:**
- 🔌 Empiezas a cargar (detecta cambio de estado)
- ❌ Cancelas la reserva manualmente
- ⚠️ El socket deja de estar disponible

Para cancelar: **📋 Mi Reserva → Cancelar Reserva**

## 📊 Iconos de Estado

| Icono | Estado | Significado |
|-------|--------|-------------|
| ✅ | AVAILABLE | Cargador disponible |
| 🔴 | OCCUPIED | Cargador en uso |
| 🟡 | RESERVED | Cargador reservado |
| ⚠️ | OUT_OF_SERVICE | Fuera de servicio |
| ❓ | UNKNOWN | Estado desconocido |

## 🔐 Sistema de Autenticación (Avanzado)

Para acceder a funciones como favoritos, historial y reservas:

```bash
# Login interactivo (te pedirá el código MFA por email)
python3 test_auth_api.py
```

Ver [AUTH_REVERSE_ENGINEERING.md](AUTH_REVERSE_ENGINEERING.md) para documentación técnica completa.

## 🔧 Desarrollo Local

```bash
# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Instalar Playwright (para login con Turnstile y pagos 3DS)
playwright install chromium

# Ejecutar bot
python bot_monitor.py
```

## 🐛 Solución de Problemas

### No encuentro cargadores con find_chargers.py
- Aumenta el radio: `python3 find_chargers.py --radius 0.1`
- Verifica que las coordenadas son correctas
- Comprueba tu conexión a internet

### El bot no arranca
- Verificar que todas las variables están en `.env`
- Comprobar que el token del bot es válido
- Revisar logs: `docker-compose logs -f`

### Token de autenticación expirado
- El sistema renueva automáticamente usando refresh_token
- Si falla, el bot hace login completo automáticamente (Playwright + MFA por email)
- Como último recurso, borra la tabla `auth_tokens` de `data/monitor.db`

### Error en reserva/pago
- Verifica que tienes una tarjeta guardada en la app Iberdrola
- Asegúrate de aprobar el 3DS en tu app bancaria (2 minutos máximo)
- Revisa logs para ver el mensaje de error específico

## 🐳 Docker

El Dockerfile incluye:
- Python 3.11
- Playwright con Chromium (para login con Turnstile y pagos 3D Secure)
- Todas las dependencias de sistema para navegador headless
- Timezone configurado a Europe/Madrid

```bash
# Rebuild después de cambios
docker-compose build --no-cache

# Ver logs en tiempo real
docker-compose logs -f
```

## ⚠️ Disclaimer

Este proyecto no está afiliado con Iberdrola. Es una herramienta independiente para uso personal.

## 📄 Licencia

MIT License

---

Made with ❤️ for EV owners
