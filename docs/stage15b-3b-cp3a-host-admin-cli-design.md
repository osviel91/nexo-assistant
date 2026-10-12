# NEXO Stage 15B.3B / CP3-A: Host-Side Administrative CLI Design

- Modalidad: inspección, diseño de seguridad y contratos; sin implementación productiva
- HEAD preflight: `4270a017c1482269ab2bf03339323338ea12810b`, rama `main`
- Worktree inicial limpio; stash histórico `b7d2662a55bb2519aadd4291a93400c689afb46c` presente e intacto
- Decisión: **GO para diseño y validación aislada; NO-GO para bootstrap, operación, activación y producción**
- No se abrió SQLite productiva, no se ejecutó bootstrap real, no se modificaron runtime/migraciones/Docker y no hubo despliegue.

## 1. Recomendación

Diseñar `nexo-admin` como herramienta offline ejecutada en el host que controla la instalación, con acceso explícito al volumen/directorio de datos y una interfaz estrecha con el supervisor de servicio local. No debe importarse desde `app.main`, exponerse por HTTP ni enviar secretos por argumentos, entorno, logs o archivos de manifiesto. `inspect`, `status` y `backup-verify` pueden ser lecturas; cualquier comando de cambio debe exigir TTY local, identidad de operador autorizada, selección inequívoca de instalación, ventana de mantenimiento y consentimiento ligado al plan actual.

Esta es una recomendación de diseño, no una declaración de que exista hoy un mecanismo fiable para cerrar ingress, detener todo ejecutor o demostrar su ausencia. Con el runtime observado y sin integración con un supervisor/fence probado, `bootstrap-apply` debe fallar cerrado: el gate de apply no está satisfecho. La herramienta no puede transformar una afirmación del operador en evidencia técnica de quiescence.

## 2. Alternativas

| Alternativa | Ventajas | Límites y riesgos | Decisión |
|---|---|---|---|
| Host-side CLI + supervisor del host | UID/GID, filesystem, sockets de servicio y operación de mantenimiento quedan en el mismo dominio administrativo; no requiere publicar API ni montar socket Docker | Requiere instalación/versionado del CLI y un adaptador específico del supervisor; `root` puede saltarse controles de archivo; no detecta ejecutores externos por sí sola | Recomendada; separar validación local de atestaciones operacionales verificables |
| Contenedor administrativo efímero | Reutiliza imagen/runtime Python; en Portainer puede ejecutar un one-shot con volumen de datos | Debe detenerse antes el servicio y cerrar ingress externamente; identidad UID del host y ACL varían por bind/named volume; imagen debe coincidir por digest; montaje del volumen amplía privilegio; no usar Docker socket ni `privileged` | Alternativa operacional si host CLI no es viable, bajo procedimiento Portainer versionado y probado |
| Operación dentro del contenedor NEXO | Código y ruta SQLite directamente disponibles | Mezcla plano HTTP y autoridad administrativa, compite con workers, facilita exposición accidental | Rechazada |
| Endpoint HTTP administrativo / bootstrap remoto | Automatización remota cómoda | No establece autoridad local ni demuestra ingress/fencing; reintroduce superficie de ataque y dependencia del runtime anónimo | Prohibida |

No añadir SSH al contenedor NEXO. El administrador de Portainer puede realizar operaciones equivalentes como usuario de infraestructura: parar stack/servicio, restringir ingress, ejecutar contenedor one-shot del release exacto con sólo el volumen NEXO necesario, sin puertos, sin socket Docker, sin modo privilegiado y con UID/GID/permiso de escritura mínimos. El contenedor one-shot no prueba que otros procesos o servicios externos estén detenidos. Qdrant no se debe montar ni alterar para esta transición salvo que un plan aprobado lo requiera explícitamente.

## 3. Evidencia Del Código Y Límites

