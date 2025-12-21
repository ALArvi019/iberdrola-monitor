#!/usr/bin/env python3
"""
Módulo para leer el código MFA de Iberdrola desde el email.
Conecta a Gmail via IMAP y extrae el código de verificación.

Requisitos:
- Activar IMAP en Gmail: Settings > See all settings > Forwarding and POP/IMAP
- Crear App Password: https://myaccount.google.com/apppasswords
  (Requiere tener 2FA activado en la cuenta de Google)
"""

import imaplib
import email
from email.header import decode_header
import re
import time
import os
from datetime import datetime, timedelta


class IberdrolaEmailReader:
    """Lee códigos MFA de Iberdrola desde el email."""
    
    # Remitente de los emails de Iberdrola
    IBERDROLA_SENDER = "clientes@clientesiberdrola.es"
    SUBJECT_PATTERN = "código de verificación"
    
    def __init__(self, email_address=None, email_password=None, imap_server="imap.gmail.com"):
        """
        Args:
            email_address: Dirección de email (o usar IMAP_USER env var)
            email_password: App Password de Gmail (o usar IMAP_PASS env var)
            imap_server: Servidor IMAP (default: Gmail)
        """
        self.email_address = email_address or os.getenv("IMAP_USER")
        self.email_password = email_password or os.getenv("IMAP_PASS")
        self.imap_server = imap_server
        self.imap_port = 993
        
        if not self.email_address or not self.email_password:
            raise ValueError("Necesitas configurar IMAP_USER e IMAP_PASS en el .env")
    
    def connect(self):
        """Conecta al servidor IMAP."""
        try:
            self.mail = imaplib.IMAP4_SSL(self.imap_server, self.imap_port)
            self.mail.login(self.email_address, self.email_password)
            return True
        except imaplib.IMAP4.error as e:
            print(f"❌ Error de autenticación IMAP: {e}")
            print("   Asegúrate de usar una App Password de Google, no tu contraseña normal.")
            print("   Crear en: https://myaccount.google.com/apppasswords")
            return False
    
    def disconnect(self):
        """Desconecta del servidor IMAP."""
        try:
            self.mail.logout()
        except:
            pass
    
    def _decode_subject(self, subject):
        """Decodifica el subject del email."""
        decoded_parts = decode_header(subject)
        subject_text = ""
        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                subject_text += part.decode(encoding or 'utf-8', errors='ignore')
            else:
                subject_text += part
        return subject_text
    
    def _extract_code_from_html(self, html_content):
        """Extrae el código de 6 dígitos del HTML del email."""
        # El código está en un <strong>XXXXXX</strong> dentro de una celda específica
        # Patrón: buscar 6 dígitos dentro de <strong>
        patterns = [
            r'<strong>\s*(\d{6})\s*</strong>',  # <strong>123456</strong>
            r'<td[^>]*>\s*<strong>\s*(\d{6})\s*</strong>',  # En una celda
            r'código[^<]*<[^>]*>(\d{6})',  # Cerca de la palabra "código"
            r'(\d{6})',  # Cualquier número de 6 dígitos como fallback
        ]
        
        for pattern in patterns:
            match = re.search(pattern, html_content, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1)
        
        return None
    
    def _get_email_body(self, msg):
        """Extrae el cuerpo del email (HTML o texto)."""
        body = ""
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or 'utf-8'
                        body = payload.decode(charset, errors='ignore')
                        break
                elif content_type == "text/plain" and not body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or 'utf-8'
                        body = payload.decode(charset, errors='ignore')
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                body = payload.decode(charset, errors='ignore')
        
        return body
    
    def get_latest_mfa_code(self, max_age_minutes=5, max_wait_seconds=60, poll_interval=5):
        """
        Obtiene el código MFA más reciente de Iberdrola.
        Solo busca emails que llegaron DESPUÉS de llamar a esta función.
        
        Args:
            max_age_minutes: Máxima antigüedad del email en minutos (backup)
            max_wait_seconds: Tiempo máximo de espera si no hay email
            poll_interval: Intervalo entre intentos en segundos
            
        Returns:
            str: Código de 6 dígitos o None si no se encuentra
        """
        if not self.connect():
            return None
        
        # Guardar el momento en que empezamos a buscar
        from datetime import timezone
        search_start_time = datetime.now(timezone.utc)
        
        try:
            start_time = time.time()
            
            while (time.time() - start_time) < max_wait_seconds:
                code = self._search_for_code(max_age_minutes, search_start_time)
                if code:
                    return code
                
                if (time.time() - start_time) + poll_interval < max_wait_seconds:
                    print(f"   ⏳ Esperando email de Iberdrola... ({int(time.time() - start_time)}s)")
                    time.sleep(poll_interval)
                else:
                    break
            
            print("❌ No se encontró email de verificación reciente")
            return None
            
        finally:
            self.disconnect()
    
    def _search_for_code(self, max_age_minutes, search_start_time=None):
        """Busca el código en los emails recientes."""
        self.mail.select("INBOX")
        
        # Buscar emails del remitente de Iberdrola
        search_criteria = f'(FROM "{self.IBERDROLA_SENDER}")'
        
        status, messages = self.mail.search(None, search_criteria)
        
        if status != "OK" or not messages[0]:
            return None
        
        # Obtener los IDs de los emails (del más reciente al más antiguo)
        email_ids = messages[0].split()
        email_ids.reverse()  # Más reciente primero
        
        from datetime import timezone
        now_utc = datetime.now(timezone.utc)
        max_age = timedelta(minutes=max_age_minutes)
        
        # Revisar los últimos emails
        for email_id in email_ids[:10]:  # Solo revisar los 10 más recientes
            status, msg_data = self.mail.fetch(email_id, "(RFC822)")
            
            if status != "OK":
                continue
            
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    # Verificar el subject
                    subject = self._decode_subject(msg.get("Subject", ""))
                    if self.SUBJECT_PATTERN.lower() not in subject.lower():
                        continue
                    
                    # Verificar la fecha
                    date_str = msg.get("Date", "")
                    try:
                        # Parsear la fecha del email
                        email_date = email.utils.parsedate_to_datetime(date_str)
                        
                        # Asegurar que tiene timezone, si no asumir UTC
                        if email_date.tzinfo is None:
                            email_date = email_date.replace(tzinfo=timezone.utc)
                        
                        # Si tenemos search_start_time, solo aceptar emails más nuevos
                        if search_start_time:
                            # Dar 30 segundos de margen por posibles delays
                            adjusted_start = search_start_time - timedelta(seconds=30)
                            if email_date < adjusted_start:
                                # Email es anterior a cuando empezamos a buscar
                                continue
                        
                        age = now_utc - email_date
                        
                        if age > max_age:
                            # Email muy antiguo, saltar
                            continue
                        
                    except Exception as e:
                        # Si no podemos parsear la fecha, asumir que es reciente
                        pass
                    
                    # Extraer el código del cuerpo
                    body = self._get_email_body(msg)
                    code = self._extract_code_from_html(body)
                    
                    if code:
                        print(f"   ✅ Código MFA encontrado: {code}")
                        return code
        
        return None


def get_mfa_code_from_email(max_wait_seconds=60):
    """
    Función helper para obtener el código MFA del email.
    
    Returns:
        str: Código de 6 dígitos o None
    """
    try:
        reader = IberdrolaEmailReader()
        print("📧 Buscando código MFA en el email...")
        return reader.get_latest_mfa_code(
            max_age_minutes=5,
            max_wait_seconds=max_wait_seconds,
            poll_interval=5
        )
    except ValueError as e:
        print(f"⚠️ {e}")
        return None
    except Exception as e:
        print(f"❌ Error leyendo email: {e}")
        return None


if __name__ == "__main__":
    # Test del módulo
    print("=" * 60)
    print("📧 TEST DE LECTURA DE EMAIL MFA")
    print("=" * 60)
    
    code = get_mfa_code_from_email(max_wait_seconds=30)
    
    if code:
        print(f"\n🎉 Código obtenido: {code}")
    else:
        print("\n❌ No se pudo obtener el código")
        print("\nVerifica que:")
        print("1. IMAP_USER e IMAP_PASS están en el .env")
        print("2. Estás usando una App Password de Google")
        print("3. IMAP está activado en Gmail")
