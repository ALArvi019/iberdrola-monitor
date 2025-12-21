#!/usr/bin/env python3
"""
Módulo para procesar pagos con Redsys (preautorización para reservas).
Implementa el flujo de pago virtual de la app Iberdrola.
"""

import hashlib
import json
import os
import requests
from typing import Optional, Dict, Any

class RedsysPayment:
    """Procesador de pagos Redsys para Iberdrola."""
    
    # URLs de Redsys
    REDSYS_PROD_URL = "https://sis.redsys.es/sis/virtualControllerV2/generaFirmaPagoVirtual"
    REDSYS_PAGO_URL = "https://sis.redsys.es/sis/realizarPago"
    
    # Bundle de la app
    BUNDLE = "es.iberdrola.recargaverde"
    
    def __init__(self):
        # Licencia Android de Iberdrola (del APK o de variable de entorno)
        self.android_license = os.getenv('REDSYS_ANDROID_LICENSE', 'NMQuPUdGvjcP7yLhJHvH')
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; SM-G930F Build/RQ3A.211001.001)",
            "Accept-Encoding": "gzip",
            "Connection": "Keep-Alive"
        })
    
    def _generate_signature(self, message_json: str) -> str:
        """
        Genera la firma SHA256 del mensaje + license.
        La app usa: SHA256(json + androidLicense) en hex.
        """
        data = message_json + self.android_license
        # Usar ISO-8859-1 como en la app
        hash_bytes = hashlib.sha256(data.encode('iso-8859-1')).digest()
        return hash_bytes.hex()
    
    def generate_payment_request(
        self,
        order_id: str,
        amount_cents: int,
        token: str,
        merchant_code: str,
        terminal: str,
        currency: str,
        product_description: str,
        merchant_url: str,
        url_ok: str,
        url_ko: str,
        consumer_language: str = "1"
    ) -> Dict[str, Any]:
        """
        Genera la petición de pago para Redsys.
        
        Args:
            order_id: ID de la orden (de getOrderId)
            amount_cents: Importe en céntimos (1€ = 100)
            token: Token del método de pago (Ds_Merchant_Identifier)
            merchant_code: Código del comercio
            terminal: Terminal
            currency: Moneda (978 = EUR)
            product_description: Descripción del producto
            merchant_url: URL de notificación
            url_ok: URL de éxito
            url_ko: URL de error
            consumer_language: Idioma (1 = español)
            
        Returns:
            dict con datoEntrada para enviar a Redsys
        """
        # Parámetros del pago
        parametros = {
            "Ds_Merchant_TransactionType": "1",  # Autorización
            "Ds_Merchant_UrlOK": url_ok,
            "Ds_Merchant_Identifier": token,
            "Ds_Merchant_DirectPayment": "false",
            "Ds_Merchant_Amount": str(amount_cents),
            "Ds_Merchant_UrlKO": url_ko,
            "Ds_Merchant_Order": order_id,
            "Ds_Merchant_Currency": currency,
            "Ds_Merchant_MerchantCode": merchant_code,
            "Ds_Merchant_Module": "PSis_Android",
            "Ds_Merchant_ProductDescription": product_description,
            "Ds_Merchant_Terminal": terminal,
            "Ds_Merchant_ConsumerLanguage": consumer_language,
            "Ds_Merchant_MerchantURL": merchant_url
        }
        
        # Mensaje completo
        mensaje = {
            "parametros": parametros,
            "bundle": self.BUNDLE,
            "so": "Android",
            "fuc": merchant_code,
            "terminal": terminal,
            "version": "2.3.0"
        }
        
        mensaje_json = json.dumps(mensaje, separators=(',', ':'))
        
        # Generar firma
        firma = self._generate_signature(mensaje_json)
        
        # Petición final
        dato_entrada = {
            "firma": firma,
            "mensaje": mensaje_json
        }
        
        return dato_entrada
    
    def request_payment_signature(self, dato_entrada: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Paso 1: Llama a generaFirmaPagoVirtual para obtener la firma de Redsys.
        
        Returns:
            dict con Ds_MerchantParameters, Ds_Signature, Ds_SignatureVersion
        """
        try:
            # Serializar dato_entrada como JSON
            dato_entrada_json = json.dumps(dato_entrada, separators=(',', ':'))
            
            response = self.session.post(
                self.REDSYS_PROD_URL,
                data={"datoEntrada": dato_entrada_json},
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=30
            )
            
            response.raise_for_status()
            result = response.json()
            
            # Parsear el mensaje interno
            if "mensaje" in result:
                mensaje_interno = json.loads(result["mensaje"])
                if mensaje_interno.get("code") == 0:
                    return mensaje_interno.get("datosPeticion")
                else:
                    print(f"❌ Error Redsys: {mensaje_interno.get('desc')}")
                    return None
            
            return None
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Error en petición a Redsys: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"❌ Error parseando respuesta Redsys: {e}")
            return None
    
    def execute_payment(
        self,
        ds_merchant_parameters: str,
        ds_signature: str,
        ds_signature_version: str = "HMAC_SHA256_V1"
    ) -> bool:
        """
        Paso 2: Ejecuta el pago llamando a realizarPago.
        
        Returns:
            True si el pago se procesa (aunque sea con redirección)
        """
        try:
            response = self.session.post(
                self.REDSYS_PAGO_URL,
                data={
                    "Ds_MerchantParameters": ds_merchant_parameters,
                    "Ds_Signature": ds_signature,
                    "Ds_SignatureVersion": ds_signature_version
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Origin": "null",
                    "X-Requested-With": self.BUNDLE
                },
                timeout=30,
                allow_redirects=True
            )
            
            # Redsys devuelve HTML, verificamos el código de estado
            if response.status_code == 200:
                # Buscar indicadores de éxito en la respuesta
                content = response.text.lower()
                if "error" in content and "pago" in content:
                    print("⚠️ Posible error en el pago")
                    return False
                return True
            
            return False
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Error ejecutando pago: {e}")
            return False
    
    def execute_payment_with_3ds(
        self,
        ds_merchant_parameters: str,
        ds_signature: str,
        ds_signature_version: str = "HMAC_SHA256_V1",
        timeout_seconds: int = 120
    ) -> bool:
        """
        Ejecuta el pago con soporte para 3D Secure usando Playwright.
        Abre un navegador, envía el formulario y espera la autenticación.
        
        Args:
            ds_merchant_parameters: Parámetros del pago en Base64
            ds_signature: Firma del pago
            ds_signature_version: Versión de la firma
            timeout_seconds: Tiempo máximo de espera para 3DS (default: 120s)
            
        Returns:
            True si el pago se completa correctamente
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("❌ Playwright no está instalado. Instala con: pip install playwright && playwright install chromium")
            return False
        
        import time
        
        print(f"🌐 Abriendo navegador para 3D Secure...")
        print(f"   ⏱️ Tienes {timeout_seconds} segundos para aprobar en tu móvil")
        
        success = False
        
        with sync_playwright() as p:
            # Lanzar navegador visible para que el usuario vea el proceso
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 11; SM-G930F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.70 Mobile Safari/537.36"
            )
            page = context.new_page()
            
            try:
                # Crear una página HTML temporal con el formulario de pago
                form_html = f'''
                <!DOCTYPE html>
                <html>
                <head><title>Procesando pago...</title></head>
                <body>
                    <h2>Redirigiendo a Redsys...</h2>
                    <form id="redsysForm" action="{self.REDSYS_PAGO_URL}" method="POST">
                        <input type="hidden" name="Ds_MerchantParameters" value="{ds_merchant_parameters}">
                        <input type="hidden" name="Ds_Signature" value="{ds_signature}">
                        <input type="hidden" name="Ds_SignatureVersion" value="{ds_signature_version}">
                    </form>
                    <script>document.getElementById('redsysForm').submit();</script>
                </body>
                </html>
                '''
                
                # Cargar el HTML y enviar automáticamente
                page.set_content(form_html)
                
                notification_url = "eva.iberdrola.com/vepagos/api/redsys/notification"
                
                # Esperar a que la navegación inicial complete (puede ser rápida si no hay 3DS)
                page.wait_for_timeout(2000)
                
                # Verificar INMEDIATAMENTE si ya redirigió (caso sin 3DS)
                try:
                    current_url = page.url
                    if notification_url in current_url:
                        print("   ✅ Pago completado sin 3DS - redirigido directamente")
                        success = True
                except:
                    pass
                
                if not success:
                    print("   📱 Esperando confirmación 3D Secure en tu móvil...")
                    
                    # Usar wait_for_url para detectar el redirect de forma más confiable
                    try:
                        # Esperar hasta que la URL contenga la notification URL
                        import re
                        page.wait_for_url(
                            re.compile(r".*eva\.iberdrola\.com/vepagos/api/redsys/notification.*"),
                            timeout=timeout_seconds * 1000  # En milisegundos
                        )
                        print("   ✅ Pago completado - redirigido a notificación")
                        success = True
                    except Exception as wait_error:
                        error_str = str(wait_error)
                        
                        # Si el error es por auth credentials, significa que SÍ llegó a la URL
                        if "ERR_INVALID_AUTH_CREDENTIALS" in error_str or "401" in error_str:
                            print("   ✅ Pago completado (redirect detectado)")
                            success = True
                        else:
                            # Verificar si de todas formas llegó a la URL
                            try:
                                final_url = page.url
                                if notification_url in final_url:
                                    print("   ✅ Pago detectado (verificación final)")
                                    success = True
                                else:
                                    print(f"   ⏰ Timeout o error durante la espera")
                            except:
                                print(f"   ⏰ Timeout - no se completó el pago")


                    
            except Exception as e:
                print(f"   ❌ Error durante el pago: {e}")
                
            finally:
                browser.close()
        
        return success


