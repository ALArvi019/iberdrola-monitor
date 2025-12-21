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
    
    def __init__(self, device_id, auth_manager=None):
        """
        Args:
            device_id: Identificador único del dispositivo
            auth_manager: Instancia de IberdrolaAuth para peticiones autenticadas (opcional)
        """
        self.base_url = "https://eva.iberdrola.com/vecomges/api"
        self.device_id = device_id
        self.auth_manager = auth_manager
        
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
        Requiere autenticación.
        """
        if not self.auth_manager:
            print("❌ Esta función requiere autenticación")
            return None
        
        headers = self._get_headers(authenticated=True, lat=lat, lon=lon)
        url = f"{self.base_url}/appfavoritechargepoint/get-favorite-charge-points"
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ Error obteniendo favoritos: {e}")
            if hasattr(e, 'response') and e.response:
                print(f"   Respuesta: {e.response.text[:200]}")
            return None
    
    def obtener_historial_recargas(self, lat=None, lon=None):
        """
        Obtiene el historial de recargas del usuario.
        Requiere autenticación.
        """
        if not self.auth_manager:
            print("❌ Esta función requiere autenticación")
            return None
        
        headers = self._get_headers(authenticated=True, lat=lat, lon=lon)
        url = f"{self.base_url}/appoperation/recharge/history"
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ Error obteniendo historial: {e}")
            return None
    
    def obtener_datos_usuario(self, lat=None, lon=None):
        """
        Obtiene los datos del perfil del usuario.
        Requiere autenticación.
        """
        if not self.auth_manager:
            print("❌ Esta función requiere autenticación")
            return None
        
        headers = self._get_headers(authenticated=True, lat=lat, lon=lon)
        url = f"{self.base_url}/appuser/newUserData"
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ Error obteniendo datos de usuario: {e}")
            return None
    
    def is_authenticated(self):
        """Comprueba si hay una sesión autenticada válida."""
        if not self.auth_manager:
            return False
        return self.auth_manager.is_token_valid() or bool(self.auth_manager.refresh_token)
