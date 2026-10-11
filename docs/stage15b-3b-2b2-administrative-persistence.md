# NEXO Stage 15B.3B-2B.2: Administrative Persistence Preparation

- Modalidad: prototipo aislado, fixtures sintéticos y pruebas; sin integración de runtime
- HEAD de preflight: `9d0a95952e191121c0a9b0cfbc2bff9fc8d5b2d9`, rama `main`
- Worktree inicial limpio; stash histórico `stash@{0}` preservado
- Esquema del repositorio: migraciones 1–28; esquema productivo desconocido y no inspeccionado
- Resultado: GO limitado para revisión del prototipo; **NO-GO** para usarlo en producción o activar autenticación

## Diseño del adaptador

`prototypes/admin_persistence.py` prepara la forma de persistencia objetivo en una SQLite temporal explícitamente suministrada. No se importa desde `app/`, no contiene CLI ni rutas HTTP, y nunca deriva ni abre una ruta obtenida de `NEXO_DATA_DIR`. Sólo lee ese valor para rechazar candidatos dentro del data dir configurado. Rechaza `:memory:`, paths fuera de `tempfile.gettempdir()`, componentes symlink bajo el temp root y cualquier archivo llamado `nexo.sqlite3`. No crea directorios padre implícitamente. Todos los tests crean y destruyen sus bases dentro de `TemporaryDirectory`.

La capa reusa los helpers de contraseña `_password_hash` / `_verify_password` del core aislado para conservar el encoding scrypt versionado y no duplicar criptografía. No modifica `prototypes/admin_auth.py`, `admin_auth_http.py` ni `admin_auth_cli.py`. El core existente usa `auth_admin` y un store dedicado v1; este adaptador implementa el modelo futuro `auth_users` y rechaza esquemas `auth_*` parciales o incompatibles. No se debe abrir una DB del core antiguo con ambos stores ni inferir migración entre ellos.

Cada conexión habilita y verifica `PRAGMA foreign_keys=ON`, fija `busy_timeout`, y toda mutación usa `BEGIN IMMEDIATE` con commit/rollback explícito. Errores SQLite de almacenamiento/busy se normalizan a `PersistenceUnavailable`; conflictos de constraints a `PersistenceConflict`; esquema, ownership o versión incompatibles se rechazan mediante errores tipados sin incluir SQL, path ni credenciales. No se registran secretos ni causa cruda SQLite.

## Estructuras preparadas

`initialize()` crea los objetos administrativos en una sola transacción SQLite usando sentencias individuales `execute` (no `executescript`) o valida que el conjunto esté completo. Inicialización repetida es idempotente; un conjunto parcial nunca se “repara” rellenando tablas silenciosamente.

| Tabla | Restricciones implementadas | Relación/índices |
|---|---|---|
| `auth_schema_version` | Singleton `id=1`, versión positiva | Sólo versión 1 conocida; desconocida falla cerrado |
| `auth_state` | Singleton, `initialized` boolean, `policy_version`, timestamp condicionado por estado | `initialized=1` sólo tras usuario, grants, ownership exigible, auditoría y FK correctos |
| `auth_users` | PK texto, username normalizado único, hash, status active/disabled, timestamps | Estado inicial comprobado contra el actor del evento bootstrap; no se guarda password |
| `auth_user_permissions` | PK `(user_id,permission)`, permiso no vacío/sin `*` | FK user `ON DELETE CASCADE`, índice permission/user |
| `auth_sessions` | Hash token BLOB de 32 bytes como PK, hash CSRF de 32 bytes, expiries, revocación | FK user `ON DELETE CASCADE`; índices por user/revocación y expiración |
| `auth_login_guard` | PK subject, fallos no negativos, ventana y lockout no negativos | Un bucket durable singleton para admin MVP, no basado en IP |
| `auth_security_events` | Event type allowlisted, tiempo, correlation id opcional | FK opcional actor `ON DELETE SET NULL`; índice por tiempo/id; sin body ni credenciales |

