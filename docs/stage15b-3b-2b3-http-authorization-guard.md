# NEXO Stage 15B.3B-2B.3: Isolated HTTP Authorization Guard

- Modalidad: prototipo aislado, FastAPI sintética y SQLite temporal
- HEAD de preflight: `fa0128a5d5de8e586885860e3d98aebca473b3d7`, rama `main`
- Runtime, migraciones, frontend y configuración productiva: sin cambios
- Decisión: **GO limitado para prototipo; NO-GO para integración, activación o producción**

## Arquitectura

`HttpAuthorizationGuard` es middleware ASGI externo a la aplicación FastAPI sintética. Evalúa el inventario antes de invocar el router: registro/policy snapshot, identidad/sesión, Host/Origin/CSRF, permiso, resolver de ownership y finalmente endpoint/dependencias/body. No depende del orden de `Depends`; una denegación no consume el body ni ejecuta endpoint, dependencias, almacenamiento o productor.

El constructor no autoriza tráfico. Se debe llamar `validate()` tras registrar todos los routers, mounts y rutas framework. La validación usa `inspect_routes(..., strict=True)` del Stage 2B.1. Cada request compara el fingerprint actual y responde 503 si no existe snapshot, la validación falló o el registro cambió. Debe validarse de nuevo deliberadamente después de completar el registro; no hay readiness/lifespan integrado en NEXO.

El inspector aporta método por ruta, Mount, rutas anidadas inspeccionables, framework, duplicados, overlaps, política faltante y drift. El guard hace matching Starlette en el orden real. Políticas de Mount y leaf se acumulan. Las rutas no coincidentes `/api/*` dan 404; un catch-all SPA `/{path:path}` no puede servir HTML para una ruta API desconocida. Un Mount estático sólo protege su boundary; no se enumeran archivos internos.

## Políticas E Identidad

Se reutiliza `AccessPolicy` de `http_policy_inventory.py`: `public`, `user(permission)`, `admin(permission)` y `service(scope, audience)`. La declaración es por método y `validate()` falla ante una política incompleta, método no declarado, duplicado o overlap error. No se infiere acceso por prefijos.

`user` y `admin` validan cookie `__Host-nexo_session` usando `authenticate()` del store; esto consulta sesión, estado vigente y grants actuales en cada request. El principal humano no puede satisfacer `service`. El store administrativo actual sólo ofrece el principal humano del administrador bootstrap; como no hay una autoridad de roles reutilizable, `admin` además exige `admin_verifier` confiable inyectado. Sin él, deniega 403.

`service` requiere `service_verifier` inyectado y un `ServiceCredential` validado por ese componente: principal `service`, scope y audience exactos. Un header por sí solo no demuestra identidad. Este callback es un seam sintético para pruebas, no una verificación criptográfica de workload.

La persistencia usa `AdministrativePersistence` con SQLite temporal explícita y su contrato existente de hash de token/CSRF, expiración idle/absoluta, revocación, permisos y transacciones. El prototipo no implementa criptografía nueva ni crea credenciales reales. Errores de almacenamiento, verificador o resolver se normalizan a 503 sin causa/SQL/path en la respuesta.

## Ownership Y Escrituras

Toda política humana necesita un `ownership_resolver` inyectado; su ausencia devuelve 503 y el resolver puede devolver falso para 404 indistinguible (ajeno/inexistente). El middleware nunca usa un ID de URL como prueba. Errores de almacenamiento del resolver fallan 503.

El resolver sólo es una comprobación sintética previa. No puede prometer atomicidad con una mutación posterior genérica. En integración, cada write debe filtrar owner y cambiar el recurso en la misma transacción o con CAS/precondición SQL; callbacks de ownership separados de una escritura dejan TOCTOU. La prueba cubre propio/no-propio sintético y error, no una mutación de dominio real ni carrera concurrente de ownership.

## CSRF, Origin Y Cookies

Cada solicitud humana exige HTTPS según el scope ASGI y Host exactamente igual a `allowed_origin`; GET permite Origin ausente, pero si está presente debe coincidir. Métodos inseguros exigen Origin exacto y `X-CSRF-Token`, validado por el store con el hash de sesión. Se rechazan content types no admitidos antes de body parsing (JSON y `+json`, form-urlencoded, multipart, text/plain y octet-stream son aceptados). Servicio sin cookie no usa CSRF browser.

