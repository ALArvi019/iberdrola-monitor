# Iberdrola EV Charger Monitor Bot

Bot de Telegram para monitorizar la disponibilidad de cargadores eléctricos de Iberdrola con soporte para **acceso autenticado** (favoritos, historial) y **acceso público** (estado de cualquier cargador).

## ✨ Características

### Modo Público (Sin Login)
- 🔌 Monitorización en tiempo real de cargadores
- 📊 Tabla ASCII visual con el estado de todos los cargadores
- 🔔 Notificaciones automáticas cuando cambia el estado
- ⏸️ Pausar/reanudar monitorización
- ⏱️ Intervalo de escaneo configurable (30s a 10min)
- 💾 Base de datos SQLite para persistencia

### Modo Autenticado (Con Login)
- 🔐 Login con OAuth2 + PKCE + MFA por email
- ⭐ Consultar tus cargadores favoritos
- 📜 Ver historial de recargas
- 🔄 Renovación automática de tokens (sin repetir MFA)

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
```

### 5. Ejecutar con Docker

```bash
docker-compose up -d
docker-compose logs -f
```

## 📁 Estructura del Proyecto

```
iberdrola-monitor/
├── bot_monitor.py          # Bot principal de Telegram
├── iberdrola_api.py        # Cliente API (público + autenticado)
├── iberdrola_auth.py       # Módulo de autenticación OAuth2+PKCE+MFA
├── find_chargers.py        # 🆕 Buscador de cargadores por coordenadas
├── test_api.py             # Test básico de la API pública
├── test_auth_api.py        # Test completo de autenticación
├── deploy.sh               # Script de despliegue
├── requirements.txt        # Dependencias Python
├── Dockerfile              # Imagen Docker
├── docker-compose.yml      # Docker Compose config
├── .env.example            # Plantilla de configuración
├── AUTH_REVERSE_ENGINEERING.md  # Documentación técnica
└── data/                   # Datos persistentes
    ├── monitor.db          # Base de datos SQLite
    └── auth_tokens.json    # Tokens de autenticación
```

## 🛠️ Scripts

### `find_chargers.py` (🆕 Nuevo)
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
Bot principal de Telegram. Lee la configuración del `.env` y monitoriza los cargadores especificados en `CHARGER_IDS`.

### `iberdrola_api.py`
Cliente API con soporte para:
- Modo anónimo (consulta pública de cargadores)
- Modo autenticado (favoritos, historial)

### `iberdrola_auth.py`
Módulo de autenticación OAuth2+PKCE+MFA. Gestiona:
- Login inicial con 2FA
- Renovación automática de tokens
- Persistencia de sesión

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
| `IBERDROLA_USER` | Email de Iberdrola (para auth) | ❌ |
| `IBERDROLA_PASS` | Contraseña de Iberdrola | ❌ |

## 📱 Comandos del Bot

| Botón | Función |
|-------|---------|
| 🔌 Ver Estado | Ver estado actual de todos los cargadores |
| 🔄 Forzar Chequeo | Forzar escaneo inmediato |
| ⏸️ Pausar/Reanudar | Pausar o reanudar escaneo automático |
| ⏱️ Cambiar Intervalo | Cambiar intervalo de escaneo |
| ℹ️ Info | Ver información del sistema |

## 📊 Iconos de Estado

| Icono | Estado | Significado |
|-------|--------|-------------|
| ✅ | AVAILABLE | Cargador disponible |
| 🔴 | OCCUPIED | Cargador en uso |
| 🟡 | RESERVED | Cargador reservado |
| ⚠️ | OUT_OF_SERVICE | Fuera de servicio |
| ❓ | UNKNOWN | Estado desconocido |

## 🔐 Sistema de Autenticación (Avanzado)

Para acceder a funciones como favoritos e historial:

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
- Si falla, elimina `data/auth_tokens.json` y haz login de nuevo

## ⚠️ Disclaimer

Este proyecto no está afiliado con Iberdrola. Es una herramienta independiente para uso personal.

## 📄 Licencia

MIT License

---

Made with ❤️ for EV owners
