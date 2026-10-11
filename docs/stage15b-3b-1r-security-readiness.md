# NEXO Stage 15B.3B-1R: Administrative Security Readiness

- Estado: CLI local aislado implementado y verificado; listo para revisión
- Base inicial: `43fcbf5b28c8c64362f32238406b3eab9fc46926`
- QA lockout commit: `d99bd4988ae6a030d5514d03de0237c77965fa05`
- Migraciones del repositorio: máxima observada 28; no se reservó versión
- Stage 15B.3B-2: **bloqueada**
- Stage 15C: **bloqueada**
- No usar con la DB de NEXO ni desplegar el CLI en el contenedor productivo

## Resumen

El parche QA de lockout quedó consolidado en `d99bd4988ae6a030d5514d03de0237c77965fa05` (`test: harden administrative authentication lockout behavior`): evita persistir/loguear cada intento durante un bloqueo activo; rechaza ventana de rate limit inválida; prueba username alterno. La modificación se limitó a los ajustes QA y el informe Stage 15B.3B-1 QA.

Se añadió `prototypes/admin_auth_cli.py`, CLI local para el store **aislado** de auth. No se instala como entry point, no se monta en HTTP, no se conecta a NEXO y no cambia `app.main`, AgentRuntime o migraciones. `bootstrap`, `reset-password` y `revoke-sessions` exigen stdin/stderr TTY; status es de sólo lectura y no crea DB. La ruta DB es obligatoria en argv, pero nunca se acepta password en argv: las passphrases entran por `getpass`. El constructor prohíbe el destino resuelto de `$NEXO_DATA_DIR/nexo.sqlite3`, basename `nexo.sqlite3`, `:memory:` y symlinks; CLI valida directorio propietario del UID actual, con permisos del owner y sin escritura de grupo/otros. El archivo DB queda en `0600`.

Uso **sólo sobre archivo aislado, temporal/de prueba o de prototipo**:

```text
python -m prototypes.admin_auth_cli --db /ruta/privada/auth.sqlite3 status
python -m prototypes.admin_auth_cli --db /ruta/privada/auth.sqlite3 bootstrap
python -m prototypes.admin_auth_cli --db /ruta/privada/auth.sqlite3 reset-password
python -m prototypes.admin_auth_cli --db /ruta/privada/auth.sqlite3 revoke-sessions
```

No ejecutar esos comandos contra la base productiva. El bloqueo explícito a `nexo.sqlite3` es defensa adicional, no autorización para usar otra ruta que contenga datos productivos.

## Consolidación QA

El preflight inició en HEAD esperado `43fcbf5b28c8c64362f32238406b3eab9fc46926`, rama `main`; worktree contenía las tres correcciones QA no consolidadas de 15B.3B-1 y el informe `docs/stage15b-3b-1-qa-security-review.md`. Stash histórico observado: `stash@{0}: On main: checkpoint: preserve pre-alignment worktree cce925b7`; no se aplicó ni modificó.

Cambios QA revisados:

- Durante un lockout ya activo no se inserta una fila de seguridad ni se escribe un log por cada intento. Se conserva el evento de inicio del lockout y la respuesta externa genérica.
- `failure_window < 1` se rechaza al construir el store.
- Tests: ventana inválida, rate limit común entre usernames, y spam bajo lockout sin crecimiento del ledger.
- Los cambios no alteran contraseña/KDF, expiraciones, permisos, origen/host, cookies ni sesiones.

Commit local independiente: `d99bd4988ae6a030d5514d03de0237c77965fa05` (`test: harden administrative authentication lockout behavior`); incluye sólo las correcciones QA, actualización factual de 15B.3B-1 y el informe QA. No incluye CLI, tests del CLI ni este informe 1R.

## CLI administrativo aislado

