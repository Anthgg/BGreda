# BGreda — Backend del Cotizador Greda

API HTTP construida con FastAPI. Es la **única autoridad** del sistema: toda la
lógica de negocio, la autenticación, el acceso a Supabase / PostgreSQL y la
integración con servicios externos ocurren aquí.

- Frontend (consumidor de esta API): <https://github.com/Anthgg/FGreda>

---

## Regla arquitectónica

```
React (FGreda)
   │  HTTPS  (cookies HttpOnly + X-CSRF-Token)
FastAPI (BGreda)
   │
Supabase Auth · PostgreSQL · servicios externos
```

El frontend **nunca** habla con Supabase ni con PostgreSQL, no recibe tokens, no
conoce el esquema de datos y no ejecuta lógica de negocio oficial. Cualquier dato
o permiso que llegue desde el navegador se vuelve a validar en el backend.

---

## Requisitos

| Herramienta | Versión |
|---|---|
| Python | 3.12 o superior |
| [uv](https://docs.astral.sh/uv/) | 0.12 o superior |
| PostgreSQL | el del proyecto Supabase |
| Docker | opcional, para la imagen de producción |

---

## Variables de entorno

Copie `.env.example` a `.env` y complete los valores. **`.env` nunca se commitea.**

| Variable | Obligatoria | Descripción |
|---|---|---|
| `APP_ENV` | no | `local`, `development`, `staging` o `production`. Por defecto `local`. |
| `APP_NAME` | no | Nombre mostrado en la documentación y los health checks. |
| `LOG_LEVEL` | no | Nivel de logging. Por defecto `INFO`. |
| `SUPABASE_URL` | sí | URL del proyecto Supabase, sin barra final. |
| `SUPABASE_PUBLISHABLE_KEY` | sí | Publishable key. **Solo backend**, jamás se envía al frontend. |
| `SUPABASE_TIMEOUT_SECONDS` | no | Timeout de las llamadas a Supabase. Por defecto `10`. |
| `DATABASE_URL` | sí | Credencial PostgreSQL para SQLAlchemy y Alembic. |
| `FRONTEND_ORIGINS` | sí | Orígenes CORS permitidos, separados por comas. Nunca `*`. |
| `COOKIE_SECURE` | sí | `true` en producción. |
| `COOKIE_SAMESITE` | sí | `lax`, `strict` o `none`. |
| `COOKIE_DOMAIN` | no | Dominio de las cookies. Vacío = host actual. |
| `REFRESH_COOKIE_MAX_AGE_SECONDS` | no | Vida de la cookie de refresh. Por defecto 30 días. |
| `CSRF_SECRET` | sí en producción | Mínimo 32 caracteres. Fuera de producción se genera uno efímero. |
| `CSRF_TOKEN_TTL_SECONDS` | no | Vigencia del token CSRF. Por defecto 8 horas. |

> **`DATABASE_URL` no se deduce de Supabase.** La publishable key sirve para
> Supabase Auth; conectarse a PostgreSQL requiere la credencial de base de datos,
> que es distinta y se obtiene en *Project Settings → Database*.

La configuración se valida al arrancar y falla de forma explícita si es insegura:
`COOKIE_SAMESITE=none` exige `COOKIE_SECURE=true`, `FRONTEND_ORIGINS` rechaza el
comodín, y en producción se exigen `CSRF_SECRET` largo, cookies seguras y
Supabase configurado.

---

## Instalación y ejecución local

```bash
uv sync
cp .env.example .env   # y complete los valores
uv run uvicorn app.main:app --reload --port 8000
```

Documentación interactiva en <http://localhost:8000/docs> (deshabilitada en producción).

---

## Comandos

```bash
uv run pytest --ignore=tests/smoke   # suite completa, sin red ni base de datos
uv run ruff check .                  # lint
uv run ruff format .                 # formato
uv run mypy                          # tipos
```

Los *smoke tests* corren aparte, contra un entorno ya desplegado:

```bash
SMOKE_BASE_URL=https://... SMOKE_EMAIL=... SMOKE_PASSWORD=... uv run pytest tests/smoke -m smoke
```

---

## Migraciones

```bash
uv run alembic upgrade head            # aplicar
uv run alembic upgrade head --sql      # revisar el SQL sin conectarse
uv run alembic downgrade base          # revertir
uv run alembic revision --autogenerate -m "descripcion"
```

`alembic/env.py` toma la conexión de `DATABASE_URL`; no hay credenciales en
`alembic.ini`.

### Seguridad de `profiles`

Supabase publica automáticamente el esquema `public` mediante PostgREST usando la
publishable key. Por eso la migración `0001` habilita **RLS sin ninguna policy**:
los roles `anon` y `authenticated` quedan sin acceso, y además se les revocan los
privilegios sobre la tabla. El backend se conecta con el rol de `DATABASE_URL`,
propietario de la tabla, que no está sujeto a RLS.

No se usa `FORCE ROW LEVEL SECURITY` a propósito: aplicaría RLS también al
propietario —es decir, al propio backend— sin aportar ninguna protección
adicional frente a PostgREST.

> RLS **no sustituye** la autorización del backend. FastAPI verifica en cada
> petición que el usuario esté autenticado, tenga perfil y esté activo.

### Aprovisionamiento de usuarios

No hay registro público. El alta tiene dos pasos:

1. Crear el usuario en Supabase (*Authentication → Users → Add user*), marcando
   **Auto Confirm User**.
2. Insertar su fila en `profiles` con el mismo `id`, el `display_name` y el rol.

Sin el paso 2 el backend responde `AUTH_PROFILE_NOT_PROVISIONED`: `profiles` es
la lista de habilitación de la aplicación.

---

## Arquitectura

```
app/
├── api/
│   ├── deps.py          # sesión actual, repositorios, autorización por rol
│   ├── health.py        # /live y /ready
│   └── v1/
│       ├── auth.py      # endpoints de autenticación
│       └── router.py    # agregador de /api/v1
├── auth/
│   ├── cookies.py       # emisión y borrado de cookies de sesión
│   ├── csrf.py          # tokens firmados + double submit
│   └── middleware.py    # protección CSRF de métodos mutadores
├── core/
│   ├── config.py        # configuración validada por entorno
│   ├── errors.py        # errores de dominio
│   ├── handlers.py      # formato uniforme de respuesta de error
│   ├── logging.py       # logging sin secretos
│   └── precision.py     # convención Decimal / NUMERIC
├── db/                  # base declarativa y sesiones asíncronas
├── models/              # modelos ORM
├── schemas/             # contratos de entrada y salida
├── services/            # cliente de Supabase Auth, repositorio de perfiles
└── main.py              # composición de la aplicación
```

### Autenticación

1. El frontend pide un token CSRF a `GET /api/v1/auth/csrf` y lo guarda **en memoria**.
2. Envía credenciales a `POST /api/v1/auth/login` con la cabecera `X-CSRF-Token`.
3. El backend autentica contra Supabase Auth y recibe los tokens.
4. Comprueba que el usuario tenga un perfil **aprovisionado y activo**.
5. Guarda los tokens en cookies `HttpOnly`. **Nunca** los devuelve al JavaScript.
6. Responde solo con los datos de usuario necesarios para la interfaz.

`/auth/me` es la única fuente de verdad de la sesión para React. El frontend no
decodifica ni interpreta JWT.

La validación del access token se hace consultando a Supabase en cada petición
autenticada. Es la opción correcta sin manejar el secreto de firma; si el volumen
lo justifica, una fase posterior puede pasar a verificación local vía JWKS.

### Protección CSRF

Dos mecanismos combinados sobre `POST`, `PUT`, `PATCH` y `DELETE` bajo `/api/`:

- **Token firmado**: nonce + expiración + HMAC-SHA256 con `CSRF_SECRET`.
- **Double submit**: el mismo token viaja en una cookie `HttpOnly` y en la
  cabecera `X-CSRF-Token`. Un sitio atacante puede provocar el envío de la cookie,
  pero no puede leerla ni fijar la cabecera.

El propio login está protegido: el *login CSRF* también es un ataque real. El
token se rota al iniciar sesión, anulando cualquier token fijado previamente.

### Cookies

| Cookie | Contenido | Atributos |
|---|---|---|
| `greda_access` | Access token de Supabase | `HttpOnly`, `Secure`*, `SameSite`*, `Path=/` |
| `greda_refresh` | Refresh token de Supabase | igual, con vida más larga |
| `greda_csrf` | Copia del token CSRF | igual |

\* configurables por entorno. Escenarios previstos:

| Escenario | `COOKIE_SECURE` | `COOKIE_SAMESITE` |
|---|---|---|
| Desarrollo local (`http://localhost`) | `false` | `lax` |
| Producción same-site (`app.dominio` / `api.dominio`) | `true` | `lax` |
| Producción cross-site (dominios `*.run.app` distintos) | `true` | `none` |

La arquitectura preferida es `app.<dominio>` y `api.<dominio>` bajo el mismo
sitio, que permite `SameSite=Lax`. Cualquier debilitamiento debe ser explícito y
documentado; la configuración nunca se degrada en silencio.

### Formato de errores

```json
{ "error": { "code": "AUTH_INVALID_CREDENTIALS", "message": "Credenciales inválidas" } }
```

Los errores de validación añaden `details` con el campo y el motivo, **nunca** el
valor enviado. En ninguna respuesta aparecen trazas de pila ni detalles internos.

| Código | HTTP | Situación |
|---|---|---|
| `AUTH_INVALID_CREDENTIALS` | 401 | Credenciales incorrectas |
| `AUTH_NOT_AUTHENTICATED` | 401 | No hay sesión |
| `AUTH_SESSION_EXPIRED` | 401 | Token vencido o revocado |
| `AUTH_PROFILE_NOT_PROVISIONED` | 403 | Usuario sin perfil en la aplicación |
| `AUTH_ACCOUNT_INACTIVE` | 403 | Perfil desactivado |
| `AUTH_INSUFFICIENT_ROLE` | 403 | Rol sin permiso para la operación |
| `CSRF_TOKEN_MISSING` / `CSRF_TOKEN_INVALID` | 403 | Fallo de protección CSRF |
| `VALIDATION_ERROR` | 422 | Datos de entrada inválidos |
| `SERVICE_UNAVAILABLE` | 503 | Dependencia requerida no configurada |
| `UPSTREAM_AUTH_ERROR` | 502 | Supabase inalcanzable o con respuesta inesperada |

### Precisión numérica

El dinero y los costos usan `Decimal` en Python y `NUMERIC` en PostgreSQL;
`float` queda descartado para cálculos monetarios oficiales. La convención está
en `app/core/precision.py`. La Fase 1 no crea tablas de costos.

### Correlativos

El Plan v1.2 establece correlativos backend-only para documentos. **La Fase 1 no
implementa `CTZ` ni `HR`.** Queda fijada la restricción arquitectónica: todo
correlativo futuro se generará transaccionalmente en el backend y jamás en el
frontend.

---

## Endpoints de la Fase 1

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/live` | Liveness. Confirma que el proceso responde. |
| `GET` | `/ready` | Readiness. Verifica configuración, Supabase y base de datos. |
| `GET` | `/api/v1/auth/csrf` | Emite un token CSRF. |
| `POST` | `/api/v1/auth/login` | Inicia sesión y emite las cookies. |
| `POST` | `/api/v1/auth/refresh` | Renueva la sesión desde la cookie de refresh. |
| `POST` | `/api/v1/auth/logout` | Cierra la sesión y borra las cookies. |
| `GET` | `/api/v1/auth/me` | Sesión actual. Única fuente de verdad para React. |

---

## Docker

```bash
docker build -t bgreda .
docker run --rm -p 8080:8080 --env-file .env bgreda
```

La imagen está orientada a Cloud Run: escucha `PORT`, corre como usuario sin
privilegios (UID 1001), instala desde `uv.lock` y no incluye `.env` ni tests.

---

## CI

`.github/workflows/ci.yml` se ejecuta en cada PR y en `main`:

1. `uv sync --frozen` — falla si el lockfile no coincide con `pyproject.toml`
2. `ruff check` y `ruff format --check`
3. `mypy`
4. `pytest` con cobertura
5. Renderizado del SQL de las migraciones
6. Construcción de la imagen Docker, arranque del contenedor, verificación de
   `/live` y comprobación de que el proceso no corre como root

Ningún paso usa `continue-on-error`.
