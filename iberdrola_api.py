#!/usr/bin/env python3
"""
API de Iberdrola para obtener información de cargadores
Soporta modo anónimo (público) y autenticado (con login)
"""

import requests
import json
import secrets


class IberdrolaAPI:
    """
    Cliente API para Iberdrola Recarga Pública.
    
    Modos de uso:
    - Sin auth_manager: Solo funciones públicas (estado de cargadores)
    - Con auth_manager: Acceso a funciones de usuario (favoritos, historial, reservas)
    """
    
    def __init__(self, device_id, auth_manager=None, on_auth_failure=None):
        """
        Args:
            device_id: Identificador único del dispositivo
            auth_manager: Instancia de IberdrolaAuth para peticiones autenticadas (opcional)
            on_auth_failure: Callback cuando falla la autenticación y se necesita re-login
        """
        self.base_url = "https://eva.iberdrola.com/vecomges/api"
        self.device_id = device_id
        self.auth_manager = auth_manager
        self.on_auth_failure = on_auth_failure  # Callback para re-autenticación externa
        
        self.base_headers = {
            'Content-Type': 'application/json; charset=UTF-8',
            'Accept': 'application/json',
            'Accept-Language': 'es-ES',
            'Accept-Encoding': 'gzip',
            'versionApp': 'ANDROID-4.35.0',
            'Plataforma': 'Android',
            'societyId': '1',
            'deviceid': self.device_id,
            'deviceModel': 'samsung-o1s-SM-G991B',
            'darkMode': '0',
            'User-Agent': 'Iberdrola/4.35.0/Dalvik/2.1.0 (Linux; U; Android 13; SM-G991B Build/TP1A.220624.014)',
            'Connection': 'Keep-Alive'
        }
    
    def _get_headers(self, authenticated=False, lat=None, lon=None):
        """Genera las cabeceras para una petición."""
        headers = self.base_headers.copy()
        
        # Generar c-rid único para cada petición
        headers['c-rid'] = f"{secrets.token_hex(3)}-{secrets.token_hex(2)[:3]}-{secrets.token_hex(2)[:3]}-{secrets.token_hex(2)[:3]}-{secrets.token_hex(5)}"
        
        # Coordenadas opcionales
        if lat and lon:
            headers['numLat'] = str(lat)
            headers['numLon'] = str(lon)
        
        # Token de autenticación
        if authenticated and self.auth_manager:
            token = self.auth_manager.get_access_token()
            if token:
                headers['Authorization'] = f'Bearer {token}'
            else:
                print("⚠️ No se pudo obtener token de acceso")
        else:
            headers['Authorization'] = ''
        
        return headers
    
    def _authenticated_request(self, method, url, lat=None, lon=None, **kwargs):
        """
        Realiza una petición autenticada con manejo de errores 401.
        Si recibe 401, intenta refrescar el token y reintentar una vez.
        
        Args:
            method: 'GET' o 'POST'
            url: URL completa del endpoint
            lat, lon: Coordenadas opcionales
            **kwargs: Argumentos adicionales para requests (json, data, etc.)
        
        Returns:
            Response JSON o None si falla
        """
        if not self.auth_manager:
            print("❌ Esta función requiere autenticación")
            return None
        
        # Códigos que indican problema de autenticación
        # 401 = Unauthorized (estándar)
        # 403 = Forbidden (Iberdrola lo usa cuando no hay token)
        # 500 = Internal Server Error (Iberdrola lo usa cuando el token es inválido)
        AUTH_ERROR_CODES = (401, 403, 500)
        
        # Primer intento
        headers = self._get_headers(authenticated=True, lat=lat, lon=lon)
        
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, timeout=10, **kwargs)
            else:
                response = requests.post(url, headers=headers, timeout=10, **kwargs)
            
            # Si es error de autenticación, intentar refrescar y reintentar
            if response.status_code in AUTH_ERROR_CODES:
                print(f"⚠️ Error de autenticación ({response.status_code}). Intentando refrescar token...")
                
                # Intentar refresh token
                if self.auth_manager.refresh_token:
                    if self.auth_manager.refresh_access_token():
                        print("✅ Token refrescado correctamente")
                        # Reintentar con nuevo token
                        headers = self._get_headers(authenticated=True, lat=lat, lon=lon)
                        if method.upper() == 'GET':
                            response = requests.get(url, headers=headers, timeout=10, **kwargs)
                        else:
                            response = requests.post(url, headers=headers, timeout=10, **kwargs)
                        
                        if response.status_code in AUTH_ERROR_CODES:
                            # Aún falla, necesita login completo
                            print(f"❌ Token refrescado pero sigue fallando ({response.status_code}). Se requiere login completo.")
                            if self.on_auth_failure:
                                self.on_auth_failure()
                            return None
                    else:
                        print("❌ No se pudo refrescar el token. Se requiere login completo.")
                        if self.on_auth_failure:
                            self.on_auth_failure()
                        return None
                else:
                    print("❌ No hay refresh_token. Se requiere login completo.")
                    if self.on_auth_failure:
                        self.on_auth_failure()
                    return None
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Error en la petición: {e}")
            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code in AUTH_ERROR_CODES:
                    print(f"⚠️ Error {e.response.status_code} - Problema de autenticación")
                    if self.on_auth_failure:
                        self.on_auth_failure()
                print(f"   Respuesta: {e.response.text[:200] if e.response.text else 'vacía'}")
            return None
    
    # ==================== FUNCIONES PÚBLICAS (SIN LOGIN) ====================
    
    def obtener_detalles_cargador(self, cupr_ids, lat=None, lon=None):
        """Obtiene detalles completos de uno o varios cargadores por su cuprId"""
        if isinstance(cupr_ids, int):
            cupr_ids = [cupr_ids]
        
        payload = {"cuprId": cupr_ids}
        headers = self._get_headers(authenticated=False, lat=lat, lon=lon)
        url = f"{self.base_url}/appchargepoint/getChargePoint"
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ Error en la petición: {e}")
            return None
    
    def obtener_estado_conectores(self, cupr_ids, lat=None, lon=None):
        """Obtiene el estado detallado de todos los conectores físicos"""
        detalles = self.obtener_detalles_cargador(cupr_ids, lat, lon)
        
        if not detalles:
            return None
        
        conectores = []
        
        for cargador in detalles:
            cp_id = cargador.get('cpId')
            location = cargador.get('locationData', {})
            cupr_name = location.get('cuprName', 'Sin nombre')
            cupr_id = location.get('cuprId')
            
            for logical_socket in cargador.get('logicalSocket', []):
                logical_id = logical_socket.get('logicalSocketId')
                
                for physical_socket in logical_socket.get('physicalSocket', []):
                    conector = {
                        'cuprId': cupr_id,
                        'cuprName': cupr_name,
                        'cpId': cp_id,
                        'logicalSocketId': logical_id,
                        'physicalSocketId': physical_socket.get('physicalSocketId'),
                        'socketCode': physical_socket.get('physicalSocketCode'),
                        'socketType': physical_socket.get('socketType', {}).get('socketName', 'N/A'),
                        'maxPower': physical_socket.get('maxPower', 0),
                        'status': physical_socket.get('status', {}).get('statusCode', 'UNKNOWN'),
                        'statusUpdateDate': physical_socket.get('status', {}).get('updateDate'),
                        'price': physical_socket.get('appliedRate', {}).get('recharge', {}).get('finalPrice', 0)
                    }
                    conectores.append(conector)
        
        return conectores
    
    # ==================== FUNCIONES AUTENTICADAS (CON LOGIN) ====================
    
    def obtener_favoritos(self, lat=None, lon=None):
        """
        Obtiene la lista de cargadores favoritos del usuario.
        Requiere autenticación. Maneja errores de auth automáticamente.
        """
        url = f"{self.base_url}/appfavoritechargepoint/get-favorite-charge-points"
        return self._authenticated_request('GET', url, lat=lat, lon=lon)
    
    def obtener_historial_recargas(self, lat=None, lon=None):
        """
        Obtiene el historial de recargas del usuario.
        Requiere autenticación. Maneja errores de auth automáticamente.
        """
        url = f"{self.base_url}/appoperation/recharge/history"
        return self._authenticated_request('GET', url, lat=lat, lon=lon)
    
    def obtener_datos_usuario(self, lat=None, lon=None):
        """
        Obtiene los datos del perfil del usuario.
        Requiere autenticación. Maneja errores de auth automáticamente.
        """
        url = f"{self.base_url}/appuser/newUserData"
        return self._authenticated_request('GET', url, lat=lat, lon=lon)
    
    def is_authenticated(self):
        """Comprueba si hay una sesión autenticada válida."""
        if not self.auth_manager:
            return False
        return self.auth_manager.is_token_valid() or bool(self.auth_manager.refresh_token)
