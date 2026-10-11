# NEXO Stage 15B.3B-1 QA: Authentication Core Security Validation

- Estado: auditoría aislada terminada y verificada; consolidación QA local
- Commit de consolidación: `43fcbf5` (`test: validate isolated administrative authentication core`)
- HEAD observado al inicio: `7e861b27bc2fd18f3f686e704b61ff52d13745f4`
- Rama: `main`
- Stage 15B.3B-2: **NO-GO para implementación; permanece bloqueada**
- Stage 15C: bloqueada
- No hubo acceso, prueba ni afirmación sobre el esquema/infraestructura productivos

## Resultado ejecutivo

El commit `43fcbf5` consolida los cuatro archivos de Stage 15B.3B-1. El core se mantiene fuera de `app/` y no autentica ni protege rutas NEXO. Las pruebas se ejecutaron exclusivamente contra archivos SQLite temporales y passphrases `FAKE_*`.

La auditoría encontró que las solicitudes recibidas durante un lockout escribían un evento SQLite y un log por intento; un atacante podía hacer crecer el ledger/log aunque el KDF ya estuviera bloqueado. Se cambió el comportamiento para no escribir/loguear por cada petición ya bloqueada, y la prueba confirma que el ledger no crece durante esos intentos. También se añadió validación `failure_window >= 1`, prueba para impedir desactivar el limitador mediante una ventana inválida, y test de que variar el username no evita el contador global. Se reejecutaron focal y quality gate con estos cambios antes de consolidarlos.

## Estado Git y alcance

- El preflight confirmó HEAD esperado `7e861b27bc2fd18f3f686e704b61ff52d13745f4`, rama `main`, worktree con los cuatro archivos de Stage 15B.3B-1 sin seguimiento y sin otros cambios, y stash `stash@{0}: On main: checkpoint: preserve pre-alignment worktree cce925b7`.
- Tras revisar y ejecutar el gate, los cuatro archivos se consolidaron en `43fcbf5`. No se hizo push ni despliegue.
- No se encontraron credenciales reales en los cuatro archivos. Los valores sensibles de fixture son sintéticos `FAKE_*`. La búsqueda de patrones de keys/bearer no encontró matches y la lectura confirmó que las pruebas no cargan configuración de proveedores.
- `app/` no importa `prototypes.admin_auth` ni `prototypes.admin_auth_http`; el router se monta sólo en una app FastAPI efímera de pruebas. No hay middleware activo.
- El core rechaza `:memory:`, nombres resueltos `nexo.sqlite3`, la ruta NEXO calculada desde `NEXO_DATA_DIR` y symlinks para su DB. El código de QA no abrió la DB de NEXO, no ejecutó `app.migrations.migrate()` y no modificó runtime, rutas, proveedores, AgentRuntime, datos, migraciones, frontend o despliegue.
- El commit de QA separado incluye únicamente los tres paths ajustados (`prototypes/admin_auth.py`, `tests/test_stage15b3b1_auth_core.py`, `docs/stage15b-3b-1-authentication-core.md`) y este informe; no incluye el CLI ni el informe 15B.3B-1R. El stash sigue intacto.

## Arquitectura auditada

El core usa `AdminAuthStore` con conexiones SQLite por operación y `BEGIN IMMEDIATE` para transacciones de bootstrap, login, validación/revocación, rotación CSRF y recovery. Mantiene un solo administrador; crea sesiones con token aleatorio de 256 bits y guarda sólo SHA-256 del token; CSRF tiene valor aleatorio separado y se almacena como hash asociado a la sesión. Autorización de usuario usa un set cerrado de permisos; `authorize` deniega permiso desconocido. Los errores de SQLite/IO se traducen a `AuthUnavailable` (HTTP 503 en el adaptador), sin fallback abierto.

Las tablas aisladas son `auth_schema_version`, `auth_state`, `auth_admin`, `auth_sessions`, `auth_login_guard` y `auth_security_events`. La DB se fuerza a modo POSIX `0600` antes y después de abrirla. Esto protege el fichero frente a otros usuarios locales bajo permisos POSIX normales, pero no separa procesos con el mismo UID, no protege de root/Portainer owner ni cifra backups.