| Comando | Comportamiento |
|---|---|
| `status` | Lectura SQLite `mode=ro`; muestra sólo `state=initialized|uninitialized` y cantidad de sesiones activas; no muestra usuario, DB path, hashes, tokens ni crea una DB inexistente. Exit 0; almacenamiento ilegible/incompatible exit 3. |
| `bootstrap` | Exige TTY antes de crear el directorio; crea parent con modo privado si falta; rechaza admin existente sin volver a pedir passphrase; `getpass` + confirmación y bootstrap transaccional one-shot. Exit 0, ya configurado exit 4, no TTY/input inválido exit 2, storage exit 3. |
| `reset-password` | Exige TTY y estado admin inicializado; prompts ocultos y confirmados; actualización del hash, revocación de todas las sesiones y reset de contador en una transacción. No hay endpoint HTTP. |
| `revoke-sessions` | Exige TTY, admin inicializado y confirmación literal `REVOKE`; revoca todas las sesiones atómicamente. |

Exit codes comunes: 0 éxito, 2 uso/TTY/path/input no aceptado, 3 storage no disponible/fallo operativo, 4 el estado no permite bootstrap/reset/revoke. Los mensajes no muestran el path ni detalles SQLite/password. No hay password positional, option ni fallback de stdin pipe. TTY es la única modalidad de entrada de secretos en esta versión; no se implementó descriptor/file secret.

El límite de autorización es el control del host: usuario capaz de ejecutar Python con acceso de lectura/escritura al archivo auth puede administrar la identidad local. El CLI valida dueño/permisos del directorio, pero no verifica identidad del operador más allá de Unix UID/TTY. Esto no es MFA, separación de duties, ni frontera contra root/Portainer owner. No desplegarlo dentro de NEXO; debe usarse desde un procedimiento host-side revisado.

### Bootstrap y recuperación propuesta

1. Operador con identidad administrativa legítima del host; cerrar/restringir ingress y detener el servicio durante recovery.
2. Verificar fuera de banda instancia, path y mount; preservar backup cifrado consistente y comprobar recuperación. No copiar la DB NEXO como destino del prototipo.
3. Para primera alta, ejecutar `bootstrap` sobre store dedicado: no preconfigurar password; `getpass` dos veces; atomicidad SQLite, singleton y estado durable impiden alta repetida. Reinicio no vuelve el estado a `uninitialized`.
4. Password perdida: ejecutar `reset-password` desde TTY bajo el usuario autorizado del host. El reset no elimina conversaciones/configuración, rota el hash, revoca sesiones y deja evento no secreto. Reiniciar y comprobar status/login antes de reabrir ingress.
5. Storage ausente/corrupto/permission denied: fail closed; no invocar bootstrap sobre un estado ambiguo, no borrar DB ni habilitar auth anónimo. Restaurar backup válido o llevar reparación offline auditada. No hay endpoint de emergencia ni password universal.
6. `revoke-sessions` es operación local adicional para incidentes; no requiere autenticación web y requiere capacidad de filesystem/TTY del host.

Para el producto, el CLI no constituye aún un recovery operacional: se debe seleccionar el store definitivo (se recomienda SQLite principal), ruta/configuración autorizadas, backup y restore, identidad host, instalador, runbook y control concurrente del proceso. No puede usar hoy SQLite principal porque `AdminAuthStore` lo rechaza deliberadamente.

## Persistencia definitiva propuesta

**Recomiendo SQLite principal de NEXO (opción A) en la futura integración, no la DB aislada B.** Chat, ownership y acciones futuras comparten el mismo estado transaccional; una única DB simplifica backup/restore y evita ventanas de consistencia entre sesión y decisión de acceso a recursos. Tablas separadas preservan separación lógica, no ACL de filesystem. Como el archivo actual contiene secretos de provider y datos de usuario, backups deben protegerse como secreto; auth no añade cifrado en reposo.

La DB independiente B sólo gana frontera si el broker corre con otro UID/proceso y ACL real. Mismo UID/proceso puede abrir ambos ficheros, así que el prototipo de DB separada no contiene compromiso del runtime; además genera doble backup, restore, lifecycle, monitorización y un fallo no atómico entre revocar sesión y ejecutar acción en NEXO.