def process_reservation_payment(
    order_data: Dict[str, Any],
    payment_token: str,
    amount_cents: int = 100,  # 1€ por defecto
    use_3ds: bool = True,
    timeout_seconds: int = 120
) -> bool:
    """
    Procesa el pago/preautorización para una reserva.
    
    Args:
        order_data: Respuesta de getOrderId
        payment_token: Token del método de pago
        amount_cents: Importe en céntimos
        use_3ds: Si True, usa Playwright para 3DS. Si False, intenta sin 3DS.
        timeout_seconds: Tiempo de espera para 3DS
        
    Returns:
        True si el pago se procesa correctamente
    """
    redsys = RedsysPayment()
    
    # Paso 1: Generar petición de pago
    print("🔐 Generando petición de pago...")
    dato_entrada = redsys.generate_payment_request(
        order_id=order_data["orderId"],
        amount_cents=amount_cents,
        token=payment_token,
        merchant_code=order_data["merchantCode"],
        terminal=order_data["terminal"],
        currency=order_data["currency"],
        product_description=order_data["productDescription"],
        merchant_url=order_data["merchantUrl"],
        url_ok=order_data["urlOk"],
        url_ko=order_data["urlKo"],
        consumer_language=order_data.get("consumerLanguage", "001")
    )
    
    # Paso 2: Obtener firma de Redsys
    print("📝 Obteniendo firma de Redsys...")
    payment_data = redsys.request_payment_signature(dato_entrada)
    
    if not payment_data:
        print("❌ No se pudo obtener firma de Redsys")
        return False
    
    print(f"✅ Firma obtenida: {payment_data.get('Ds_Signature', '')[:20]}...")
    
    # Paso 3: Ejecutar pago
    if use_3ds:
        print("💳 Ejecutando pago con 3D Secure...")
        success = redsys.execute_payment_with_3ds(
            ds_merchant_parameters=payment_data["Ds_MerchantParameters"],
            ds_signature=payment_data["Ds_Signature"],
            ds_signature_version=payment_data.get("Ds_SignatureVersion", "HMAC_SHA256_V1"),
            timeout_seconds=timeout_seconds
        )
    else:
        print("💳 Ejecutando pago/preautorización (sin 3DS)...")
        success = redsys.execute_payment(
            ds_merchant_parameters=payment_data["Ds_MerchantParameters"],
            ds_signature=payment_data["Ds_Signature"],
            ds_signature_version=payment_data.get("Ds_SignatureVersion", "HMAC_SHA256_V1")
        )
    
    if success:
        print("✅ Pago procesado correctamente")
    else:
        print("❌ Error en el pago")
    
    return success


if __name__ == "__main__":
    # Test básico de firma
    print("=== TEST REDSYS PAYMENT ===")
    
    redsys = RedsysPayment()
    
    # Test de generación de firma (sin datos reales)
    test_message = '{"test": "data"}'
    signature = redsys._generate_signature(test_message)
    
    print(f"✅ Módulo cargado correctamente")
    print(f"   Android License configurada: {'Sí' if redsys.android_license else 'No'}")
    print(f"   Firma de prueba generada: {signature[:20]}...")
    print("\nPara usar el flujo completo, ejecuta reservar_cargador.py")

