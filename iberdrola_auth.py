"""
Módulo de Autenticación para Iberdrola Recarga Pública.
Implementa el flujo OAuth2 + PKCE + MFA por email de Auth0.

Flow:
1. Iniciar sesión web (obtener state inicial)
2. POST /u/login con credenciales
3. Seguir redirecciones hasta /u/mfa-email-challenge
4. Solicitar código OTP al usuario (enviado por email)
5. POST /u/mfa-email-challenge con el código
6. Seguir redirecciones hasta obtener authorization code
7. POST /oauth/token para intercambiar code por tokens
8. Usar refresh_token para renovar access_token cuando expire
"""

import requests
import hashlib
import base64
import secrets
import re
import json
import os
from urllib.parse import urlencode, urlparse, parse_qs
from datetime import datetime, timedelta


class IberdrolaAuth:
    """Gestiona la autenticación OAuth2+PKCE con MFA de Iberdrola."""
    
    AUTH_BASE_URL = "https://login-rp.iberdrola.com"
    CLIENT_ID = "6K4rRPc6x0LmBO7FLWKxrqhBewNEYbuU"
    REDIRECT_URI = "rv://callback/android/es.iberdrola.recargaverde/callback"
    
    def __init__(self, tokens_file="data/auth_tokens.json"):
        self.tokens_file = tokens_file
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Linux; Android 11; SM-G930F) AppleWebKit/537.36 Chrome/129.0.6668.70 Mobile Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9",
        })
        
        # PKCE values (generated fresh each login attempt)
        self.code_verifier = None
        self.code_challenge = None
        
        # Tokens
        self.access_token = None
        self.refresh_token = None
        self.id_token = None
        self.token_expiry = None
        
        # Try to load existing tokens (only if tokens_file is set)
        if self.tokens_file:
            self._load_tokens()
    
    def _generate_pkce(self):
        """Generate PKCE code_verifier and code_challenge."""
        # Generate random 43 character string (Base64 URL safe)
        self.code_verifier = secrets.token_urlsafe(32)
        
        # SHA256 hash of verifier, then Base64 URL encode
        digest = hashlib.sha256(self.code_verifier.encode('utf-8')).digest()
        self.code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('utf-8')
    
    def _save_tokens(self):
        """Save tokens to file for persistence."""
        if not self.tokens_file:
            return  # No file storage, tokens managed externally
        
        os.makedirs(os.path.dirname(self.tokens_file), exist_ok=True)
        data = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "token_expiry": self.token_expiry.isoformat() if self.token_expiry else None,
        }
        with open(self.tokens_file, 'w') as f:
            json.dump(data, f)
    
    def _load_tokens(self):
        """Load tokens from file if they exist."""
        try:
            with open(self.tokens_file, 'r') as f:
                data = json.load(f)
                self.access_token = data.get("access_token")
                self.refresh_token = data.get("refresh_token")
                self.id_token = data.get("id_token")
                if data.get("token_expiry"):
                    self.token_expiry = datetime.fromisoformat(data["token_expiry"])
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    
    def is_token_valid(self):
        """Check if current access token is still valid."""
        if not self.access_token or not self.token_expiry:
            return False
        # Give 30 seconds buffer
        return datetime.now() < (self.token_expiry - timedelta(seconds=30))
    
    def get_access_token(self):
        """Get a valid access token, refreshing if necessary."""
        if self.is_token_valid():
            return self.access_token
        
        if self.refresh_token:
            print("🔄 Token expirado, renovando...")
            if self.refresh_access_token():
                return self.access_token
        
        return None
    
    def refresh_access_token(self):
        """Use refresh_token to get a new access_token."""
        if not self.refresh_token:
            print("❌ No hay refresh_token disponible")
            return False
        
        url = f"{self.AUTH_BASE_URL}/oauth/token"
        payload = {
            "client_id": self.CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        headers = {
            "Content-Type": "application/json",
            "Auth0-Client": base64.b64encode(
                json.dumps({"name": "Auth0.Android", "env": {"android": "30"}, "version": "3.10.0"}).encode()
            ).decode(),
        }
        
        try:
            resp = requests.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)  # May or may not be returned
            self.id_token = data.get("id_token")
            self.token_expiry = datetime.now() + timedelta(seconds=data.get("expires_in", 360))
            
            self._save_tokens()
            print("✅ Token renovado correctamente")
            return True
            
        except Exception as e:
            print(f"❌ Error renovando token: {e}")
            return False
    
    def start_login(self, username: str, password: str):
        """
        Inicia el proceso de login usando Playwright para resolver captcha Turnstile.
        Devuelve el state para el MFA si se requiere 2FA, o tokens si no.
        """
        self._generate_pkce()

        authorize_url = f"{self.AUTH_BASE_URL}/authorize"
        params = {
            "client_id": self.CLIENT_ID,
            "redirect_uri": self.REDIRECT_URI,
            "response_type": "code",
            "scope": "openid profile email offline_access",
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
            "audience": "http://eva.iberdrola.com/veappapi/okta/",
        }
        full_url = f"{authorize_url}?{urlencode(params)}"

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("❌ Playwright no disponible, intentando login sin navegador...")
            return self._start_login_requests(username, password, full_url)

        print("🚀 Iniciando login con navegador (captcha Turnstile)...")

        headless_mode = os.getenv('DISPLAY') is None

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless_mode)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.70 Mobile Safari/537.36"
            )
            page = context.new_page()

            try:
                page.goto(full_url, wait_until='networkidle', timeout=30000)
                print(f"📍 URL de login: {page.url[:80]}...")

                # Cerrar banner de cookies (OneTrust) si aparece
                try:
                    reject_btn = page.locator('#onetrust-reject-all-handler')
                    if reject_btn.is_visible(timeout=3000):
                        reject_btn.click()
                        print("🍪 Banner de cookies cerrado")
                        page.wait_for_timeout(500)
                except Exception:
                    pass

                # Rellenar credenciales
                page.fill('input[name="username"]', username)
                page.fill('input[name="password"]', password)

                # Esperar a que Turnstile resuelva el captcha
                try:
                    page.wait_for_function(
                        '() => { const el = document.querySelector("input[name=captcha]"); return el && el.value !== ""; }',
                        timeout=20000
                    )
                    print("✅ Captcha resuelto")
                except Exception:
                    print("⚠️ Timeout en captcha, intentando submit igualmente...")

                # Submit
                print("🔑 Enviando credenciales...")
                page.locator('button[name="action"][value="default"]').filter(has_text="Entrar").last.click(force=True)

                # Esperar redirección a MFA, callback, o error
                try:
                    page.wait_for_url(
                        lambda url: '/u/mfa' in url or 'code=' in url or 'rv://' in url,
                        timeout=30000
                    )
                except Exception:
                    # Comprobar si hay error en la página
                    error_el = page.query_selector('.ulp-input-error-message')
                    error_msg = error_el.inner_text() if error_el else page.url
                    print(f"⚠️ Login falló: {error_msg}")
                    browser.close()
                    return None

                result_url = page.url

                # Transferir cookies a la session de requests (para MFA posterior)
                for cookie in context.cookies():
                    self.session.cookies.set(
                        cookie['name'], cookie['value'],
                        domain=cookie.get('domain', '').lstrip('.'),
                        path=cookie.get('path', '/')
                    )

            except Exception as e:
                print(f"❌ Error en login con navegador: {e}")
                browser.close()
                return None

            browser.close()

        # Procesar resultado
        if "/u/mfa-email-challenge" in result_url:
            print("📧 Se requiere verificación por email (MFA)")
            mfa_state = parse_qs(urlparse(result_url).query).get('state', [None])[0]
            return {"status": "mfa_required", "mfa_state": mfa_state, "mfa_url": result_url}

        if "code=" in result_url:
            return self._handle_callback(result_url)

        print(f"⚠️ Estado inesperado: {result_url}")
        return None

    def _start_login_requests(self, username, password, authorize_url):
        """Fallback: login sin navegador (funciona si no hay captcha)."""
        print("🚀 Iniciando flujo de autenticación (sin navegador)...")
        resp = self.session.get(authorize_url, allow_redirects=True)

        login_url = resp.url
        print(f"📍 URL de login: {login_url[:80]}...")

        state = parse_qs(urlparse(login_url).query).get('state', [None])[0]
        if not state:
            print("❌ No se pudo obtener el state inicial")
            return None

        print("🔑 Enviando credenciales...")
        form_data = {"state": state, "username": username, "password": password}
        resp = self.session.post(
            f"{self.AUTH_BASE_URL}/u/login?state={state}",
            data=form_data, allow_redirects=True
        )

        if "/u/mfa-email-challenge" in resp.url:
            print("📧 Se requiere verificación por email (MFA)")
            mfa_state = parse_qs(urlparse(resp.url).query).get('state', [None])[0]
            return {"status": "mfa_required", "mfa_state": mfa_state, "mfa_url": resp.url}

        if "code=" in resp.url:
            return self._handle_callback(resp.url)

        print(f"⚠️ Estado inesperado: {resp.url}")
        return None
    
    def submit_mfa_code(self, mfa_state: str, otp_code: str):
        """Submit the MFA code received by email."""
        print(f"🔢 Enviando código MFA: {otp_code}")
        
        mfa_url = f"{self.AUTH_BASE_URL}/u/mfa-email-challenge?state={mfa_state}"
        form_data = {
            "state": mfa_state,
            "code": otp_code,
        }
        
        # The final redirect will be to rv://callback... which requests can't follow
        # We need to catch the InvalidSchema exception and extract the URL from it
        try:
            resp = self.session.post(mfa_url, data=form_data, allow_redirects=True)
            final_url = resp.url
            
            # If we're still on login-rp, check for the redirect in the HTML
            if "login-rp.iberdrola.com" in final_url:
                # Try to find rv:// callback in response
                match = re.search(r'href="(rv://callback[^"]+)"', resp.text)
                if match:
                    final_url = match.group(1)
            
        except requests.exceptions.InvalidSchema as e:
            # This is expected! The rv:// scheme causes this error
            # Extract the URL from the error message
            error_msg = str(e)
            match = re.search(r"'(rv://[^']+)'", error_msg)
            if match:
                final_url = match.group(1)
                print(f"📥 Callback capturado: {final_url[:60]}...")
            else:
                print(f"❌ No se pudo extraer la URL del callback: {error_msg}")
                return None
        
        if "code=" in final_url:
            return self._handle_callback(final_url)
        
        print(f"⚠️ No se encontró código de autorización. URL: {final_url[:100]}")
        return None
    
    def _handle_callback(self, callback_url: str):
        """Extract authorization code and exchange for tokens."""
        print("📥 Procesando callback...")
        
        # Parse the callback URL
        parsed = urlparse(callback_url)
        params = parse_qs(parsed.query)
        
        auth_code = params.get('code', [None])[0]
        if not auth_code:
            print("❌ No se encontró código de autorización")
            return None
        
        print(f"🎫 Código de autorización: {auth_code[:20]}...")
        
        # Exchange code for tokens
        return self._exchange_code_for_tokens(auth_code)
    
    def _exchange_code_for_tokens(self, auth_code: str):
        """Exchange authorization code for access/refresh tokens."""
        print("🔄 Intercambiando código por tokens...")
        
        url = f"{self.AUTH_BASE_URL}/oauth/token"
        payload = {
            "client_id": self.CLIENT_ID,
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": self.REDIRECT_URI,
            "code_verifier": self.code_verifier,
        }
        headers = {
            "Content-Type": "application/json",
            "Auth0-Client": base64.b64encode(
                json.dumps({"name": "Auth0.Android", "env": {"android": "30"}, "version": "3.10.0"}).encode()
            ).decode(),
        }
        
        try:
            resp = requests.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
            self.access_token = data["access_token"]
            self.refresh_token = data["refresh_token"]
            self.id_token = data.get("id_token")
            self.token_expiry = datetime.now() + timedelta(seconds=data.get("expires_in", 360))
            
            self._save_tokens()
            
            print("✅ ¡Autenticación completada!")
            print(f"   Token expira en: {data.get('expires_in', 360)} segundos")
            
            return {"status": "success", "access_token": self.access_token}
            
        except Exception as e:
            print(f"❌ Error intercambiando código: {e}")
            if hasattr(e, 'response'):
                print(f"   Respuesta: {e.response.text}")
            return None


