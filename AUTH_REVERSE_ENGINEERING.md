# Ingeniería Inversa del Login de Iberdrola (App v4.35.0)

## Resumen Ejecutivo
La aplicación utiliza **Auth0** como proveedor de identidad con:
- **OAuth 2.0 + PKCE** (Proof Key for Code Exchange)
- **MFA obligatorio** por email (código de 6 dígitos)
- **Tokens JWT** con expiración corta (6 minutos)

## Credenciales de la App (Producción - España)
| Campo | Valor |
|-------|-------|
| Client ID | `6K4rRPc6x0LmBO7FLWKxrqhBewNEYbuU` |
| Auth Domain | `login-rp.iberdrola.com` |
| Redirect URI | `rv://callback/android/es.iberdrola.recargaverde/callback` |
| Audience | `http://eva.iberdrola.com/veappapi/okta/` |

## Flujo de Autenticación Completo

### Diagrama de Flujo
```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│   Cliente   │────>│  Auth0 (Auth)    │────>│   EVA API   │
│   (App)     │<────│ login-rp.iber... │<────│ eva.iber... │
└─────────────┘     └──────────────────┘     └─────────────┘
      │                     │
      │ 1. /authorize       │
      │ 2. /u/login (POST)  │
      │ 3. /u/mfa-email-challenge
      │ 4. /oauth/token     │
      │                     │
      ▼                     ▼
   access_token ──────> Authorization: Bearer XXX
```

### Paso 1: Iniciar Autorización
```
GET https://login-rp.iberdrola.com/authorize
    ?client_id=6K4rRPc6x0LmBO7FLWKxrqhBewNEYbuU
    &redirect_uri=rv://callback/android/es.iberdrola.recargaverde/callback
    &response_type=code
    &scope=openid profile email offline_access
    &code_challenge=<SHA256_BASE64URL(code_verifier)>
    &code_challenge_method=S256
    &audience=http://eva.iberdrola.com/veappapi/okta/
```
**Respuesta**: Redirige a `/u/login?state=XXX`

### Paso 2: Enviar Credenciales
```
POST https://login-rp.iberdrola.com/u/login?state=XXX
Content-Type: application/x-www-form-urlencoded

state=XXX&username=email@ejemplo.com&password=contraseña
```
**Respuesta**: Redirige a `/authorize/resume?state=YYY`

### Paso 3: Desafío MFA (Email)
Tras seguir redirecciones, llegamos a:
```
GET https://login-rp.iberdrola.com/u/mfa-email-challenge?state=ZZZ
```
El servidor envía un código de 6 dígitos al email del usuario.

### Paso 4: Enviar Código MFA
```
POST https://login-rp.iberdrola.com/u/mfa-email-challenge?state=ZZZ
Content-Type: application/x-www-form-urlencoded

state=ZZZ&code=123456
```
**Respuesta**: Redirige a callback con `code`:
```
Location: rv://callback/android/.../callback?code=AUTH_CODE&state=STATE
```

### Paso 5: Intercambiar Code por Tokens
```
POST https://login-rp.iberdrola.com/oauth/token
Content-Type: application/json
Auth0-Client: eyJuYW1lIjoiQXV0aDAuQW5kcm9pZCIsImVudiI6eyJhbmRyb2lkIjoiMzAifSwidmVyc2lvbiI6IjMuMTAuMCJ9

{
    "client_id": "6K4rRPc6x0LmBO7FLWKxrqhBewNEYbuU",
    "grant_type": "authorization_code",
    "code": "AUTH_CODE",
    "redirect_uri": "rv://callback/android/es.iberdrola.recargaverde/callback",
    "code_verifier": "RANDOM_STRING_GENERADO_EN_PASO_1"
}
```

**Respuesta** (200 OK):
```json
{
    "access_token": "eyJhbGciOiJSUzI1NiI...",
    "refresh_token": "v1.MUaqbmcpl-wp1je3...",
    "id_token": "eyJhbGciOiJSUzI1NiI...",
    "scope": "openid profile email offline_access",
    "expires_in": 360,
    "token_type": "Bearer"
}
```

### Paso 6: Renovar Token (Sin MFA)
```
POST https://login-rp.iberdrola.com/oauth/token
Content-Type: application/json

{
    "client_id": "6K4rRPc6x0LmBO7FLWKxrqhBewNEYbuU",
    "grant_type": "refresh_token",
    "refresh_token": "v1.MUaqbmcpl-wp1je3..."
}
```

## Uso del Token en la API EVA

```
GET https://eva.iberdrola.com/vecomges/api/appfavoritechargepoint/get-favorite-charge-points
Authorization: Bearer eyJhbGciOiJSUzI1NiI...
versionApp: ANDROID-4.35.0
deviceid: UUID-DEL-DISPOSITIVO
deviceModel: samsung-o1s-SM-G991B
User-Agent: Iberdrola/4.35.0/Dalvik/2.1.0 (...)
```

## Endpoints Autenticados Descubiertos

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/appfavoritechargepoint/get-favorite-charge-points` | GET | Lista de favoritos |
| `/appuser/newUserData` | GET | Datos del perfil |
| `/appoperation/recharge/history` | GET | Historial de recargas |

## JWT Payload (Decodificado)
```json
{
    "idSociedad": "1",
    "idCliente": "536927",
    "iss": "https://login-rp.iberdrola.com/",
    "sub": "auth0|537298",
    "aud": [
        "http://eva.iberdrola.com/veappapi/okta/",
        "https://ibe-iberdrolarp-retd-pro.eu.auth0.com/userinfo"
    ],
    "iat": 1766307201,
    "exp": 1766307561,
    "scope": "openid profile email offline_access",
    "azp": "6K4rRPc6x0LmBO7FLWKxrqhBewNEYbuU"
}
```

## Notas de Implementación

### PKCE (Proof Key for Code Exchange)
1. Generar `code_verifier`: String aleatorio de 43+ caracteres (Base64 URL safe)
2. Calcular `code_challenge`: `BASE64URL(SHA256(code_verifier))`
3. Enviar `code_challenge` en `/authorize`
4. Enviar `code_verifier` en `/oauth/token`

### Cookies Importantes
- `auth0`: Sesión de Auth0 (necesario mantener entre requests)
- `did` / `did_compat`: Device ID de Auth0
- `__cf_bm`: Cloudflare Bot Management

### Cabecera Auth0-Client
Es Base64 de:
```json
{"name":"Auth0.Android","env":{"android":"30"},"version":"3.10.0"}
```

## Archivos Implementados
- `iberdrola_auth.py`: Módulo de autenticación OAuth2+PKCE+MFA
- `iberdrola_api.py`: API con soporte para modo anónimo y autenticado
- `test_auth_api.py`: Script de prueba del flujo completo

## Limitaciones Conocidas
1. **Token expira en 6 minutos**: Requiere uso frecuente del refresh_token
2. **MFA obligatorio**: No se puede evitar el código por email en el primer login
3. **Refresh token**: Permite renovar sin MFA mientras sea válido
