#!/usr/bin/env python3
"""
Script de prueba para la autenticación y API de Iberdrola.
Demuestra el flujo completo: login → MFA → obtener favoritos.
"""

import os
import sys

# Añadir el directorio actual al path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from iberdrola_auth import IberdrolaAuth
from iberdrola_api import IberdrolaAPI


def test_auth_flow():
    """Prueba el flujo de autenticación completo."""
    print("=" * 60)
    print("🔐 TEST DE AUTENTICACIÓN IBERDROLA")
    print("=" * 60)
    
    # Inicializar el gestor de autenticación
    auth = IberdrolaAuth()
    
    # Comprobar si ya tenemos tokens válidos
    if auth.is_token_valid():
        print("✅ Ya tienes una sesión válida guardada")
        print(f"   Token expira: {auth.token_expiry}")
    elif auth.refresh_token:
        print("🔄 Intentando renovar token con refresh_token...")
        if auth.refresh_access_token():
            print("✅ Token renovado correctamente")
        else:
            print("❌ No se pudo renovar. Necesitas hacer login completo.")
            return None
    else:
        print("🆕 No hay sesión guardada. Iniciando login...")
        
        # Credenciales (desde variables de entorno o input)
        username = os.getenv("IBERDROLA_USER")
        password = os.getenv("IBERDROLA_PASS")
        
        if not username:
            username = input("📧 Email de Iberdrola: ")
        if not password:
            password = input("🔑 Contraseña: ")
        
        # Iniciar login
        result = auth.start_login(username, password)
        
        if not result:
            print("❌ Error iniciando login")
            return None
        
        if result.get("status") == "mfa_required":
            print("\n📧 Se ha enviado un código a tu email.")
            print("   Revisa tu bandeja de entrada...")
            otp = input("\n🔢 Introduce el código de 6 dígitos: ")
            result = auth.submit_mfa_code(result["mfa_state"], otp)
        
        if not result or result.get("status") != "success":
            print("❌ Error en el proceso de autenticación")
            return None
    
    return auth


def test_api_with_auth(auth):
    """Prueba las funciones autenticadas de la API."""
    print("\n" + "=" * 60)
    print("📡 TEST DE API AUTENTICADA")
    print("=" * 60)
    
    # Crear API con autenticación
    device_id = os.getenv("DEVICE_ID", "test-device-12345")
    api = IberdrolaAPI(device_id=device_id, auth_manager=auth)
    
    # Coordenadas de Jerez (para contexto)
    lat, lon = 40.4168, -3.7038
    
    # Test 1: Obtener favoritos
    print("\n📋 Obteniendo favoritos...")
    favoritos = api.obtener_favoritos(lat=lat, lon=lon)
    
    if favoritos:
        print(f"   ✅ Tienes {len(favoritos)} cargadores favoritos:")
        for fav in favoritos:
            nombre = fav.get('locationData', {}).get('cuprName', 'Sin nombre')
            alias = fav.get('alias', '')
            estado = fav.get('cpStatus', {}).get('statusCode', 'UNKNOWN')
            print(f"      - {nombre} ({alias}): {estado}")
    else:
        print("   ⚠️ No se pudieron obtener favoritos")
    
    # Test 2: Obtener datos de usuario
    print("\n👤 Obteniendo datos de usuario...")
    usuario = api.obtener_datos_usuario(lat=lat, lon=lon)
    
    if usuario:
        print(f"   ✅ Datos obtenidos correctamente")
        # Mostrar algunos datos (sin exponer info sensible)
        if isinstance(usuario, dict):
            print(f"      Campos disponibles: {list(usuario.keys())[:5]}...")
    else:
        print("   ⚠️ No se pudieron obtener datos de usuario")
    
    # Test 3: Estado de un cargador público (sin auth)
    print("\n🔌 Probando consulta pública (sin auth)...")
    # IKEA Jerez
    cupr_id = 1234
    detalles = api.obtener_detalles_cargador([cupr_id], lat=lat, lon=lon)
    
    if detalles:
        print(f"   ✅ Cargador público consultado correctamente")
        for cargador in detalles:
            nombre = cargador.get('locationData', {}).get('cuprName', 'Sin nombre')
            print(f"      - {nombre}")
    else:
        print("   ⚠️ Error consultando cargador público")
    
    return api


def main():
    """Función principal."""
    # Paso 1: Autenticación
    auth = test_auth_flow()
    
    if not auth:
        print("\n❌ No se pudo completar la autenticación")
        sys.exit(1)
    
    # Paso 2: Probar API
    api = test_api_with_auth(auth)
    
    print("\n" + "=" * 60)
    print("✅ TODOS LOS TESTS COMPLETADOS")
    print("=" * 60)
    print("\nLos tokens se han guardado en: data/auth_tokens.json")
    print("El próximo login usará el refresh_token automáticamente.")


if __name__ == "__main__":
    main()