def interactive_login(auto_mfa=True):
    """
    Helper function for interactive login with MFA.
    
    Args:
        auto_mfa: If True and IMAP credentials are configured, try to read MFA code from email
    """
    auth = IberdrolaAuth()
    
    # Check if we already have valid tokens
    if auth.is_token_valid():
        print("✅ Ya tienes una sesión válida")
        return auth.access_token
    
    # Try to refresh if we have a refresh token
    if auth.refresh_token:
        if auth.refresh_access_token():
            return auth.access_token
    
    # Need full login
    username = os.getenv("IBERDROLA_USER") or input("📧 Email: ")
    password = os.getenv("IBERDROLA_PASS") or input("🔑 Contraseña: ")
    
    result = auth.start_login(username, password)
    
    if result and result.get("status") == "mfa_required":
        print("\n📧 Se ha enviado un código a tu email.")
        
        otp = None
        
        # Try automatic email reading if configured
        if auto_mfa and os.getenv("IMAP_USER") and os.getenv("IMAP_PASS"):
            try:
                from email_mfa_reader import get_mfa_code_from_email
                print("🤖 Intentando leer código automáticamente del email...")
                otp = get_mfa_code_from_email(max_wait_seconds=60)
            except ImportError:
                print("⚠️ Módulo email_mfa_reader no disponible")
            except Exception as e:
                print(f"⚠️ Error leyendo email: {e}")
        
        # If automatic reading failed, ask for manual input
        if not otp:
            otp = input("🔢 Introduce el código: ")
        
        result = auth.submit_mfa_code(result["mfa_state"], otp)
    
    if result and result.get("status") == "success":
        return result["access_token"]
    
    return None


def automatic_login():
    """
    Intenta hacer login completamente automático.
    Requiere IBERDROLA_USER, IBERDROLA_PASS, IMAP_USER, IMAP_PASS en el .env
    
    Returns:
        str: access_token o None si falla
    """
    return interactive_login(auto_mfa=True)


if __name__ == "__main__":
    token = interactive_login()
    if token:
        print(f"\n🎉 Token obtenido: {token[:50]}...")
    else:
        print("\n❌ No se pudo obtener el token")