- `app/safe_migrations.py` ofrece `DatabaseLease`: runtime toma `flock` compartido durante su vida y el motor offline requiere exclusivo. Coordina procesos cooperantes únicamente; no demuestra que no haya consumidores legacy, escritor ajeno, worker externo ni efecto en vuelo. Sus `inspect_database()`/manifests detectan estados de esquema y datos admitidos; no son autorización de operador.
- `app.main.startup()` adquiere el lease y valida versión/schema antes de iniciar módulos. Sigue sin comprobar bootstrap/auth y conserva rutas anónimas. No iniciar esta versión después de una transición como si estuviera protegida.
- El acceso HTTP crea streaming SSE, runs y tareas shadow; AgentRuntime invoca proveedores y herramientas; módulos MCP pueden llamar servicios remotos. Hay tareas asyncio y refresh de proveedor. `ToolExecutionContext` son IDs lógicos, no una credencial revocable o lease de executor.
- Approvals pueden entrar en `executing` y luego llamar a herramientas; interacciones suspenden y reanudan streams. SQLite registra parte del estado, pero no prueba el estado real de solicitudes, llamada remota, retries o resultado externo ambiguo.
- El worker mantiene el lease SH incluso tras callback `shutdown()` hasta que termina el proceso. Detener servicio vía supervisor y confirmar salida de todos los workers es diferente de llamar shutdown, obtener lease SQLite o cerrar el listener.
- `prototypes/admin_auth_cli.py` usa un almacén de auth dedicado y explícitamente rechaza `nexo.sqlite3`; `prototypes/admin_persistence.py`, asignador y simulador están confinados a archivos temporales. No son comandos productivos ni deben aflojarse para operar sobre `$NEXO_DATA_DIR`.
- `app/historical_ownership_audit.py` es un writer estrecho dentro de transacción caller-owned. El asignador actual requiere bootstrap ya completado y abre su propia transacción; no puede invocarse para crear el primer admin en la transacción única propuesta.
- Docker Compose monta `nexo-data:/data`; `NEXO_DATA_DIR=/data`. La imagen actual arranca Uvicorn por defecto, no contiene contrato de CLI administrativa ni define usuario `USER` explícito. Portainer Compose mantiene volumen nombrado, pero UID/GID efectivo y controles del host dependen del despliegue concreto.
- CP2-B.2b-2B.2 recomienda una transición `BEGIN IMMEDIATE` única en SQLite v31, con identidad/grants, asignación histórica, invalidaciones, auditoría v31 y finalización. Sólo SQLite es atómica; las barreras de infraestructura son precondiciones externas.

Los documentos CP1, CP2-A, CP2-B.1 y CP2-B.2 existentes revisados registran HEAD históricos de sus respectivos checkpoints. CP2-B.2b-2B.2 indica que el informe de persistencia solicitado con otro nombre no existe; la evidencia relacionada está en `stage15b-3b-2b2-administrative-persistence.md`, blueprint 2A y prototipos. `prototypes/admin_auth.py` sí existe en este HEAD aunque el inventario de CP2-B.2 lo trataba como referencia de etapa; no se presupone compatibilidad entre su store y el modelo v31.

## 4. Autoridad Y Selección De Instalación

Separar explícitamente estas identidades:

| Identidad | Función | No demuestra |
|---|---|---|
| Usuario OS del proceso CLI | Sujeto local que solicita operación; UID efectivo, grupos, sesión/TTY y autorización del host | Que sea el admin de NEXO o dueño legítimo de cualquier volumen |
| Admin inicial de NEXO | Persona destinataria, creada dentro de la transición con identidad/grants explícitos | Autoridad del proceso que invoca CLI |
| Identidad administrativa SQLite | Filas `auth_users`, grants, sesiones/eventos del esquema objetivo | Integridad/autenticidad de la DB frente a root o propietario del archivo |
| Service principal | Identidad técnica con audiencia/scope acotados en una integración futura | Ser persona, destinatario de ownership o autoridad de bootstrap |
| Proceso NEXO | Worker HTTP/runtime que posee lease SH cooperativo | Cobertura de procesos no cooperantes o ejecutores externos |
| Ejecutores externos | Provider, MCP, Qdrant u otros sistemas/colas/workers fuera del proceso | Ser detenidos por lock o transacción SQLite |

Autorización propuesta para una instalación: operación interactiva desde una sesión local autenticada por el OS; UID efectivo miembro de una allowlist de mantenimiento configurada por el propietario de esa instalación; directorio de datos y DB pertenecen al UID/GID esperado o a una ACL aprobada; instalación seleccionada por identificador configurado por administrador de infraestructura, nunca inferida sólo desde cwd, `NEXO_DATA_DIR` heredado o URL HTTP. Registrar ID de instalación opaco, release esperado y volumen/dispositivo observado. `root` no se acepta como sustituto de una identidad aprobada sin un procedimiento break-glass separado, registrado y de doble control.

