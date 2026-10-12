# NEXO Stage 15B.3B CP2-B.2a: Administrative Schema Foundation

- Preflight: `HEAD=1b72702b48785731d7701d4f4b4ff9b3df45c06c`, rama `main`, worktree limpio (ahead 16); stash `b7d2662a55bb2519aadd4291a93400c689afb46c` presente e intacto.
- Resultado: esquema v29 aplicado transaccionalmente sólo a SQLite temporal; no bootstrap, usuarios, sesiones productivas, permisos concedidos ni autenticación activa.
- Producción y exposición: **NO-GO**. Entrega en commit local para revisión.

## Modelo

La migración 29 añade `auth_state`, `auth_users`, `auth_user_permissions`, `auth_sessions`, `auth_login_guard` y `auth_security_events`. El estado inicial singleton es `schema_installed`; `bootstrap_pending` y `bootstrap_completed` son estados futuros que no activan auth por sí mismos, pues no existe enforcement HTTP. `bootstrapped_at` debe ser NULL en los dos primeros y no NULL en el último. El guard runtime rechaza estados desconocidos/inconsistentes; `bootstrap_completed` requiere usuario activo, grant explícito y un evento de bootstrap relacionado. No hay estado incompatible persistido: ledger futuro, objeto parcial o definición distinta se rechazan.

`auth_users.id` es PK estable; `username_normalized` es único; `password_hash` no nulo; `status` sólo admite `active`/`disabled`. Fechas son epoch REAL como en el prototipo de persistencia. No se crea usuario.

`auth_user_permissions` usa PK `(user_id, permission)`, FK `ON DELETE CASCADE`, permiso no vacío y sin `*`; índice `idx_auth_user_permissions_permission(permission,user_id)`. La migración no crea grants.

`auth_sessions.token_hash` y `csrf_hash` son BLOB de 32 bytes, nunca tokens en claro; usuario FK cascade; restricciones de expiración ordenada y revocación posterior a creación. Índices `idx_auth_sessions_user(user_id,revoked_at)` y `idx_auth_sessions_expiry(revoked_at,idle_expires_at,absolute_expires_at)`. No se crean sesiones.

`auth_login_guard` almacena sujeto, contador no negativo, comienzo de ventana y fin de bloqueo no negativo. Se crea sólo el bucket vacío `administrator` con contador/tiempos en cero, conforme a la semántica del prototipo; la lógica de ventanas/lockout no se integra.

`auth_security_events` restringe event type a la allowlist del prototipo, actor nullable con FK `ON DELETE SET NULL`, timestamp y correlación opcional; índice `idx_auth_security_events_created(created_at,id)`. Dos triggers rechazan `UPDATE` y `DELETE`, implementando append-only frente a DML ordinario en SQLite. Un actor con permiso de cambiar schema o borrar/recrear triggers puede eludirlo: no es garantía de seguridad contra propietario de la base, filesystem o proceso. Retención/privilegios de aplicación quedan pendientes.

## Migración Y Runtime

`app.safe_migrations.TRANSACTIONAL_MIGRATIONS` registra sólo v29, después de verificar que v28 es la última histórica en el repo. Usa statements individuales `execute()`, `BEGIN IMMEDIATE`, validación exacta del manifest de entrada/salida, `integrity_check`, `foreign_key_check`, ledger y DDL dentro de la misma transacción. Cualquier excepción antes de commit hace rollback; `executescript()` no se usa en esta unidad. El migrador offline CP2-B.1 ejecuta la nueva migración en staging y sólo promueve el archivo validado. La migración histórica 1–28 no se modificó.

El manifest v29 incluye los objetos administrativos exactos y contempla la extensión histórica conocida `user_preferences`; drift, objetos inesperados, columnas o índices faltantes y versiones futuras fallan cerrados. Runtime sigue aceptando el manifest íntegro v28 para compatibilidad con la app previa y v29 exacto; no migra en startup ni en una petición. No se añadieron rutas/middleware, el import/runtime no crea identidad ni completa bootstrap y las rutas conservan su comportamiento anónimo actual. La validez de v29 **no** habilita exposición.

No se duplicó `auth_schema_version`: la versión administrativa es el ledger único `schema_migrations.version=29`. El prototipo 2B.2 lo mantenía como versión propia del store aislado; duplicarlo aquí crearía dos autoridades de versión.

## Compatibilidad De Prototipos