No hay permisos wildcard ni autoridad por defecto. El bootstrap persiste cada grant del conjunto explícito permitido. El estado inconsistente falla: `initialized=0` no admite usuarios/grants/evento de bootstrap; `initialized=1` necesita fecha, actor bootstrap activo con grants válidos y ownership completo cuando el esquema tiene recursos privados.

## Compatibilidad con SQLite NEXO v28

`_validate_nexo_schema()` acepta una SQLite vacía para pruebas del store aislado o el ledger exacto 1–28 junto con tablas/columnas representativas requeridas, incluyendo columnas introducidas al final de la secuencia (`agent_profiles.max_tool_calls`, `mcp_tools.read_only_hint`, approvals/interactions). Ledger incompleto, estructura requerida faltante, versión desconocida/futura o filas con FK rota fallan antes de añadir auth. El adaptador revalida el ledger al iniciar cada transacción. No se inspeccionó ninguna DB productiva y no se asume que ésta tenga v28.

Los tests construyen fixtures con el `app.migrations.migrate()` **existente** en un path temporal y comprueban `MAX(schema_migrations.version)=28`; preservan datos sintéticos en providers, notebooks, conversaciones, mensajes y preferencias. El código de producción de `app/migrations.py` no se modificó ni se agregó migración.

El esquema v28 no contiene `owner_user_id`. Para ensayar el contrato futuro, el fixture añade `conversations.owner_user_id` y `notebooks.owner_user_id` como columnas experimentales con FK a `auth_users`; nunca se escriben esas columnas en `app/` ni en una DB NEXO real. Si existen tablas privadas sin esas columnas, bootstrap se rechaza. Si hay filas históricas con owner nulo, bootstrap requiere `assign_historical=True` explícito; la asignación y el alta de identidad/grants son una única transacción. Los mensajes, runs y recursos descendientes conservan su relación actual y heredan ownership por conversación/notebook.

La DB v28 se valida por versión y un conjunto representativo de tablas/columnas. Esto no es una inspección real ni un substituto de `sqlite_master`/FK/integridad sobre una copia autorizada de cada instalación. Esquema con columnas/constraints divergentes pero ledger falsamente completo requiere auditoría adicional antes de una integración.

## Operaciones y contratos

| Operación | Contrato comprobado |
|---|---|
| `initialize()` | Esquema nuevo completo, validación/idempotencia; no crea directorios ni migra una NEXO existente automáticamente |
| `bootstrap_admin()` | Normaliza username, valida grants de allowlist, scrypt mediante core compartido, singleton one-shot; ownership histórico sólo con consentimiento explícito |
| `login()` | Password verificada, respuesta genérica, rate limit persistente en SQLite, token/CSRF aleatorios y sólo hashes en DB |
| `authenticate()` | Hash de token, permiso, revocación/expiración, CSRF mutante y actualización idle TTL acotada por absolute TTL |
| `issue_csrf_token()` | Rota CSRF asociado a sesión válida; nunca persiste token claro |
| `revoke_session()` / `logout()` | Revocación condicional idempotente; evento sólo en transición efectiva |
| `revoke_all_sessions()` | Actualiza sesiones activas atómicamente; llamada repetida sin cambios no añade evento |
| `reset_password_local()` | Password scrypt, revoca todas las sesiones, limpia lockout y registra evento en la misma transacción |
| `status()` / `verify()` | Status no crea archivo ausente; verificación valida versión, estado, integridad y foreign keys |
| `backup_database()` / `restore_database()` | SQLite Backup API a archivo staging temporal, `integrity_check` + `foreign_key_check`, publicación sólo después de validar; no sobrescribe destino existente |

Las mutaciones incluyen usuario, grants, sesiones/guard y evento relacionado en una transacción. `auth_state.initialized` se actualiza al final del bootstrap tras verificar asignación, FK y evento; los triggers de fallo confirman rollback completo. Bootstrap/reset se ensayan sólo como llamadas del fixture: no hay herramienta host-side instalada ni identidad de operador en este adapter. La futura herramienta específica debe validar UID/TTY, path/volumen, backup y esquema, detener writers y obtener autorización explícita. No modificar el CLI aislado para que acepte `nexo.sqlite3`.

## Backup y restauración