La ruta se recibe explícitamente como configuración confiable, se canonicaliza una vez, y se valida cada componente con `lstat`/`openat` y `O_NOFOLLOW` donde el OS lo soporte; rechazar symlinks, archivos no regulares, directorios escribibles por grupo/otros fuera de política, hard links inesperados cuando sea verificable, mount/source distinto del esperado, rutas relativas/ambiguas, DB ausente/cero bytes y sidecars/estado inesperado. Abrir por descriptor y mantener descriptor/lease evita re-resolución entre check y open, aunque SQLite requiere un path utilizable; comparar `fstat` del descriptor, `stat` posterior, device/inode y mount identity inmediatamente antes de abrir y dentro del procedimiento, y abortar ante cambio. Esto reduce TOCTOU, no protege de un actor con privilegios equivalentes/root ni certifica identidad del volumen en todos los sistemas.

UID/GID, permisos mode, ACL, `O_NOFOLLOW`, `flock`, `openat`, `fstat` y filesystem local son controles dependientes del OS/filesystem. NEXO debe comprobar los límites que pueda observar y fallar cerrado; no puede por sí solo garantizar la política del host, integridad contra root, verdad de Portainer, fencing de red, autoridad humana, apagado de otros servicios ni durabilidad frente a pérdida de energía.

Principio de privilegio mínimo: no ejecutar como root si grupo/ACL bastan; no pertenecer a grupo Docker ni montar `/var/run/docker.sock`; usar acceso read-only para comandos de lectura y abrir escritura sólo después de todas las verificaciones en apply/reset/revoke; nunca heredar variables secretas innecesarias. Si el filesystem no soporta los locks/rename/fsync requeridos, negar mutación.

## 5. Interfaz Propuesta (No Congelada)

Códigos propuestos: `0` éxito/estado esperado; `2` argumentos, TTY o selección inválida; `3` storage/IO/lock no disponible; `4` precondición, evidencia o consentimiento no válido/obsoleto; `5` conflicto de operación/estado distinto; `6` recovery requerida/estado ambiguo. Salida de máquina opcional sólo con esquema versionado, allowlist de campos y redacción; no volcar SQL, excepciones SQLite crudas, secretos, contenidos o rutas completas por defecto. Todas las escrituras generan audit event transaccional con actor local, instalación opaca, operación, resultado y digest; nunca password/token ni valores de contenido.

| Comando | Parámetros / identidad | Precondiciones y efecto permitido | Salida, error, idempotencia, auditoría y recuperación |
|---|---|---|---|
| `inspect` | `--installation ID`; operador local autenticado para leer instalación | Sólo lectura; valida ruta/volumen, release, ledger/manifiesto, generación, integridad/FK, estado auth y leases/journal visibles | Resume estados y blockers sin datos de usuario. Idempotente, sin evento de mutación. Cualquier discrepancia = código 3/4/6 y no repara |
| `backup-verify` | `--installation ID --backup REF`; operador con permiso de lectura/custodia | Verifica archivo de backup seleccionado, hash, Backup API/restore aislado, integrity/FK, esquema/ledger, generación e igualdad con objetivo requerido. No crea ni restaura sobre DB canónica | Informa `verified` sólo para artefacto y criterios nombrados. Repetible sobre mismo artefacto. No equivale a custodia, durabilidad host ni coherencia de otros stores. No borrar ni reemplazar backup en fallo |
| `bootstrap-plan` | `--installation ID --recipient USERNAME`; TTY/operador autorizado; sin password en argv/env | Snapshot sólo lectura de instalación, inventario histórico, relaciones, estados operativos y filas que resultarían afectadas; producir operation ID, manifiesto canónico y backup requerido | Muestra IDs/conteos y hashes, no contenido. Plan no concede permiso, no reserva estado y caduca si cambia cualquier entrada. Puede guardarse manifiesto sin secretos con permisos restrictivos; replanificar ante cambio. Sin escritura DB |
| `bootstrap-apply` | `--installation ID --plan FILE`; TTY obligatorio; operador mantenimiento + consentimiento específico; credencial de destinatario leída con prompt seguro | Sólo con schema/ledger soportado, backup restaurado/verificado, manifiesto exacto actual, ingress cerrado, procesos NEXO detenidos, ejecutores conocidos fenceados, cero efecto ambiguo, lease exclusivo y confirmación fresca. Una única transacción CP2-B.2b-2B.2 | Éxito sólo tras commit, cierre/reapertura read-only, auditoría/postcondiciones y readiness administrativa offline. Error antes commit rollback; error o crash tras commit se diagnostica mediante operation ID sin repetir ciegamente. Sin bandera `--yes` ni fallback |
| `status` | `--installation ID`; lectura local autorizada | No escribe ni inicia DB ausente. Lee auth state, generación, audit/bootstrap, locks y recovery markers visibles | Estado estructurado, `ready_for_authenticated_activation` siempre false salvo que gates CP3-B+ posteriores estén demostrados; la CLI no abre ingress. Idempotente |
| `recover` | `--installation ID`; operador de break-glass local, TTY y motivo obligatorio | Diagnóstico offline. Sólo permite finalización/limpieza si journal y generación coinciden exactamente con estado reconocido por motor existente; bootstrap ambiguo requiere bloquear y escalar, no reparar | Nunca cambia estado para habilitar runtime anónimo. Cada acción explícita y auditada. Repetición exacta sólo lectura/no-op validado; contradicción conserva DB, backup, journal y staging |
| `reset-password` | `--installation ID`; operador break-glass autorizado; password y confirmación por prompt TTY | Bootstrap completado, schema íntegro, lock exclusivo y runtime/ingress cerrado o writer fenceado; actualiza hash, revoca todas las sesiones y limpia guard en la misma transacción | Sin username/password en logs/output. Idempotencia: contraseña nueva repetida produce nuevas escrituras/eventos, así que no prometer no-op; un operation ID impide ambigüedad de retry. Fallo rollback; tras crash inspeccionar event/estado antes de reintentar |
| `revoke-sessions` | `--installation ID`; operador break-glass; confirmación TTY con resumen/cantidad | Estado auth coherente, lock y transacción; revoca sesiones activas y agrega evento sólo si hubo cambio | Repetición devuelve cero cambios, sin duplicar evento. No invalida credenciales externas ya emitidas salvo que su validador consulte estado/revocación. Error conserva sesión/estado previos |