No crear/ejecutar migración aquí. La fuente `app/migrations.py` enumera hasta 28; no se puede extrapolar eso a una instalación productiva sin inspección autorizada de su copia/metadata.

### Especificación de migración futura (propuesta, no ejecutable)

Volver a revisar HEAD, `MIGRATIONS` y la versión de una copia real autorizada antes de seleccionar número. No reservar 29 desde este doc. Diseñar tablas con prefijo claro:

| Tabla/relación propuesta | Esquema mínimo |
|---|---|
| `auth_users` | `id` PK, `username_normalized` UNIQUE, `password_hash` versionado, `status`, `created_at`, `password_changed_at` |
| `auth_state` | singleton, bootstrap completado, versión/policy de auth |
| `auth_sessions` | `token_hash` PK, `user_id` FK, `csrf_hash`, `created_at`, `last_seen_at`, `idle_expires_at`, `absolute_expires_at`, `revoked_at`; índice expiración/user |
| `auth_login_guard` | sujeto de login, ventana, fallos, `locked_until`; clave única por administrador (y sólo sumar red/IP si ingress confiable) |
| `auth_security_events` | id, tipo allowlisted, actor local/user si se conoce, tiempo, correlation id; sin secreto/body |
| `conversations.owner_user_id` | FK nullable inicialmente; rellenar con el admin sólo en bootstrap transaccional después de una decisión explícita sobre el histórico |
| `notebooks.owner_user_id` | FK nullable inicialmente; migrar a admin o ACL tras decisión del operador |
| agent profiles/providers/config | permanecen recursos administrativos/globales en MVP; no inventar ownership personal ni copiar/modificar secrets |

Sources heredan acceso del notebook; messages, citations, runs, approvals e interactions deben validar ownership vía conversación/run padre y recibir índices para esas consultas. Preferencias globales se asignan/migran bajo decisión explícita. Data ya existente no se borra.

Bootstrap local en la DB principal debe, bajo `BEGIN IMMEDIATE` en la misma conexión, crear admin y asignar ownership a histórico (si el operador aprueba), bloquear/cancelar approvals/interactions no verificables y marcar estado completo. No exponer API privada antes de que todo esté listo. Operaciones repetidas/idempotencia y migración interrumpida requieren tests sobre copia. El migrador actual aplica secuencialmente y algunas migraciones usan `executescript`; definir atomicidad explícita para auth y probar fallo/crash/rollback, no presumir que todo `migrate()` ya es transaccional. Backup pre-migration, restore ejercitado y no downgrade borrando filas auth. El release debe fallar cerrado si migración/bootstrap incompletos. Ninguna de estas tablas/columnas se añadió en 1R.

### Tratamiento de datos y operaciones pendientes

- Preservar conversaciones, mensajes, citas, notebooks, documentos, agentes, modelos, providers y configuración existentes.
- Asignar datos históricos al primer admin sólo con aprobación del operador; exportar un reporte de conteos/IDs, no contenido sensible.
- Pending approvals: establecer `invalidated`/cancelar transaccionalmente antes de auth-enable, sin ejecutar; no confiar en UUID ni aprobación previa anónima.
- Pending interactions y active/waiting runs: cancelar/expirar, no reanudar con contexto anónimo; run resumido exige sesión/owner y nuevo check de delegación.
- Qdrant/vector index es derivado y no se borra/reindexa por una migración auth.
- Rollback mantiene ingress cerrado y restaura snapshot completo bajo runbook; jamás volver a binario sin auth en red pública.

## Contrato futuro AgentRuntime

No cambiar AgentRuntime en 1R. La separación actual `Principal`/`service_principal()` es estructura lógica solamente; cualquier código del proceso puede construir un `Principal(kind="service")` permitido. No autorizar tools o providers basándose en ese objeto.

Flujo futuro requerido:

1. Autenticar principal humano en HTTP; comprobar permiso de correr agente, owner de conversación, profile, notebook, provider y tools antes de crear run/side effects.
2. Crear una delegación de servidor ligada a `user_id`, session/revision, `conversation_id`, `run_id`, service audience exacta, scopes mínimos, provider, tool allowlist/fingerprint y expiración. No aceptar actor, scope o run owner de payload no confiable.
3. Resolver en cada llamada el run activo y la delegación; verificar expiry, revocación, audience, scope, owner, tool allowlist y límites de ejecución antes de side effect. Tools no reciben `system:admin` ni credencial humana.
4. Approval/interactions: aprobación se vincula a usuario + conversación + run + tool snapshot + argumentos hash, se consume una vez mediante transición SQL CAS; revalidar sesión/permiso al responder y al resume. `interaction_id`, `approval_id` y `run_id` por sí solos no autorizan.
5. Revocar grants al fin/cancelación del run, logout/reset de sesión, cambio de perfil/policy relevante o decisión de operador. Grant expirado no se renueva automáticamente con scope más amplio.
6. Dentro del mismo proceso, una firma/delegación sirve para evitar confusiones y bugs, pero código comprometido puede invocar issuer o saltarse verificación. Si se requiere resistencia a servicio falsificado, correr servicio con UID separado y canal IPC autenticado; token firmado por issuer confiable con audience, expiración y scope estrecho. No incluir Docker socket ni password admin.

Debe medirse si permisos mínimos por ejecución basta con grant verificado en proceso o si el threat model de tools mutativas exige split de proceso antes de integración.

## Resultados de pruebas

- CLI focal `.venv/bin/python -m unittest tests.test_stage15b3b1r_admin_cli -v`: **8 tests, OK**.
- Auth core y regresión 3A: **24 tests, OK**.
- Focal conjunta Auth Core + CLI + 3A: **32 tests, OK**.
- `./scripts/nexo check`: **290 passed, 1 skipped**; regresiones frontend OK.
- Advertencias del gate: FastAPI `on_event`, alias Starlette `BlockingPortal` y Node module type; sin fallos.
- El test de reset inyecta un fallo SQLite después de actualizar hash/revocar sesiones y verifica rollback completo. Los tests de destino prohibido cubren path principal, symlink que resuelve a `nexo.sqlite3`, y directorio con permisos de escritura para grupo/otros.
- `git diff --check`: OK. Los tres archivos nuevos se comprobaron además con `git diff --no-index --check /dev/null <path>`; sin diagnósticos whitespace.
- Fixtures sintéticas `FAKE_*`; ningún proveedor, API real o DB de producción fue accedido.
- Estado final: sólo el helper de reset local en `prototypes/admin_auth.py` modificado y tres archivos nuevos de 1R; sin staging ni commit de implementación.
- No realizar `./scripts/nexo smoke`, migración, despliegue ni commit del trabajo 1R.

## Riesgos pendientes y decisión

- El CLI trabaja sólo con DB aislada, no sabe ubicar/administrar una DB principal productiva ni está instalado como entry point. El operador puede ejecutarlo únicamente en una ubicación aislada autorizada; no es runbook aprobado para reset de NEXO real.
- La frontera local equivale a control POSIX UID/TTY/path; root/host owner puede leer/modificar o reemplazar auth DB. `0600` no cifra backups ni separa el web process con mismo UID.
- SQLite principal requiere diseñar migración y probar atomicidad real; la versión repo 28 no demuestra la versión productiva.
- Principal de servicio no autenticado, no hay delegaciones firmadas/revocables/expiry reales ni integración de aprobación.
- TLS/proxy móvil/forwarded header trust no verificados. Core no activa autenticación en rutas; la app mantiene el estado anterior anónimo.
- Lockout global puede ser DoS; valores se medirán en hardware/ingress elegido.

**NO-GO para Stage 15B.3B-2 de integración** hasta revisión del CLI/procedimiento, aprobación del operador para persistencia A, plan de migración/restore, contrato de delegación confiable y topología real. GO únicamente para revisión de este diseño y el prototipo local. Stage 15C sigue bloqueada. No hubo push/despliegue; el stash se conserva.
