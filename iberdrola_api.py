#!/usr/bin/env python3
"""
API de Iberdrola para obtener información de cargadores
Versión simplificada para Docker
"""

import requests
import json

class IberdrolaAPI:
    def __init__(self, device_id):
        self.base_url = "https://eva.iberdrola.com/vecomges/api"
        self.device_id = device_id
        self.headers = {
            'Content-Type': 'application/json; charset=UTF-8',
            'Accept': 'application/json',
            'Accept-Language': 'es-ES',
            'Authorization': '',
            'Accept-Encoding': 'gzip',
            'versionApp': 'ANDROID-4.35.0',
            'Plataforma': 'Android',
            'societyId': '1',
            'deviceid': self.device_id,
            'deviceModel': 'samsung-o1s-SM-G991B',
            'c-rid': '2d28f5-b2e-5c6-ea6-038d5f9c0',
            'darkMode': '0',
            'User-Agent': 'Iberdrola/4.35.0/Dalvik/2.1.0 (Linux; U; Android 13; SM-G991B Build/TP1A.220624.014)',
            'Connection': 'Keep-Alive'
        }
    
    def obtener_detalles_cargador(self, cupr_ids, lat=None, lon=None):
        """Obtiene detalles completos de uno o varios cargadores por su cuprId"""
        if isinstance(cupr_ids, int):
            cupr_ids = [cupr_ids]
        
        payload = {"cuprId": cupr_ids}
        
        if lat and lon:
            self.headers['numLat'] = str(lat)
            self.headers['numLon'] = str(lon)
        
        url = f"{self.base_url}/appchargepoint/getChargePoint"
        
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=10)
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