Estos comandos son operaciones locales de host. `status` no es readiness HTTP y `recover` no es una puerta de bypass. No se propone `bootstrap` basado en el CLI antiguo: el destino es el esquema integrado que acuerde CP3-B y el contrato atomic de CP2, no el archivo auth dedicado del prototipo.

## 6. Plan Y Consentimiento

El plan será versionado, determinista, ordenado y serializado canónicamente. Como mínimo liga: installation ID; release y manifest de esquema esperado; schema version y digest; generación/digest de datos; ID estable y username normalizado del destinatario (sin secreto); grants exactos/versionados; inventario histórico de conversaciones/notebooks y relaciones necesarias en digest/conteos; approvals a invalidar e interacciones a denegar; estados de runs que bloquean; operación ID aleatorio único; backup ID, hash, generación y resultado de restore; versión del contrato y timestamp/expiración. El documento no contiene password/hash, token, API key, texto de conversación ni path absoluto si el identificador opaco basta.

El hash del plan ofrece integridad/correlación, no prueba identidad del autor, consentimiento, origen del backup ni custodia. Consentimiento no transferible: inmediatamente antes de cambiar SQLite, volver a leer el plan y estado, verificar el hash y solicitar en TTY una frase que incluya una porción del operation ID y confirme explícitamente instalación, destinatario, generación, backup y resumen de elementos afectados. Exigir `stdin` y `stderr` TTY real; rechazar pipes, `--yes`, variable de entorno, respuesta almacenada y prompt de contraseña por argumento. La contraseña se solicita por canal sin eco y se confirma; su hash sólo se produce dentro del proceso y sólo el digest se persiste.

Alternativa futura para automatización: artefacto de consentimiento firmado con clave de operador protegida por OS/HSM, con audiencia igual a installation ID, hash de plan, operation ID y expiración/nonce. No aceptarlo hasta decidir gestión/rotación/revocación de claves y replay. No aceptar un JSON con `approved: true` ni firma autocertificada por la propia CLI. Un plan diferente, DB cambiada, otro backup, destinatario/grants distintos, expiración, operation ID reaprovechado o falta de TTY invalida consentimiento y exige nuevo plan/confirmación.

## 7. Quiescence, Exclusividad Y Fencing

