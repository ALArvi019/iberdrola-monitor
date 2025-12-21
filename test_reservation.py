#!/usr/bin/env python3
"""
Script de prueba para el flujo de reservas.
Prueba si podemos saltarnos Redsys o si es necesario.
"""

import os
import sys

from iberdrola_api import IberdrolaAPI
from iberdrola_auth import IberdrolaAuth


def get_config():
    """Obtiene configuración del .env."""
    device_id = os.getenv('DEVICE_ID')
    lat = os.getenv('LATITUDE')
    lon = os.getenv('LONGITUDE')
    charger_ids = os.getenv('CHARGER_IDS', '')
    
    if not device_id or not lat or not lon:
        print("❌ Configurar DEVICE_ID, LATITUDE, LONGITUDE en .env")
        return None
    
    return {
        'device_id': device_id,
        'lat': float(lat),
        'lon': float(lon),
        'charger_ids': [int(x.strip()) for x in charger_ids.split(',') if x.strip()]
    }


def test_reservation_flow():
    """Prueba el flujo de reserva paso a paso."""
    
    print("=" * 60)
    print("🔌 TEST DE FLUJO DE RESERVA")
    print("=" * 60)
    
    config = get_config()
    if not config:
        return
    
    device_id = config['device_id']
    lat = config['lat']
    lon = config['lon']
    charger_ids = config['charger_ids']
    
    if not charger_ids:
        print("❌ Configurar CHARGER_IDS en .env")
        return
    
    cupr_id = charger_ids[0]  # Usar el primer cargador configurado
    
    # Cargar autenticación
    auth = IberdrolaAuth()
    
    if not auth.is_token_valid() and not auth.refresh_token:
        print("❌ No hay sesión guardada. Ejecuta primero test_auth_api.py")
        return
    
    # Refrescar token si es necesario
    if not auth.is_token_valid():
        print("🔄 Refrescando token...")
        if not auth.refresh_access_token():
            print("❌ Error refrescando token")
            return
    
    api = IberdrolaAPI(device_id=device_id, auth_manager=auth)
    
    print(f"\n📍 Ubicación: {lat}, {lon}")
    print(f"🔌 Cargador de prueba: {cupr_id}")
    
    # ========== PASO 1: Obtener método de pago ==========
    print("\n" + "=" * 40)
    print("PASO 1: Obtener método de pago")
    print("=" * 40)
    
    payment = api.get_payment_method(lat=lat, lon=lon)
    
    if payment:
        print(f"✅ Método de pago encontrado:")
        print(f"   Token: {payment.get('token', 'N/A')[:20]}...")
        print(f"   Tarjeta: ****{payment.get('cardNumber', 'N/A')}")
        print(f"   Expirada: {payment.get('expired', 'N/A')}")
    else:
        print("❌ No se pudo obtener método de pago")
        return
    
    # ========== PASO 2: Obtener detalles del cargador ==========
    print("\n" + "=" * 40)
    print("PASO 2: Obtener connectores del cargador")
    print("=" * 40)
    
    detalles = api.obtener_detalles_cargador([cupr_id], lat=lat, lon=lon)
    
    if not detalles:
        print("❌ No se pudo obtener detalles del cargador")
        return
    
    # Encontrar un conector disponible
    physical_socket_id = None
    socket_info = None
    
    for cargador in detalles:
        location = cargador.get('locationData', {})
        cupr_name = location.get('cuprName', 'Sin nombre')
        print(f"\n🔌 Cargador: {cupr_name}")
        
        for cp in cargador.get('chargePoints', []):
            for socket in cp.get('physicalSocket', []):
                psid = socket.get('physicalSocketId')
                status = socket.get('status', {}).get('statusCode', 'UNKNOWN')
                socket_type = socket.get('socketType', {}).get('label', '')
                print(f"   - Socket {psid}: {status} ({socket_type})")
                
                if status == 'AVAILABLE' and not physical_socket_id:
                    physical_socket_id = psid
                    socket_info = socket
    
    if not physical_socket_id:
        print("\n⚠️ No hay conectores disponibles para reservar")
        return
    
    print(f"\n✅ Usaremos socket {physical_socket_id}")
    
    # ========== PASO 3: Verificar si hay reserva activa ==========
    print("\n" + "=" * 40)
    print("PASO 3: Verificar reservas activas")
    print("=" * 40)
    
    transaction = api.get_transaction_in_progress(lat=lat, lon=lon)
    
    if transaction:
        print(f"   Recarga en progreso: {transaction.get('rechargeInProgress', False)}")
        print(f"   Reserva en progreso: {transaction.get('reservationInProgress', False)}")
        
        if transaction.get('reservationInProgress'):
            print(f"   📍 Cargador: {transaction.get('cuprId')}")
            print(f"   🔌 Socket: {transaction.get('physicalSocketId')}")
            print(f"   ⏰ Fin: {transaction.get('reservationEndDate')}")
            print("\n⚠️ Ya tienes una reserva activa!")
            return
    else:
        print("❌ No se pudo verificar transacciones")
    
    # ========== PASO 4: Obtener orderId ==========
    print("\n" + "=" * 40)
    print("PASO 4: Obtener orderId (preautorización)")
    print("=" * 40)
    
    order_data = api.get_order_id(cupr_id, physical_socket_id, amount=1.0, lat=lat, lon=lon)
    
    if order_data:
        order_id = order_data.get('orderId')
        print(f"✅ Order ID obtenido: {order_id}")
        print(f"   Token COD: {order_data.get('tokenCod', 'N/A')[:20]}...")
        print(f"   Merchant Code: {order_data.get('merchantCode', 'N/A')}")
        print(f"   COF Transaction ID: {order_data.get('cofTxnId', 'N/A')}")
    else:
        print("❌ No se pudo obtener orderId")
        return
    
    # ========== PASO 5: Intentar reservar (SIN Redsys) ==========
    print("\n" + "=" * 40)
    print("PASO 5: Intentar reservar (saltando Redsys)")
    print("=" * 40)
    
    print(f"🔄 Intentando reservar...")
    print(f"   cuprId: {cupr_id}")
    print(f"   physicalSocketId: {physical_socket_id}")
    print(f"   orderId: {order_id}")
    
    # Pregunta antes de continuar (es dinero real)
    respuesta = input("\n⚠️ ¿Continuar con la reserva? (costará 1€) [s/N]: ")
    if respuesta.lower() != 's':
        print("❌ Reserva cancelada por el usuario")
        return
    
    result = api.reserve_charger(cupr_id, physical_socket_id, order_id, lat=lat, lon=lon)
    
    if result:
        print("\n🎉 ¡RESERVA EXITOSA!")
        print(f"   Reservation ID: {result.get('reservationId')}")
        print(f"   Inicio: {result.get('startDate')}")
        print(f"   Fin: {result.get('endDate')}")
        print(f"   Precio: {result.get('reserve', {}).get('finalPrice', 'N/A')}€")
        print(f"   Estado: {result.get('status', {}).get('description', 'N/A')}")
        
        # Guardar para cancelar después
        print(f"\n💡 Para cancelar, usa: reservation_id={result.get('reservationId')}")
    else:
        print("\n❌ La reserva falló.")
        print("   Esto puede significar que necesitamos pasar por Redsys.")
        print("   Revisa los logs para ver el código de error.")