El harness usa `sqlite3.Connection.backup()` con source/destination explícitos dentro del temp root. Admite copias por páginas y callback de progreso para inyectar fallos. Copia a un staging sibling único; verifica integridad y FK; ajusta permisos a `0600`; crea el destino con hard-link sólo después de validar y quita staging. Cualquier excepción cierra conexiones y elimina los archivos staging/WAL/SHM/journal. Restore exige destino nuevo; no reemplaza una DB abierta ni borra destino existente.

Resultados de pruebas:

- Backup/restore de SQLite administrativa con sesión y eventos: `integrity_check=ok`, cero violaciones FK, sesión y estado siguen utilizables.
- Source en modo WAL con writer abierto y escritura confirmada: el snapshot contiene los registros confirmados desde WAL y pasa checks tras restore.
- `test_backup_during_controlled_wal_writer_commit_is_transactionally_consistent`: tras copiar páginas iniciales, el callback sincronizado confirma una transacción writer en otra conexión WAL durante el backup. El backup valida y contiene íntegramente la generación anterior o la nueva; no mezcla/omite parcialmente filas. Writer y backup terminan sin busy/error.
- DB NEXO sintética v28 con datos legacy, ownership experimental y auth: backup/restore preserva ledger v28, datos y estado inicializado.
- Excepción inyectada por callback durante backup y durante restore: ni destino ni staging parcial quedan publicados.
- Source corrupta rechazada; destination existente no se sobrescribe.

La prueba WAL anterior mantiene abierto el source y verifica datos ya confirmados antes de iniciar backup; **por sí sola no prueba escrituras concurrentes**. La prueba `test_backup_during_controlled_wal_writer_commit_is_transactionally_consistent` cubre explícitamente el commit concurrente controlado.

El harness no cifra backups ni acredita custodia, permisos del host, snapshots Portainer/Qdrant, restore de volúmenes productivos o coordinación con NEXO en ejecución. Los fixtures sólo contienen valores sintéticos.

## Pruebas de seguridad y fallos

Prueba focal: `.venv/bin/python -W error::ResourceWarning -m unittest tests.test_stage15b3b2b2_admin_persistence -v` -> **22 tests, OK**, sin ResourceWarnings. Cubre:

- SQLite vacía/auth-only, ledger v28, ledger incompleto, columnas/tablas requeridas ausentes y versión futura.
- Auth tables parciales, versión de auth desconocida, esquema v1 legacy del core rechazado sin reescritura, estado uninitialized inconsistente e initialized inconsistente.
- Owner columns ausentes, owner nulo sin consentimiento, asignación histórica en bootstrap y FK experimental.
- Bootstrap concurrente (un ganador), bootstrap repetido rechazado sin cambiar admin/grants/owners/sesiones, y fallo inyectado tras insertar identidad/grants/ownership: rollback completo. Tras el fallo, `test_bootstrap_event_failure_rolls_back_identity_permissions_and_ownership` reabre con otro adapter, comprueba estado uninitialized, login rechazado y recovery por bootstrap completo.
- Login/session/CSRF hash, rotación CSRF, grants explícitos, permiso desconocido denegado, idle expiry, rate limit tras recrear adapter, revocación individual/global concurrente e idempotente.
- Reset password + revoke all; trigger de auditoría falla después de update: rollback al hash/sesión previos.
- SQLite locked/busy -> `PersistenceUnavailable`; tras liberar lock la operación vuelve a funcionar.
- Password/token/CSRF sintéticos ausentes en bytes persistidos; mensajes de error no incluyen la causa inyectada.
- Rechazo de `nexo.sqlite3`, archivo y directorio symlink, path fuera de temp root y path bajo `NEXO_DATA_DIR` configurado; `status()` en archivo ausente no lo crea.
- Backup/restore WAL, integridad/FK, corrupción, fallos inyectados y ausencia de destinos parciales publicados.

No hubo conexiones de red, llamadas a proveedores, MCP, navegador, startup de `app.main`, credenciales reales ni datos productivos. Los fallos probados son errores/triggers/callbacks inyectados; **no se mató un proceso ni se probó terminación abrupta real del host** durante transacción, backup o restore.

## Riesgos residuales y límites