| Categoría observada | Alcance de control | Precondición de apply |
|---|---|---|
| Workers NEXO CP2-B.1 cooperantes | `DatabaseLease` SH/EX bloquea acceso/migración sólo si todos abren DB por ruta/lease común | Parada del supervisor confirmada, todos los procesos identificados terminados y lease EX obtenido. EX es corroboración, no inventario universal |
| HTTP/SSE, requests y tasks del worker | Cerrar ingress evita nuevas solicitudes desde frontera controlada; apagar listener no revierte request aceptada. SSE puede tener stream/run en curso; tareas shadow y refresh son locales | Cerrar ingress primero, esperar drenaje con timeout documentado, luego terminar workers; confirmar procesos fuera del supervisor también. Run/task sin terminación comprobable bloquea |
| AgentRuntime / tools locales | Por ejecución; context contiene IDs, no cancel token duradero ni fencing | Proceso parado y tarea terminada; no inferirlo del estado de conexión HTTP |
| Approvals/interactions | Approval `executing` puede haber producido efecto; `approved` puede reanudarse; interaction pendiente puede mantener coroutine | Rechazar executing, run activo/desconocido, approval/interacción recuperable o pendiente no reconciliada. Sólo estados terminales y efectos reconciliados entran al manifiesto/invalidation CP2 |
| MCP, providers, HTTP externos, Qdrant, decision runtime | Efecto y retry pueden estar fuera de NEXO; DB no coordina commit remoto | Para cada integración: detener worker y revocar/rotar o fencing de credencial/epoch verificado en cada ejecución, deshabilitar retry/cola y resolver resultado ambiguo. Si no existe comprobación verificable, bloquear apply |
| Otros procesos/escritores SQLite | `BEGIN IMMEDIATE` y EX lease detectan parte de la contención en filesystem local; descriptor inactivo/actor no cooperante no necesariamente | Supervisor/inventario completo, lock exclusivo y revalidación transaccional. Escritor o sidecar inesperado = abortar; no checkpoint/convertir journal automáticamente |
| Portainer/Docker y otros hosts | CLI no puede deducir todos los stacks, réplicas, cron, colas o tareas desde SQLite | Evidencia del plano de infraestructura (stack/replicas/workers/routes/egress) recogida por operador autorizado. Si no puede demostrarse completitud, apply bloqueado |

Orden de la ventana: cerrar ingress/proxy en frontera externa; confirmar que no entran solicitudes y detener política de retry; drenar streams y reconciliar aprobaciones/llamadas ambiguas; detener todas las réplicas y workers mediante supervisor; confirmar ausencia de procesos y tareas administradas; fencear integraciones externas y colas; verificar estabilidad de inventario/volumen; obtener lease exclusivo; preparar/verificar backup y plan; pedir consentimiento final; abrir conexión SQLite y `BEGIN IMMEDIATE`; recalcular generación, schema, backup, inventario, relaciones, runs y operation ID en esa conexión; abortar ante cualquier cambio. Mantener ingress y egress cerrados hasta el post-check y posterior activación autenticada.

CP2 especifica esta secuencia lógica: operador, inspección, schema/generación, backup, cierre ingress, parada/fencing, consentimiento, exclusividad, `BEGIN IMMEDIATE`, revalidación, identidad/grants, asignación histórica, invalidaciones permitidas, auditoría v31, finalización bootstrap, commit, verificación reabierta y preparación para activación autenticada. Las etapas de host/supervisor requieren evidencia externa; DB no puede probarlas.

**Bloqueo duro:** si hay ejecutor desconocido/no fenceable, stream/efecto en vuelo, run/approval ambiguo, lease no exclusivo o ingress sin propietario operacional identificable, `bootstrap-apply` no debe existir habilitado para esa instalación. No resolver con `--force`, booleano de operador ni escritura de una fila de fencing SQLite.

## 8. SQLite, Backup Y Fallos