### Persistencia: alternativas

| Criterio | A. SQLite principal de NEXO | B. SQLite independiente |
|---|---|---|
| Transacción y autoridad | Puede comprobar sesión/permiso y ownership de conversación/run/notebook en la misma conexión/operación; apropiado para una sola app | Login y autorización quedan en un fichero/transaction distinto de la acción sobre recursos; revocación y mutación de dominio no son atómicas entre DBs |
| Backup/restore | Un snapshot coherente contiene auth y datos ya guardados; no añade otro artefacto operacional | Restore debe coordinar versiones/datos de ambas DBs; un backup antiguo de auth podría revalidar sesiones, uno incoherente puede impedir login |
| Separación | Separación lógica por tablas/permisos; comparte modo de fichero, proceso, UID y dominio de backup. No cifra secretos/DB | Puede tener path/ACL/backup propios. Si el mismo proceso/UID puede abrir ambos, es separación operativa, no frontera frente a compromiso de NEXO |
| Migración | Requiere migración versionada y pruebas legacy/rollback; esquema queda junto al dueño de recursos | Evita cambiar `schema_migrations` de NEXO, pero requiere ruta/configuración, inicialización, permisos y runbook separados |
| Complejidad | Menos moving parts, encaja con único servicio y futura propiedad por recurso | Más administración de dos stores, consistencia, restore, lockouts y health checks |

**Recomendación: A para la integración de una instancia NEXO de proceso/servicio único**, con tablas nuevas, integridad referencial y validación de sesión/ownership dentro de transacciones sobre la DB principal. Así el backup/restauración y la autoridad de acceso comparten un estado canónico. La separación lógica no es seguridad de filesystem; el backup de la DB debe cifrarse/protegerse porque la base actual también contiene datos y secretos sensibles.

B se justificaría si el auth broker pasa a proceso/UID separado con ACL efectiva, necesidad real de dominio de backup distinto o un operador exige custodia independiente. Separar sólo el archivo mientras el runtime/root puede abrirlo añade dual-DB consistency y no mitiga el compromiso del proceso. La DB independiente actual es sólo fixture de prototipo, no una decisión de persistencia productiva.

## Migraciones observadas y recomendación

La fuente actual del repositorio (`app/migrations.py`) enumera hasta `_migration_28`; `schema_migrations.version` es PK y `migrate()` aplica cada versión pendiente en orden. La inspección del repositorio **no verifica la versión de una instancia productiva**. No se crea ni ejecuta migración en esta etapa. No debe asumirse que la DB productiva está en versión 28 ni que se usó exactamente este HEAD.

Antes de migración futura: comprobar la versión de la copia de DB real bajo un procedimiento autorizado; backup consistente y restore probado; revisar colisión con `MIGRATIONS`; diseñar migración aditiva sin alterar conversaciones/notebooks/agentes/providers; asignar histórico al admin sólo con aprobación; revocar/cancelar sessions/runs/approvals no verificables; probar DB fresca y fixture legacy. No reservar `29` hasta volver a inspeccionar HEAD y estado real.

Observación importante: el runner actual aplica migraciones secuencialmente, pero varias migraciones usan `executescript`, que tiene semántica de commit implícito SQLite; no debe asumirse atomicidad de toda la cadena. La migración auth futura debe definir y probar explícitamente su transacción/rollback y orden de fail-closed, o mejorar el mecanismo migratorio en un alcance separado. Una migración parcial no debe dejar APIs protegidas accesibles sin guard.

## Bootstrap y recuperación