def test_cancel_reservation(physical_socket_id, cupr_id=None):
    """Cancela una reserva activa."""
    
    print("=" * 60)
    print("🔄 CANCELAR RESERVA")
    print("=" * 60)
    
    config = get_config()
    if not config:
        return
    
    device_id = config['device_id']
    lat = config['lat']
    lon = config['lon']
    
    if cupr_id is None:
        cupr_id = config['charger_ids'][0] if config['charger_ids'] else None
        if not cupr_id:
            print("❌ Especifica cupr_id o configura CHARGER_IDS")
            return
    
    auth = IberdrolaAuth()
    
    if not auth.is_token_valid():
        if auth.refresh_token:
            auth.refresh_access_token()
        else:
            print("❌ No hay sesión")
            return
    
    api = IberdrolaAPI(device_id=device_id, auth_manager=auth)
    
    print(f"   Cargador: {cupr_id}")
    print(f"   Physical Socket ID: {physical_socket_id}")
    
    result = api.cancel_reservation(cupr_id, physical_socket_id, lat=lat, lon=lon)
    
    if result:
        print(f"\n✅ Reserva cancelada: {result}")
    else:
        print("\n❌ Error cancelando reserva")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "cancel":
        if len(sys.argv) < 3:
            print("Uso: python test_reservation.py cancel <physical_socket_id> [cupr_id]")
            sys.exit(1)
        
        physical_socket_id = int(sys.argv[2])
        cupr_id = int(sys.argv[3]) if len(sys.argv) > 3 else None
        test_cancel_reservation(physical_socket_id, cupr_id)
    else:
        test_reservation_flow()