- No cambiar `app/migrations.py`, callbacks 1–28 ni ejecutar migraciones históricas sobre canónico. CP2-A demuestra que `executescript` puede confirmar implícitamente; estados legacy parciales/ledger/schema desconocidos se preservan y requieren staging/revisión según el motor ya aprobado, no una reparación de CLI.
- La ruta debe corresponder al volumen autorizado y mismo filesystem cuando el protocolo dependa de atomic replace. Canonicalización + descriptor/no-follow reduce symlink swap; comparar volumen/device e inode. Docker named volume no es identificable con seguridad sólo por `/data`; el host/Portainer debe confirmar nombre/ID/mount exacto. Sin prueba del filesystem objetivo, NO-GO.
- Incluir `-wal`, `-shm`, `-journal` en inspección. No borrarlos a mano ni asumir que ausencia de archivos al instante de una lectura prueba que no hay conexión. Rechazar journal mode/sidecars fuera de política del motor `SafeMigrationEngine`; no mutar el origen para hacerlo apto.
- Backup mediante `sqlite3.Connection.backup()` a un destino staging nuevo, permisos restrictivos, hash de archivo, cierre correcto, `integrity_check`, `foreign_key_check`, ledger y manifest esperado, generation/data digest, restore a archivo aislado y repetición de esas validaciones. Comparar generación completa con target; un backup íntegro pero de v27 no satisface objetivo v31.
- El backup no se crea dentro de la transacción bootstrap. Mantener original y backup inmutables durante operación; ubicación, cifrado, retención, custodia y restore del volumen son decisiones operacionales pendientes. `0600` no equivale a cifrado ni protege frente a root.
- `BEGIN IMMEDIATE` serializa writers SQLite que comparten archivo/local locking; el recheck se calcula en la misma conexión/transacción. Grants, ownership, CAS, invalidaciones permitidas, audit v31 y estado final se confirman en un único commit. El código actual de auditoría puede registrar en transacción caller-owned; helpers de asignación actuales no implementan el primer bootstrap atómico.
- Tras commit, cerrar conexión, reabrir en modo read-only y validar esquema v31, `integrity_check`, FK, actor/grants exactos, owners y conteos, eventos/auditoría únicos correlacionados, estado bootstrap y ausencia de capabilities ejecutables. Esto demuestra estado lógico tras reopen, no durabilidad del dispositivo ante power-loss.
- Ante fallo pre-commit: rollback SQLite, no activación; verificar estado previo. Ante respuesta perdida/crash después del commit: no repetir la escritura; inspeccionar operation ID, manifiesto y todos los invariantes. Si exactos, retry es sólo lectura; si contradictorios o ambiguos, preservar evidencia y bloquear. Nunca completar ledger/eventos/ownership a mano.
- Restaurar backup antiguo puede reintroducir sessions, approvals o capacidades anteriores y deshacer revocaciones. Restore exige ingreso cerrado, copia aislada, restore/integridad/schema verificados y nueva política explícita de revocación/reconciliación antes de runtime. No reactivar el runtime anónimo actual.

Pruebas de integridad (SQLite integrity/FK y restore) no acreditan durabilidad ante caída de host, `fsync`, caché de hardware, filesystem remoto, snapshots Portainer, Qdrant ni consistencia distribuida. SIGKILL de proceso tampoco equivale a pérdida de energía.

## 9. Modelo De Amenazas