- Primer alta: `bootstrap_from_console()` llama `initialize()`, obtiene username por prompt y passphrase dos veces mediante `getpass`, valida igualdad constante y `bootstrap_local()` crea un único administrador en transacción. El estado `initialized` y PK singleton impiden segundo bootstrap incluso después de reinicio. No existe route de bootstrap en el router.
- Reinicio: auth state y sesiones sobreviven al recrear `AdminAuthStore` apuntando al mismo archivo; no se bootstrappea automáticamente ni se degrada a anónimo. La verificación se hizo sobre SQLite temporal.
- Segundo bootstrap: devuelve `BootstrapComplete` y no modifica el admin existente.
- Contraseña perdida: el método `reset_password_local()` actual sólo escribe hash nuevo, revoca sesiones globales, borra contador de lockout y registra evento. **No es todavía una operación operativa protegida por el host**: no hay CLI instalado, autorización del operador, confirmación de instalación/path, ni guard que impida a código Python invocarlo. No exponerlo como endpoint.
- Procedimiento recomendado para implementación: operador con identidad administrativa del host/Portainer y acceso al volumen; restringir/cerrar ingress y detener el servicio para recovery; identificar/verificar el path de DB contra la instancia; realizar backup consistente y comprobar que es restaurable; ejecutar comando local autenticado por permisos del host que prompt-ee dos veces sin eco; actualizar hash y revocar sesiones/CSRF en una única transacción; guardar evento local no secreto; reiniciar con auth fail-closed y verificar login antes de reabrir ingress. No password en argv/env/log, no cuenta break-glass universal.
- DB ausente/corrupta/permiso denegado: fail closed; no recrear admin automáticamente ni iniciar app sin guard. Recuperar desde backup bajo procedimiento aprobado. Si no hay backup recuperable, reparar offline con operador autorizado, nunca por endpoint remoto. El método `bootstrap_local` no debe usarse para reemplazar credenciales.
- Modo archivo: el test comprueba `0600` y que un fichero existente más permisivo se vuelve a restringir; la operación depende de semántica POSIX. Directorio, snapshots y backups requieren controls independientes.

## scrypt y limitación de intentos

- Configuración: scrypt `N=16384, r=8, p=1, dklen=32`, sal aleatoria de 16 bytes, versión `1`, comparación `hmac.compare_digest`. Memoria de trabajo nominal `128*N*r` = **16 MiB** por operación, más overhead del proceso. No se redujeron parámetros para tests.
- Medición local sintética en macOS 27.0.1 arm64/Python 3.13: mediana **66.14 ms/hash**, cinco batches de cinco invocaciones; extremos de batch **41.13–98.19 ms/hash**. No es benchmark del hardware productivo y puede cambiar por build/OpenSSL/contención.
- Login ejecuta la derivación bajo `BEGIN IMMEDIATE` para serializar el contador compartido entre requests/procesos que usan la misma SQLite. Ocho logins concurrentes pasan en el test; cada hash retiene el write lock, por lo que aumenta colas. SQLite busy timeout es 5 s; contención/DB ocupada produce error 503 fail-closed, no auth permisiva. No soporta varios hosts/filesystem de red.
- Rate limit persistente singleton: defaults 5 fallos/900 s y lock 300 s; credenciales incorrectas y usernames alternos comparten cuota; username desconocido no tiene un bucket distinto que permita evadirla. Ventana inválida <=0 se rechaza. Tras lock no se vuelve a ejecutar scrypt ni se añade un evento/log por cada intento; el evento de transición a lockout permanece. Los fallos previos al lock generan filas de auditoría acotadas por la propia política.
- Residual: bloqueo global es una superficie de DoS contra el único admin; cualquier cliente que alcance login puede consumir los intentos. Sin IP bucketing se evita confiar en forwarded headers no confirmados, pero no hay discriminación de redes. MFA, ingress privado/rate limiting del proxy y alerta operativa siguen pendientes. Límites configurables excesivos podrían causar esperas; calibrar hardware antes de producción.

## Identidad de servicio

El contrato actual separa `Principal(kind="user"|"service")`, permiso humano y scopes. Una identidad `user` no pasa `expected_kind="service"`; el factory permite sólo los scopes nominales configurados. **No autentica el proceso ni impide falsificación**: `Principal` es un dataclass construible y `service_principal(service_id, scopes, delegation_id)` es una función pública que cualquier caller dentro del proceso puede invocar con un ID/scope permitido. La prueba de tipos/scopes sólo prueba policy; no prueba identidad. El mismo proceso comprometido puede ignorar `authorize()`.

Contrato recomendado para futura integración (no implementado):