`admin_persistence.py` es el referente más próximo y coincide en nombres de tablas/índices, formato de hashes, relaciones, allowlist de eventos, sujeto `administrator` y bucket vacío. Diferencias deliberadas: su `auth_state.initialized` booleano se reemplaza por estados enumerados porque no distingue instalación de bootstrap pendiente; su `auth_schema_version` aislado se reemplaza por ledger v29; aquí el timestamp inicial de estado es NULL hasta bootstrap; y los triggers append-only se añaden para documentar/mejorar la garantía SQL. El prototipo comprueba consistencia semántica de actor/grants/bootstrap; v29 sólo fija estructura e invariantes locales, sin identidad ni eventos de bootstrap. La forma de sesión preserva hashes, expiry y `revoked_at`; `auth_login_guard` conserva contador, inicio de ventana, bloqueo y bucket durable con reset a cero. Pruebas sintéticas verifican que un estado completado coherente, sesión revocada y ventana/bloqueo no se pierden ni se rechazan por el validador. No se implementa la operación de revocación/login/rate-limit.

`admin_auth.py` y `admin_auth_http.py` son del store aislado anterior (`auth_admin`, esquema propio y su propio estado/sesiones), no tienen compatibilidad de esquema con v29. No se renombra ni migra ese modelo y no se conecta ninguno de los tres prototipos al runtime.

## Verificación Y Recuperación

Pruebas nuevas: `tests/test_cp2b2a_administrative_schema.py` cubre migración fresh y v28, estado vacío, ausencia de usuarios/grants/sesiones/eventos, unicidad, FKs, estados desconocidos/inconsistentes, estado completado sintético, revocación y rate-limit persistidos, fallo antes del ledger, SIGKILL antes y después del commit de la transacción staging y retry. Cada SIGKILL reabre e inspecciona staging: manifest, `integrity_check`, `foreign_key_check`, ledger completo y fila sintética de provider; antes del commit confirma rollback a v28 sin `auth_state`, y después confirma v29 completo sin identidades. Las pruebas CP2-B.1 comprueban lo mismo sobre la generación canónica tras SIGKILL de promoción antes/después del replace. El kill precommit deja canónica v28; postcommit de transacción deja también canónica v28 porque sólo se confirmó staging privado. El retry promueve v29 completo. No se simula pérdida eléctrica/filesystem.

Resultados de esta revisión: suite focal CP2-B.2a + CP2-B.1 + CP1: **44 passed** (5 warnings deprecación Starlette/FastAPI); `./scripts/nexo check`: **409 passed, 1 skipped**, exit 0 (mismos warnings Python y tres avisos Node `MODULE_TYPELESS_PACKAGE_JSON`). `git diff --check` y whitespace de los archivos nuevos: sin diagnósticos. No se simula pérdida eléctrica/filesystem.

## CP1 Y Límites

La evidencia `runtime-verified` ya asignada a ledger/schema/versión futura/restore sigue limitada a motor y `app.main` con SQLite temporal. Esta fase añade evidencia runtime local de aplicación transaccional v29. `bootstrap_requires_verified_schema_and_backup` permanece `prototype-verified`: no hay bootstrap integrado. `storage_failure_has_no_anonymous_fallback` permanece `prototype-verified` respecto a auth, que sigue ausente. Nada es `operational-verified`.

No se abrió SQLite real, no se inspeccionó producción, no hubo credenciales reales ni cambios de datos productivos. No se ejecutó login, middleware, endpoint administrativo, bootstrap, ownership histórico, OAuth, SIWC ni operación de despliegue. SQLite temporal no prueba Portainer, filesystem, compatibilidad de la SQLite del contenedor, energía/host crash, backup cifrado/custodia, exposición segura o protección HTTP.

## Pendiente Para CP2-B.2b

Definir y revisar transición autorizada `schema_installed -> bootstrap_pending -> bootstrap_completed`; operación host-side con TTY/path/UID/backup y recovery; semántica ejecutable de ventanas/reset de lockout; hashing KDF y política de login; catálogo/allowlist final de permisos; reglas de auditoría, retención y control de triggers; comportamiento de sesiones; y pruebas integradas de storage failure. Ownership histórico y asignación de preferencias siguen expresamente fuera de esta fase.

Condiciones de activación aún no satisfechas: bootstrap autorizado y probado, usuario/grants explícitos, ownership resuelto, catálogo HTTP completo con guard efectivo, CSRF/Host/Origin, protección pre-body y de side effects/SSE, recuperación host-side, backup cifrado/restore y evidencia operacional de proxy/TLS/ingress. No iniciar CP2-B.2b, CP3 ni Stage 15C como consecuencia de esta entrega.