| Amenaza | Mitigación / evidencia que debe verificarse | Riesgo residual |
|---|---|---|
| Operador no autorizado | UID/GID efectivo, TTY local, allowlist por instalación, operación auditada; el OS/infra debe acreditar membresía | Root/administrador del host puede eludirla; identidad humana y autorización institucional requieren control externo |
| CLI con privilegio excesivo | No root por defecto, no grupo Docker/socket, permisos read-only para consulta, escritura tardía y acotada | Propiedad/ACL mal configurada o helper privilegiado comprometido puede ampliar impacto |
| Acceso accidental a otra instalación/volumen | ID explícito, ruta canónica, device/inode/mount ID y release esperados; confirmar volumen real en host/Portainer | Named-volume identity depende del motor y namespace; path solo no prueba destino |
| Symlink/cambio de ruta entre check/open | lstat + no-follow/openat, descriptor retenido, comparar fstat/stat/device/inode y revalidar antes de transacción | Un atacante con mismos privilegios/root puede cambiar namespace/mount; portabilidad varía |
| Volumen equivocado | Verificación independiente del mount/volume ID y configuración de stack; no inferir de `/data` | Evidencia de Portainer/host no es atestada por SQLite; error humano sigue posible |
| Escritor SQLite concurrente | Parada supervisor, DatabaseLease EX, BEGIN IMMEDIATE, busy timeout explícito y recheck | Locks sólo cooperativos/locales; procesos legacy, NFS y locks defectuosos pueden escapar |
| Runtime HTTP anónimo todavía accesible | Cerrar ingress en proxy/firewall/Portainer y mantener cerrado tras commit; CP3-B debe integrar auth y readiness fail-closed | La versión observada acepta API anónima; nadie debe arrancarla/abrir ingress tras transición |
| Ejecutores externos activos | Inventario de workers/colas, revocación de credenciales o fencing epoch efectivo, reconciliar efectos | No hay prueba automática universal; ejecutor desconocido es bloqueo de apply |
| Consentimiento obsoleto/reutilizado | Hash plan canónico liga instalación, destinatario, generación, inventory, backup, IDs y operación; expira y se vuelve a verificar bajo transacción | Hash no autentica ni firma; TTY reduce errores pero no acredita comprensión del operador |
| Backup no verificable/incompatible | Backup API, hash, restore aislado, schema/generation/integrity/FK y comparación al objetivo | No prueba custodia, confidencialidad, hardware durability ni stores externos |
| Interrupción durante bootstrap | Una transacción SQLite; rollback precommit; auditoría/operation ID + post-check tras reopen; recovery sólo offline | Fallo host/storage puede tener semántica distinta; transacción no cubre efectos externos |
| Reintentos contradictorios | Operation ID único y comparación exacta de manifiesto/audit/generation; repetición idéntica sólo lectura | Si el audit o soporte de operation ID no queda integrado correctamente, crash postcommit requiere intervención manual |
| Logs/salida filtran secretos | Allowlist de campos, mensajes tipados, no dump exception/SQL/manifest sensible; passwords sólo prompt oculto | OS audit, shell history y captura de terminal dependen del host; el operador puede grabar pantalla |
| Recovery reexpone anónimo o revive capabilities | Recovery sin start/ingress, denegar estado ambiguo; restore obliga a revocación/reconciliación y versión autenticada | Runtime actual no tiene auth gate; control de activación pertenece al despliegue posterior |
| Symlink/volumen no confiable o filesystem sin garantías | Rechazar mount y filesystem no soportado, validar locks/rename/fsync en entorno objetivo | Sólo prueba operativa en el host/version concretos acredita propiedades relevantes |

## 10. Recuperación Controlada

`recover` comienza en modo read-only y clasifica: estado previo intacto, operación bootstrap íntegramente comprometida, promoción/migration journal reconocido, o estado ambiguo/desconocido. Sólo los estados exactos ya cubiertos por el protocolo CP2 pueden tener una acción determinista; otro estado conserva DB/backup/journal/staging y requiere revisión humana especializada. Nunca auto-repara esquema, completa audit, reasigna owners ni elimina sidecars.

Si bootstrap no ha hecho commit: se conserva estado inicial, se descarta sólo material temporal con path/ownership verificados y se vuelve a preparar backup/plan/consentimiento frescos. Si commit pudo ocurrir: inspección reabierta y comparación total por operation ID; no repetir hasta demostrar el resultado. Si restaurar: restaurar a destino nuevo/copia, validar esquema/generación y reconocer que se revierten también revocaciones; mantener todo ingress cerrado y repetir fencing/reconciliación. En todos los casos, servicio anónimo permanece NO-GO.

## 11. Contratos Y Pruebas Aisladas

No se añade runner paralelo de bootstrap ni se modifica el simulador CP2. Harness permitido debe aceptar únicamente objetos/valores sintéticos en memoria o fixtures bajo un directorio temporal recién creado, nunca importar rutas desde `NEXO_DATA_DIR` ni permitir nombre `nexo.sqlite3`. Tests no llaman HTTP, Docker, Portainer, providers, MCP, Qdrant ni OS supervisor real; no abren SQLite real.

Contratos mínimos futuros:

| Área | Casos sintéticos que deben fallar si se viola contrato |
|---|---|
| Selección de instalación | IDs/rutas ausentes, relativos, ambiguos, instalación equivocada, volumen/device cambiado |
| Identidad/permisos operador | UID no allowlisted, grupo/ACL insegura, root no aprobado, stdin/stderr no TTY, modo lectura no puede escribir |
| Path | symlink en cada componente, swap simulado entre selección/open, hardlink según política, DB no regular, parent no confiable, mount distinto |
| Consentimiento | cada campo ligado modificado, plan expirado, UUID repetido, TTY ausente, respuesta incorrecta, flag/env/artefacto no firmado, secreto jamás serializado |
| Generación/manifiesto | ledger gap/futuro, schema drift, inventory/relación/estado operativo nuevo, backup de otra generación |
| Exclusividad | lock ocupado/timeout, executor sin evidencia, ingress abierto, proceso vivo, run activo, approval executing, fence expirado |
| Fail-closed y recuperación | error por frontera transaccional, rollback íntegro, crash pre/post commit, retry idéntico de sólo lectura, retry contradictorio, journal corrupto preservado |
| Redacción | no aparecen password/hash/token/API key/contenido/path sensible en stdout, stderr, logs o errors |
| Separación de comandos | inspect/status no crean ni escriben; backup-verify no publica/restaura canónico; plan no muta; sólo apply y comandos mutativos alteran datos |

