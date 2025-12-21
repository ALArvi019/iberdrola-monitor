#!/usr/bin/env python3
"""
Script para hacer una reserva de cargador Iberdrola.
Incluye flujo completo con 3D Secure.

Uso:
    python3 reservar_cargador.py              # Reserva automática en IKEA Jerez
    python3 reservar_cargador.py 6103         # Reserva en cargador específico
    python3 reservar_cargador.py cancel       # Cancela reserva activa
    python3 reservar_cargador.py status       # Muestra estado de reserva
"""

import os
import sys

from iberdrola_api import IberdrolaAPI
from iberdrola_auth import IberdrolaAuth
from redsys_payment import process_reservation_payment


def get_api():
    """Obtiene una instancia autenticada de la API."""
    device_id = os.getenv('DEVICE_ID')
    lat = os.getenv('LATITUDE')
    lon = os.getenv('LONGITUDE')
    
    if not device_id:
        print("❌ DEVICE_ID no configurado en .env")
        return None, None, None, None
    
    if not lat or not lon:
        print("❌ LATITUDE/LONGITUDE no configurados en .env")
        return None, None, None, None
    
    lat = float(lat)
    lon = float(lon)
    
    auth = IberdrolaAuth()
    
    if not auth.is_token_valid():
        if auth.refresh_token:
            print("🔄 Refrescando token...")
            if not auth.refresh_access_token():
                print("❌ Error refrescando token. Ejecuta test_auth_api.py primero.")
                return None, None, None, None
        else:
            print("❌ No hay sesión guardada. Ejecuta test_auth_api.py primero.")
            return None, None, None, None
    
    api = IberdrolaAPI(device_id=device_id, auth_manager=auth)
    return api, auth, lat, lon


def get_charger_ids():
    """Obtiene los IDs de cargadores del .env."""
    charger_ids_str = os.getenv('CHARGER_IDS', '')
    if not charger_ids_str:
        print("❌ CHARGER_IDS no configurado en .env")
        return None
    return [int(x.strip()) for x in charger_ids_str.split(',')]


def find_available_socket(api, cupr_ids, lat, lon):
    """Busca un conector disponible en los cargadores especificados."""
    print(f"🔍 Buscando conector disponible en cargadores: {cupr_ids}")
    
    conectores = api.obtener_estado_conectores(cupr_ids, lat=lat, lon=lon)
    
    if not conectores:
        print("❌ Error obteniendo estado de conectores")
        return None, None, None
    
    for c in conectores:
        status = c['status']
        print(f"   {c['cuprId']} - {c['cuprName']} - Socket {c['physicalSocketId']}: {status}")
        
        if status == 'AVAILABLE':
            return c['cuprId'], c['physicalSocketId'], c['cuprName']
    
    print("❌ No hay conectores disponibles")
    return None, None, None


def reservar(cupr_ids=None):
    """Realiza una reserva de cargador."""
    api, auth, lat, lon = get_api()
    if not api:
        return False
    
    if cupr_ids is None:
        # Obtener IDs de cargadores del .env
        cupr_ids = get_charger_ids()
        if not cupr_ids:
            return False
    elif isinstance(cupr_ids, int):
        cupr_ids = [cupr_ids]
    
    print("=" * 60)
    print("🔌 RESERVA DE CARGADOR IBERDROLA")
    print("=" * 60)
    
    # Paso 1: Verificar que no hay reserva activa
    print("\n📋 Verificando reservas activas...")
    transaction = api.get_transaction_in_progress(lat=lat, lon=lon)
    
    if transaction and transaction.get('reservationInProgress'):
        print(f"⚠️ Ya tienes una reserva activa!")
        print(f"   Cargador: {transaction.get('cuprId')}")
        print(f"   Fin: {transaction.get('reservationEndDate')}")
        return False
    
    # Paso 2: Buscar conector disponible
    cupr_id, physical_socket_id, cupr_name = find_available_socket(api, cupr_ids, lat, lon)
    
    if not cupr_id:
        return False
    
    print(f"\n✅ Usaremos: {cupr_name} - Socket {physical_socket_id}")
    
    # Paso 3: Obtener método de pago
    print("\n💳 Obteniendo método de pago...")
    payment = api.get_payment_method(lat=lat, lon=lon)
    
    if not payment:
        print("❌ No hay método de pago configurado")
        return False
    
    print(f"   Tarjeta: ****{payment.get('cardNumber', '????')}")
    
    # Paso 4: Obtener orderId
    print("\n📝 Generando orden de pago...")
    order = api.get_order_id(cupr_id, physical_socket_id, amount=1.0, lat=lat, lon=lon)
    
    if not order:
        print("❌ Error obteniendo orderId")
        return False
    
    order_id = order.get('orderId')
    print(f"   Order ID: {order_id}")
    
    # Paso 5: Procesar pago con 3DS
    print("\n" + "=" * 40)
    print("💳 PROCESANDO PAGO (3D SECURE)")
    print("=" * 40)
    print("📱 Se abrirá un navegador. Aprueba el pago en tu app bancaria.")
    print("   Tienes 120 segundos para aprobar.\n")
    
    payment_success = process_reservation_payment(
        order_data=order,
        payment_token=payment['token'],
        amount_cents=100,  # 1€
        use_3ds=True,
        timeout_seconds=120
    )
    
    if not payment_success:
        print("\n❌ Error en el pago. Reserva cancelada.")
        return False
    
    # Paso 6: Ejecutar reserva
    print("\n🔌 Ejecutando reserva...")
    result = api.reserve_charger(cupr_id, physical_socket_id, order_id, lat=lat, lon=lon)
    
    if result:
        print("\n" + "=" * 60)
        print("🎉 ¡RESERVA EXITOSA!")
        print("=" * 60)
        print(f"   📍 Cargador: {cupr_name}")
        print(f"   🔌 Socket: {physical_socket_id}")
        print(f"   🆔 Reservation ID: {result.get('reservationId')}")
        print(f"   ⏰ Inicio: {result.get('startDate')}")
        print(f"   ⏰ Fin: {result.get('endDate')}")
        print(f"   💰 Precio: {result.get('reserve', {}).get('finalPrice', 'N/A')}€")
        print(f"   📊 Estado: {result.get('status', {}).get('description', 'N/A')}")
        return True
    else:
        print("\n❌ Error al crear la reserva después del pago.")
        print("   El pago se procesó pero la reserva falló.")
        return False


