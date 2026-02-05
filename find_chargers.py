#!/usr/bin/env python3
"""
Script para buscar cargadores Iberdrola cerca de unas coordenadas.
Útil para obtener los IDs de cargadores (cuprId) que luego puedes
añadir a tu configuración en .env

Uso:
    python3 find_chargers.py                    # Usa coords del .env
    python3 find_chargers.py 40.4168 -3.7038    # Coordenadas manuales (Madrid)
    python3 find_chargers.py --radius 0.05      # Cambiar radio de búsqueda
"""

import os
import sys
import requests
import secrets
from math import radians, sin, cos, sqrt, atan2

APP_VERSION = os.environ.get('IBERDROLA_APP_VERSION', '4.36.7')


def haversine_distance(lat1, lon1, lat2, lon2):
    """Calcula la distancia en km entre dos puntos geográficos."""
    R = 6371  # Radio de la Tierra en km
    
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    
    return R * c


def get_headers(device_id):
    """Genera las cabeceras para la petición."""
    return {
        'Content-Type': 'application/json; charset=UTF-8',
        'Accept': 'application/json',
        'Accept-Language': 'es-ES',
        'Accept-Encoding': 'gzip',
        'Authorization': '',
        'versionApp': f'ANDROID-{APP_VERSION}',
        'Plataforma': 'Android',
        'societyId': '1',
        'deviceid': device_id,
        'deviceModel': 'samsung-o1s-SM-G991B',
        'c-rid': f"{secrets.token_hex(3)}-{secrets.token_hex(2)[:3]}-{secrets.token_hex(2)[:3]}-{secrets.token_hex(2)[:3]}-{secrets.token_hex(5)}",
        'darkMode': '0',
        'User-Agent': f'Iberdrola/{APP_VERSION}/Dalvik/2.1.0 (Linux; U; Android 13; SM-G991B Build/TP1A.220624.014)',
        'Connection': 'Keep-Alive'
    }


def list_chargers(lat, lon, radius=0.02, device_id=None):
    """
    Lista cargadores en un área rectangular alrededor de las coordenadas.
    
    Args:
        lat: Latitud central
        lon: Longitud central
        radius: Radio en grados (aproximadamente 0.01 = 1km)
        device_id: ID de dispositivo para la API
    """
    if not device_id:
        import uuid
        device_id = os.getenv('DEVICE_ID', str(uuid.uuid4()))
    
    url = "https://eva.iberdrola.com/vecomges/api/appchargepoint/listChargePoints"
    
    headers = get_headers(device_id)
    headers['numLat'] = str(lat)
    headers['numLon'] = str(lon)
    
    # Payload para buscar cargadores en el área
    payload = {
        "advantageous": False,
        "chargePointTypesCodes": [],
        "connectorsType": [],
        "favoriteInd": None,
        "loadSpeed": [],
        "socketStatus": [],
        "latitudeMin": lat - radius,
        "latitudeMax": lat + radius,
        "longitudeMin": lon - radius,
        "longitudeMax": lon + radius,
        "parkingRestrictionsList": [],
        "tagIds": [],
        "chargerOperator": [],
        "sites": []
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ Error en la petición: {e}")
        return None


def format_charger_info(charger, center_lat, center_lon):
    """Formatea la información de un cargador para mostrar."""
    location = charger.get('locationData', {})
    cupr_id = location.get('cuprId')
    name = location.get('cuprName', 'Sin nombre')
    lat = location.get('latitude', 0)
    lon = location.get('longitude', 0)
    cp_type = location.get('chargePointTypeCode', '?')
    
    # Calcular distancia
    if lat and lon:
        distance = haversine_distance(center_lat, center_lon, lat, lon)
    else:
        distance = 999
    
    # Tipo de cargador
    type_names = {
        'P': '🔌 Público',
        'S': '🏢 Semi-público',
        'R': '🏠 Residencial'
    }
    type_str = type_names.get(cp_type, f'❓ {cp_type}')
    
    # Estado
    status = charger.get('cpStatus', {}).get('statusCode', 'UNKNOWN')
    
    return {
        'cupr_id': cupr_id,
        'name': name,
        'type': type_str,
        'status': status,
        'distance_km': distance,
        'lat': lat,
        'lon': lon
    }


def main():
    # Parsear argumentos
    radius = 0.02  # ~2km por defecto
    lat = None
    lon = None
    
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--radius' and i + 1 < len(args):
            radius = float(args[i + 1])
            i += 2
        elif args[i] == '--help' or args[i] == '-h':
            print(__doc__)
            sys.exit(0)
        elif lat is None:
            lat = float(args[i])
            i += 1
        elif lon is None:
            lon = float(args[i])
            i += 1
        else:
            i += 1
    
    # Si no se pasaron coordenadas, usar las del .env
    if lat is None:
        lat = float(os.getenv('LATITUDE', '40.4168'))  # Default: Madrid
    if lon is None:
        lon = float(os.getenv('LONGITUDE', '-3.7038'))  # Default: Madrid
    
    device_id = os.getenv('DEVICE_ID')
    
    print("=" * 70)
    print("🔍 BUSCADOR DE CARGADORES IBERDROLA")
    print("=" * 70)
    print(f"📍 Coordenadas: {lat}, {lon}")
    print(f"📏 Radio de búsqueda: ~{radius * 111:.1f} km")
    print()
    
    # Buscar cargadores
    print("⏳ Buscando cargadores...")
    chargers = list_chargers(lat, lon, radius, device_id)
    
    if not chargers:
        print("❌ No se encontraron cargadores o hubo un error.")
        print("\n💡 Prueba a aumentar el radio con: --radius 0.05")
        sys.exit(1)
    
    # Formatear y ordenar por distancia (filtrar los que no tienen cuprId)
    formatted = [format_charger_info(c, lat, lon) for c in chargers]
    formatted = [c for c in formatted if c['cupr_id'] is not None]
    formatted.sort(key=lambda x: x['distance_km'])
    
    print(f"✅ Se encontraron {len(formatted)} cargadores:\n")
    print("-" * 70)
    print(f"{'ID':<8} {'NOMBRE':<35} {'TIPO':<15} {'DIST':<8}")
    print("-" * 70)
    
    for c in formatted:
        dist_str = f"{c['distance_km']:.2f} km"
        name = c['name'][:33] + '..' if len(c['name']) > 35 else c['name']
        cupr_id = str(c['cupr_id']) if c['cupr_id'] else '?'
        print(f"{cupr_id:<8} {name:<35} {c['type']:<15} {dist_str:<8}")
    
    print("-" * 70)
    print()
    print("📝 Para monitorizar estos cargadores, añade sus IDs a tu .env:")
    print()
    
    # Generar línea de configuración
    ids = [str(c['cupr_id']) for c in formatted[:5] if c['cupr_id']]  # Top 5 más cercanos
    print(f"   CHARGER_IDS={','.join(ids)}")
    print()
    print("💡 Puedes copiar cualquier ID de la lista y añadirlo a CHARGER_IDS")


if __name__ == "__main__":
    main()
