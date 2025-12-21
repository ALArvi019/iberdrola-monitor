# Dockerfile para Bot Monitor Iberdrola con soporte de reservas
FROM python:3.11-slim

# Instalar dependencias del sistema para Playwright headless
RUN apt-get update && apt-get install -y \
    gcc \
    # Dependencias de Playwright/Chromium
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Crear directorio de la aplicación
WORKDIR /app

# Copiar requirements
COPY requirements.txt .

# Instalar dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Instalar navegador de Playwright (solo chromium para ahorrar espacio)
RUN playwright install chromium

# Copiar código de la aplicación
COPY . .

# Crear directorio para la base de datos
RUN mkdir -p /app/data

# Comando por defecto
CMD ["python", "-u", "bot_monitor.py"]