def cancelar():
    """Cancela la reserva activa."""
    api, auth, lat, lon = get_api()
    if not api:
        return False
    
    print("=" * 60)
    print("🔄 CANCELAR RESERVA")
    print("=" * 60)
    
    # Obtener reserva activa
    transaction = api.get_transaction_in_progress(lat=lat, lon=lon)
    
    if not transaction or not transaction.get('reservationInProgress'):
        print("ℹ️ No tienes ninguna reserva activa.")
        return True
    
    cupr_id = transaction.get('cuprId')
    physical_socket_id = transaction.get('physicalSocketId')
    
    print(f"   Cargador: {cupr_id}")
    print(f"   Socket: {physical_socket_id}")
    print(f"   Fin previsto: {transaction.get('reservationEndDate')}")
    
    # Cancelar reserva usando cuprId y physicalSocketId
    print("\n🔄 Cancelando reserva...")
    result = api.cancel_reservation(cupr_id, physical_socket_id, lat=lat, lon=lon)
    
    if result:
        print("✅ Reserva cancelada correctamente")
        return True
    else:
        # Verificar si realmente se canceló
        transaction2 = api.get_transaction_in_progress(lat=lat, lon=lon)
        if not transaction2.get('reservationInProgress'):
            print("✅ Reserva cancelada correctamente")
            return True
        else:
            print("❌ Error cancelando reserva")
            return False


def estado():
    """Muestra el estado de reservas activas."""
    api, auth, lat, lon = get_api()
    if not api:
        return
    
    print("=" * 60)
    print("📊 ESTADO DE RESERVAS")
    print("=" * 60)
    
    # Primero verificar si hay transacción en progreso
    transaction = api.get_transaction_in_progress(lat=lat, lon=lon)
    
    if not transaction:
        print("❌ Error obteniendo estado")
        return
    
    print(f"   Recarga en progreso: {'✅ Sí' if transaction.get('rechargeInProgress') else '❌ No'}")
    print(f"   Reserva en progreso: {'✅ Sí' if transaction.get('reservationInProgress') else '❌ No'}")
    
    # Si hay reserva activa, obtener detalles completos
    if transaction.get('reservationInProgress'):
        reservation = api.get_user_reservation(lat=lat, lon=lon)
        
        if reservation:
            print(f"\n   🆔 Reservation ID: {reservation.get('reservationId')}")
            print(f"   📍 Cargador: {reservation.get('chargePointInfo', {}).get('foldedTitle', 'N/A')}")
            print(f"   🔌 Socket: {reservation.get('physicalSocketId')}")
            print(f"   🔌 Tipo: {reservation.get('socketType', {}).get('socketName', 'N/A')}")
            print(f"   ⏰ Inicio: {reservation.get('startDate')}")
            print(f"   ⏰ Fin: {reservation.get('endDate')}")
            print(f"   💰 Precio: {reservation.get('reserve', {}).get('finalPrice', 'N/A')}€")
            print(f"   💸 Coste cancelación: {reservation.get('cancelationCost', 'N/A')}€")
            print(f"   📊 Estado: {reservation.get('status', {}).get('description', 'N/A')}")
        else:
            # Fallback a datos básicos
            print(f"\n   📍 Cargador: {transaction.get('cuprId')}")
            print(f"   🔌 Socket: {transaction.get('physicalSocketId')}")
            print(f"   ⏰ Fin: {transaction.get('reservationEndDate')}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        comando = sys.argv[1].lower()
        
        if comando == "cancel":
            cancelar()
        elif comando == "status":
            estado()
        elif comando.isdigit():
            # Reservar en cargador específico
            reservar([int(comando)])
        else:
            print(f"Comando desconocido: {comando}")
            print(__doc__)
    else:
        # Reserva por defecto
        reservar()