Cookie prevista: `__Host-nexo_session; Secure; HttpOnly; Path=/; SameSite=Lax`, sin `Domain`; el core HTTP existente aplica esos atributos. El guard no habilita CORS. Los tests no relajan HTTPS/Origin: usan base URL HTTPS sintética.

## SSE Y Revocación

El middleware autentica antes de invocar el endpoint, por lo que sin sesión, permiso, CSRF u ownership no se crea `StreamingResponse` ni corre el productor. Si la respuesta es `text/event-stream`, vuelve a autorizar inmediatamente antes de enviar `http.response.start`; una revocación entre la autorización inicial y ese punto reemplaza el inicio por respuesta HTTP 401/403/503 sin consumir el generador.

Después del inicio del stream no se promete interrupción instantánea. El productor debe llamar `authorize_effect(request, policy)` antes de cada efecto humano, o `authorize_service_effect(request, policy)` para cada efecto delegado. La prueba `test_sse_revocation_between_protected_effects_prevents_second_effect` ejecuta un `StreamingResponse` real del fixture, confirma el primer efecto, revoca la sesión y verifica que el segundo efecto no ocurre. La revocación es **cooperativa**: sólo detiene efectos futuros cuando el productor alcanza el siguiente boundary de autorización; no borra efectos ya confirmados ni interrumpe código bloqueado entre verificaciones. Fallos de almacenamiento también impiden el siguiente efecto. No hay cancellation manager ni finalización durable de runs en este prototipo.

## Servicio Y Delegación

`ServiceCredential` conserva principal de servicio, scopes y audience. La delegación opcional registra owner, scopes acotados, expiración y estado revocado. Sus scopes deben ser subconjunto de los scopes del servicio; cada verificación de efecto vuelve a consultar el verificador y el resolver recibe la credencial/delegación para revisar ownership. No se heredan grants humanos. Tests sintéticos cubren scope/audience, expiración, revocación y no ampliación.

No hay emisor/verificador, almacén de delegaciones revocables ni canal entre procesos. Un callback en proceso no constituye autenticación de servicio ni aislamiento frente a compromiso del proceso. Propagar actor/sesión/run y garantizar revocación durable requiere diseño posterior, sin ampliar aquí la arquitectura delegada.

## Matriz Probada

| Caso | Resultado observado |
|---|---|
| Inventario no validado, policy faltante o ruta añadida tras snapshot | 503 / fallo de validación; handler bloqueado |
| Método HTTP y policies por método | Inventory strict exige cobertura exacta; endpoint sólo se invoca tras policy |
| Mount estático, catch-all SPA y `/api/*` desconocida | Mount explícito; asset público; unknown API 404 sin HTML |
| Anónimo, sesión revocada, permiso ausente | 401 / 403; sin dependencia, body, handler o productor |
| CSRF ausente/inválido, Origin ausente/no permitido, Host inesperado | 403 CSRF; 400 Origin/Host |
| Content-Type `text/html` con mutación | 415 antes del handler |
| Ownership ajeno/inexistente y error de almacenamiento | 404 oculto / 503 genérico |
| Principal de servicio, scope/audience y delegación | Verificador inyectado; 401/403 si falta o no coincide |
| SSE y revocación antes del inicio efectivo | Generador no ejecutado; nueva autorización bloquea headers |
| Revocación dentro de stream | Helper de efecto niega la siguiente operación; no promete cortar una operación ya iniciada |

Los datos, usuario, contraseña, tokens, delegación, recurso y respuestas son sintéticos. No hay conexiones externas, llamadas a proveedores, tools ni lectura de archivos reales. No se crean conversaciones ni runs porque no existe endpoint de dominio en el fixture; los contadores prueban el orden previo a handler/dependencias/productor.

## Límites Y Riesgos Residuales