1. La entrada humana valida cookie/sesión y permiso de operación; emite una delegación de vida breve ligada a `user_id`, session/revision, conversation_id, run_id, service audience, scopes exactos y tool allowlist del snapshot.
2. El runtime sólo recibe esa delegación opaca/verificable; no recibe credencial humana ni `system:admin`. Cada uso verifica firma/issuer/audience/expiry, revocación, run activo, scopes, provider/tool allowlist y vinculación conversation/run; rechaza campos emitidos por el caller.
3. Delegación expira al terminar el run y se revoca al logout/reset/invalidación de run. Mantener revocación one-shot/transaccional para approval, y revalidar el permiso humano/dueño antes de approval/resume; UUID de interacción no es bearer capability.
4. Si el threat model incluye código Python/host comprometido, separar proceso/UID y canal IPC autenticado (por ejemplo socket Unix con peer credentials + protocolo estrecho) o identidad de workload equivalente. Firma en el mismo proceso ayuda contra confusiones/errores accidentales, pero no defiende frente a ejecución arbitraria/root del mismo proceso.

**Resultado QA:** la separación actual es tipado y deny-by-default para permisos, pero no autentica una identidad de servicio y no ofrece autoridad delegada. No integrar AgentRuntime ni tools con esta API hasta que el mecanismo y frontera estén diseñados/testeados.

## HTTP, cookies y proxy

- Cookie se comprueba con TestClient HTTPS: `__Host-nexo_session`, `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, no Domain; token no aparece en JSON; logout envía expiración.
- `POST login`, `POST csrf` y métodos mutantes requieren `Origin` https exacto; todas las operaciones verifican Host configurado y `request.url.scheme == https`. Origin/Host inválidos o HTTP devuelven 400. No existe fallback HTTP local que quite Secure.
- CSRF aleatorio va en respuesta de login/endpoint POST de rotación, se compara por hash y se exige en `X-CSRF-Token` para mutaciones. POST csrf valida Origin para no permitir que un origen cruzado rote el token.
- Anónimo/revocado/expirado -> 401; permiso/CSRF -> 403; DB fallida -> 503. Mensaje de credenciales genérico y no refleja password/DB error. El router de test no se monta en NEXO.
- El adaptador no lee `Forwarded` ni `X-Forwarded-*`; el esquema se toma del ASGI `request.url`. Detrás de TLS termination, proxy debe crear scope scheme/Host correctamente sólo si Uvicorn confía en dirección proxy explícita y headers saneados. Esta confianza es configuración externa que no se ha probado. No asumir proxy/Portainer productivo; no se modificó Compose.
- Riesgo residual: probar el camino real proxy->ASGI, cookies same-origin móvil, redirect HTTP sin credenciales, trusted host, IP/CIDR y cabeceras spoof antes de activar. El contrato es un fixture, no evidencia de TLS/HTTPS real.

## Resultados de pruebas

- Prueba focal actual `.venv/bin/python -m unittest tests.test_stage15b3b1_auth_core -v`: **20 tests, OK**.
- Regresión combinada `.venv/bin/python -m unittest tests.test_stage15b3b1_auth_core tests.test_stage15b3a_admin_security -v`: **24 tests, OK**.
- `./scripts/nexo check`: **282 passed, 1 skipped**, regresiones frontend OK.
- Warnings no bloqueantes: FastAPI `on_event`, alias Starlette `BlockingPortal`, advertencia Node de module type en tres assets. Sin errores de test.
- `git diff --check`: OK; archivos nuevos fueron verificados también con `git diff --no-index --check /dev/null <file>`.
- No se hizo smoke contra servicio, migración, petición a NEXO, conexión a proveedor, push o despliegue.

## Riesgos residuales y decisión

El commit `43fcbf5` contiene el core base. La corrección QA del spam durante lockout, su prueba y este informe se consolidan en un commit local separado, tras repetir las pruebas y el quality gate. No se incluye CLI ni cambios de integración.

**Persistencia recomendada para integración: SQLite principal A**, bajo nueva migración probada con backup/rollback; no crear/ejecutar aquí. **NO-GO para iniciar implementación Stage 15B.3B-2** hasta que el operador apruebe A vs B y su runbook/backup, el recovery local sea operable y revisado, se defina Proxy/origen, y se diseñe delegación de servicio con la aclaración de que el proceso actual no provee frontera efectiva. GO sólo para revisar el diseño y este QA. Stage 15C continúa bloqueada.