La prueba sintética acredita contratos del harness, no autoridad operacional. Test de permisos POSIX en temp y SQLite local tampoco acredita ACL/configuración del host real, volumen Portainer, procesos externos, fencing, pérdida de energía ni activación. La evidencia se etiquetará `prototype-verified`; sólo pruebas conectadas al sistema, entorno y procedimiento reales pueden elevar su categoría, y requieren autorización independiente.

## 12. Dependencias De CP3-B Y Bloqueos De Activación

Antes de implementar CLI productiva, CP3-B debe cerrar y probar: interfaz de persistencia/identidad con schema v31; adaptación de bootstrap que no llame al asignador transaccional actual como si fuera anidable; catálogo de grants mínimo y alineado con CP1; verificación de operator/install identity; selección de volumen/ACL y matriz de OS/filesystems soportados; integración con supervisor para cierre ingress/parada/drenaje; inventario completo de executors, retries, approvals/interactions y effects; mecanismo real de fencing o bloqueo cuando no exista; formato final de manifiesto/consentimiento y protección antireplay; custodia/cifrado/retención/restauración de backups; recovery tras crash y policy de revocación al restaurar; auditoría y redacción; revisión de seguridad independiente.

La CLI por sí sola no habilita servicio. CP4/runtime aún debe integrar guard, deny-before-effects, rutas, storage errors y readiness autenticada; debe prohibir startup público en `schema_installed`, bootstrap inválido, journal pendiente, schema incompatible o estado de auth ambiguo. La aplicación observada carece hoy de ese guard. Hace falta además aprobación `operational-verified` del entorno: HTTPS/proxy/Host/Origin, ingress, volumen real, UID/GID/ACL, backup restore, fence de todos los consumidores y runbook Portainer/host.

## 13. Criterios GO / NO-GO

**GO sólo para diseño y pruebas aisladas** si la revisión acepta la recomendación y mantiene el alcance offline. **NO-GO** para crear o instalar ejecutable, apuntar a volumen real, cambiar contraseña real, bootstrap/ownership, ejecutar restore o migraciones, detener/desplegar servicios, tocar configuración/Docker/runtime/middleware o habilitar ingress en este checkpoint.

Una futura aprobación de implementación tampoco equivale a autorización de operación. `bootstrap-apply` queda NO-GO si falta cualquiera de: identidad/autoridad del operador y target inequívocos; schema/generation admitidos; backup restaurable del mismo generation; consentimiento fresco; ingress cerrado; censo completo y parada confirmada de procesos; cada executor externo fenceado o ausente con evidencia; lease exclusivo; transacción CP2 completa y rollback probado; post-check; runtime autenticado y readiness fail-closed; aprobación operacional del host/volumen.

No se usó Software Docs Shared ni se creó propuesta: este documento registra una recomendación para revisión, sin mutar repositorio externo. Si se decide que la separación host CLI/supervisor constituye una decisión arquitectónica aprobable en Software Docs, deberá someterse explícitamente en MY WORK en una etapa autorizada.

## 14. Verificación Y Estado

- Preflight: HEAD esperado `4270a017c1482269ab2bf03339323338ea12810b`, `main`, worktree limpio; stash histórico localizable y commit íntegro.
- Artefacto de esta etapa: este documento únicamente; no se añaden pruebas porque todos los contratos propuestos deben probarse contra un adaptador/harness futuro autorizado y no existe implementación productiva de CLI. Los simuladores actuales permanecen sin cambios y aislados.
- Avisos/herencias conocidas en informes previos: warnings Python Starlette `BlockingPortal`/FastAPI `on_event` y avisos Node `MODULE_TYPELESS_PACKAGE_JSON`; se reportará el resultado actual de quality gate por separado.
- Sin commit, push, merge, despliegue, acceso a SQLite productiva ni inicio de CP3-B/Stage 15C.