- El guard no se registra en `app.main` ni protege NEXO; el fallback real permanece intacto.
- Snapshot strict cubre inspección del inventario, no congela el router físicamente ni deshabilita lifespan/readiness. El caller debe validar en el punto correcto y mantener el proceso no disponible si falla.
- Starlette matching sintético no sustituye un censo de módulos/configuración de producción, WebSocket enforcement ni la inspección completa de rutas internas de `StaticFiles`.
- `admin_verifier`, `service_verifier` y ownership resolver son callbacks de confianza; código comprometido puede evitarlos. Service verifier usa una credencial de prueba no criptográfica.
- Ownership previo sin transacción/CAS con la escritura conserva TOCTOU; la garantía real debe vivir en el repositorio/operación SQLite.
- La nueva autenticación antes de SSE start y antes de cada efecto es cooperativa. No cancela instantáneamente productor/egress ni revierte efectos que ya comenzaron.
- Content-Type es una allowlist genérica; endpoints productivos requieren validación adicional por contrato (p.ej. JSON vs multipart), tamaños y validación de contenido.
- No se implementan origin/proxy trust de despliegue, CORS, login/bootstrap HTTP, recovery, rate limit HTTP, WebSocket enforcement, autorización de cada ruta productiva ni pruebas con proveedor real.

## Diferencias Frente Al Blueprint

El guard mínimo se instala como wrapper ASGI sintético, pero el blueprint exige integración atómica con ciclo de vida, todos los endpoints/módulos, ownership en operaciones SQLite, policies versionadas, readiness cerrada, auth state migrado, proxy/origin configurado y tests de side effects reales. El prototipo no implementa migración, no toca el fallback NEXO, no abre rutas administrativas productivas y no afirma autenticación entre procesos. La persistencia reutilizada sólo se crea dentro de `TemporaryDirectory`.

## Decisión

**GO** únicamente para evaluación de contratos aislados y pruebas locales del prototipo. **NO-GO** para integración productiva, protección activa, migración, credenciales reales, despliegue o apertura de tráfico. El blueprint 2A y los informes 2B.1/2B.2 siguen siendo vinculantes para gates posteriores. Stage 15B.3B-2B.4 y Stage 15C no se inician en este checkpoint.

## Verificación

Prueba focal `.venv/bin/python -m unittest tests.test_stage15b3b2b3_http_authorization_guard -v`: **14 tests, OK**. Evidencia puntual de este review:

- `test_denial_precedes_dependencies_body_parsing_and_handler`: **OK**; TestClient envía la petición ASGI, se deniega sin cookie y los contadores de dependencia, body, handler y efectos protegidos (SQLite de dominio, conversación, run, tool, provider, archivo y SSE) permanecen en cero.
- `test_late_route_registration_invalidates_snapshot`: **OK**; ruta tardía responde 503 y no ejecuta el handler; tras `guard.validate()` el mismo endpoint responde 200 y ejecuta el handler.
- `test_sse_revocation_between_protected_effects_prevents_second_effect`: **OK**; en un stream real del fixture el primer efecto se confirma, se revoca la sesión dentro del generador y la revalidación deniega el segundo. Garantía cooperativa, no interrupción instantánea.
- `test_service_identity_requires_injected_verifier_scope_and_audience`: **OK**; headers `x-service` y `Authorization` sin verifier inyectado responden 401. Un verifier sintético puede validar la credencial de fixture.
- `test_auth_storage_error_fails_closed_without_leaking_details`: **OK**; excepción del store de auth produce 503 sanitizado y el endpoint no se ejecuta. `test_ownership_is_hidden_and_storage_errors_fail_closed` comprueba además fallo del resolver.

Quality gate: `./scripts/nexo check` -> **343 passed, 1 skipped**, exit 0. Warnings: Starlette `BlockingPortal`, FastAPI `on_event` y tres avisos Node `MODULE_TYPELESS_PACKAGE_JSON`. `git diff --check` y `git diff --no-index --check /dev/null <archivo>` para los tres archivos: sin diagnósticos.

Preflight: `HEAD=fa0128a5d5de8e586885860e3d98aebca473b3d7`, rama `main`. Stash histórico continúa como `stash@{0}`, hash `b7d2662a55bb2519aadd4291a93400c689afb46c`; no se aplicó ni modificó. Los únicos paths del checkpoint son este informe, `prototypes/http_authorization_guard.py` y `tests/test_stage15b3b2b3_http_authorization_guard.py`; `app/`, `web/`, migraciones y configuración productiva permanecen intactos. No hay emisor de credenciales, migraciones ni runtime real en este alcance.