- Es un prototipo aislado, no middleware, persistencia instalada ni herramienta host-side autorizada. No existe CLI host-side, identidad del operador/recovery real, migración, ownership efectivo o policy enforcement. Las llamadas `bootstrap_admin()` y `reset_password_local()` sólo se ejercitan con fixtures; TTY/permisos del host/runbook quedan para un checkpoint posterior.
- No es compatible con `auth_admin` del core aislado anterior; lo detecta/rechaza. Reusa helpers privados del core para KDF; una futura integración tendrá que definir interfaz estable manteniendo encoding y parámetros.
- El path está confinado al temp root, rechaza symlinks y candidatos bajo `NEXO_DATA_DIR`; temp root no es una frontera de seguridad contra host/root. La variable sólo se usa para denegar contención, nunca para abrir ni derivar DB.
- Sólo se acepta schema vacío de prueba o repo v28 exacto. Esquemas de producción, variantes antiguas, DB corruptas o ledgers alterados requieren inspección de una copia autorizada. El test fixture no decide qué migrar.
- El runner `app/migrations.migrate()` conserva migraciones con `executescript`; esta etapa no demuestra atomicidad crash-safe del runner existente ni lo modifica. Sólo el DDL administrativo experimental usa una transacción explícita.
- Login verifica KDF dentro de `BEGIN IMMEDIATE`, como el core aislado: throughput bajo concurrencia y multi-host/filesystems de red no están aprobados; lock timeout falla cerrado.
- No hay scheduler de retención/limpieza de auditoría. Expiradas se revocan al usarse y sesiones antiguas se mantienen en el prototipo; lifecycle/retención aún necesitan decisión.
- El estado de servicio no se autentica: identidad/delegación de AgentRuntime y scopes no forman parte de este adapter.
- `reset_password_local()` es una operación host-side conceptual, no verifica por sí misma la identidad de quien llama. No convertirla en endpoint ni afirmar que el método constituye autorización operacional.
- El harness de backup produce archivos sin cifrado. El `0600` temporal no sustituye cifrado/custodia ni runbook de backup/restore real.
- El test concurrente valida consistencia transaccional SQLite/WAL bajo un writer local controlado, no crash/power-loss, múltiples hosts, NAS, proxies ni coordinación de un despliegue real.

## Recomendación

**GO limitado** para revisión técnica de contratos y uso en fixtures/staging descartable. **NO-GO** para instalar este módulo en NEXO, apuntarlo al volumen productivo, ejecutar bootstrap real, añadir migraciones, modificar el CLI o integrar auth en rutas. La prueba v28 sólo demuestra compatibilidad del repo en fixture, no compatibilidad de una instancia desplegada. Esta etapa no autoriza Stage 15B.3B-2B.3 ni Stage 15C.

Software Docs Shared y su esquema representativo fueron consultados; no se modificaron y no se creó propuesta porque el checkpoint sólo implementa el modelo ya documentado.

## Verificación final

- Preflight: `HEAD=9d0a95952e191121c0a9b0cfbc2bff9fc8d5b2d9`, rama `main`, worktree limpio; stash histórico intacto.
- Prueba focal: `.venv/bin/python -W error::ResourceWarning -m unittest tests.test_stage15b3b2b2_admin_persistence -v` -> **22 tests, OK**, sin ResourceWarnings.
- `./scripts/nexo check`: **329 passed, 1 skipped**, exit 0. Warnings existentes: FastAPI `on_event`, Starlette `BlockingPortal` y tres Node `MODULE_TYPELESS_PACKAGE_JSON`; sin fallos.
- `git diff --check`: OK; también pasaron `git diff --no-index --check /dev/null <archivo>` para los tres archivos nuevos.
- `git status --short --branch`: `## main...origin/main [ahead 10]`; sólo tres untracked: `docs/stage15b-3b-2b2-administrative-persistence.md`, `prototypes/admin_persistence.py`, `tests/test_stage15b3b2b2_admin_persistence.py`.
- Cambios se dejan sin commit. `app/`, migraciones, frontend, AgentRuntime, providers, MCP, RAG, SSE, Docker y configuración productiva no se modificaron.
