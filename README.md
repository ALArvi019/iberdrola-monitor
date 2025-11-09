# Iberdrola EV Charger Monitor Bot

A Telegram bot that monitors the availability of Iberdrola electric vehicle charging stations and sends notifications when their status changes.

## Features

- 🔌 Real-time monitoring of EV charging stations
- 📊 Visual ASCII table showing all chargers' status
- 🔔 Automatic notifications when charger status changes
- ⏸️ Pause/resume monitoring functionality
- ⏱️ Configurable scan intervals (30s to 10min)
- 💾 SQLite database for state persistence
- 🐳 Fully containerized with Docker

## Status Icons

- ✅ `AVAILABLE` - Charger is available
- 🔴 `OCCUPIED` - Charger is in use
- 🟡 `RESERVED` - Charger is reserved
- ⚠️ `OUT_OF_SERVICE` - Charger is out of service
- ❓ `UNKNOWN` - Status unknown

## Prerequisites

- Docker and Docker Compose
- A Telegram Bot Token (get it from [@BotFather](https://t.me/botfather))
- Your Telegram Chat ID (get it from [@userinfobot](https://t.me/userinfobot))
- Iberdrola Device ID (optional, default provided)

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/iberdrola-monitor.git
cd iberdrola-monitor
```

### 2. Create your environment file

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
TELEGRAM_CHAT_ID=your_chat_id
DEVICE_ID=your_device_id  # Optional
LATITUDE=36.696363        # Your location latitude
LONGITUDE=-6.162114       # Your location longitude
CHECK_INTERVAL=60         # Scan interval in seconds
```

### 3. Run with Docker Compose

```bash
docker-compose up -d
```

### 4. Check logs

```bash
docker-compose logs -f
```

## Configuration

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token | - | ✅ |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID | - | ✅ |
| `DEVICE_ID` | Iberdrola API device ID | Auto-generated | ❌ |
| `LATITUDE` | Location latitude for chargers | - | ✅ |
| `LONGITUDE` | Location longitude for chargers | - | ✅ |
| `CHECK_INTERVAL` | Scan interval in seconds | 60 | ❌ |

### Finding Charger IDs

The charger IDs (`cupr_ids`) are hardcoded in `bot_monitor.py`. To monitor different chargers:

1. Find the charger IDs using the Iberdrola API
2. Edit `bot_monitor.py` and update the `self.cupr_ids` list:

```python
self.cupr_ids = [6103, 6115]  # Replace with your charger IDs
```

## Bot Commands

The bot provides a persistent keyboard with the following options:

- **🔌 Ver Estado** - View current status of all chargers
- **🔄 Forzar Chequeo** - Force an immediate scan
- **⏸️ Pausar/Reanudar** - Pause/resume automatic scanning
- **⏱️ Cambiar Intervalo** - Change scan interval
- **ℹ️ Info** - View system information

You can also use the `/start` command to display the menu.

## Project Structure

```
iberdrola-monitor/
├── bot_monitor.py          # Main bot logic
├── iberdrola_api.py        # Iberdrola API wrapper
├── requirements.txt        # Python dependencies
├── Dockerfile             # Docker image definition
├── docker-compose.yml     # Docker Compose configuration
├── .env.example          # Environment variables template
├── .gitignore           # Git ignore rules
└── README.md           # This file
```

## How It Works

1. **Monitoring**: The bot scans the Iberdrola API at regular intervals
2. **State Detection**: Compares current status with previous state stored in SQLite
3. **Notifications**: Sends a Telegram message when any charger changes status
4. **Display**: Shows all chargers in an ASCII table with current states

### Example Notification

```
🔔 CAMBIO DE ESTADO DETECTADO!

🕐 09/11/2025 11:30:00

🏪 IKEA Jerez P-0 001
🔌 Socket 001-1 (Tipo2-cable)
🔴 OCCUPIED → ✅ AVAILABLE

──────────────────────────────
ESTADO ACTUAL DE TODOS:

┌─────────────────────┬─────────────────────┐
│  001-1: ✅ AVAILABLE │  002-1: 🔴 OCCUPIED │
├─────────────────────┼─────────────────────┤
│  001-2: 🔴 OCCUPIED │  002-2: 🟡 RESERVED │
└─────────────────────┴─────────────────────┘
```

## Development

### Running locally without Docker

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
# ... other variables

# Run the bot
python bot_monitor.py
```

## Troubleshooting

### Bot doesn't start

- Check if all required environment variables are set
- Verify your Telegram bot token is valid
- Ensure the chat ID is correct

### No notifications received

- Check if the bot is running: `docker-compose ps`
- View logs: `docker-compose logs -f`
- Verify the charger IDs are correct
- Check if monitoring is paused (use ⏸️ button to resume)

### Database issues

The database is stored in `./data/monitor.db`. To reset:

```bash
docker-compose down
rm -rf data/
docker-compose up -d
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is open source and available under the [MIT License](LICENSE).

## Disclaimer

This project is not affiliated with, endorsed by, or connected to Iberdrola in any way. It's an independent monitoring tool created for personal use.

## Support

If you encounter any issues or have questions, please open an issue on GitHub.

---

Made with ❤️ for EV owners
