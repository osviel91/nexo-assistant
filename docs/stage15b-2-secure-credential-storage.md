# NEXO Stage 15B.2: Receptor y almacenamiento seguro de credenciales

- Estado: diseño y prototipo aislado; detenido para revisión
- Base verificada: `cf74533d2caa53d113a6d5f11b3a719b610a7163`
- Tokens: sintéticos únicamente (`FAKE_*`); sin login OAuth ni llamadas al proveedor
- Stage 15C: bloqueada; no hay autorización para conectar credenciales reales

## Decisión

**GO condicionado para continuar revisando el diseño y el prototipo local; NO-GO para despliegue, recepción de tokens reales o integración con NEXO.** La arquitectura recomendada conserva el SSH del host y el receptor forzado de 15B.1, pero entrega el payload a un gestor de credenciales aislado. El gestor mantiene SQLite propio con ciphertext AES-256-GCM y una clave fuera de ese almacén. La activación requiere una confirmación administrativa posterior que referencia la huella del ciphertext exacto.

Este prototipo no se conecta al runtime ni a SQLite de NEXO. NEXO hoy carece de autenticación/autorización general y ejecuta el contenedor como root. Esos dos hechos impiden identificar un administrador de NEXO y limitar de manera verificable qué proceso puede pedir un token. Stage 15C no puede empezar hasta cerrar esas condiciones y aprobar explícitamente este diseño y las operaciones para credenciales reales.

## Inspección y estado inicial

- Se leyó `AGENTS.md` y la documentación de Stage 15A, 15B, 15B.0, 15B.1 y 15B.1-QA.
- `HEAD` inicial: `cf74533d2caa53d113a6d5f11b3a719b610a7163`, que comienza con la base solicitada `cf74533d`.
- `git status --short` inicial no mostró cambios; `git status --short --branch` indicó `main...origin/main [ahead 2]`.
- Compose normal y Portainer sólo montan `nexo-data:/data` para el backend. No declaran Docker secrets, un gestor externo, un receptor de credenciales ni una clave de cifrado.
- El Dockerfile ejecuta Uvicorn como root (no define `USER`). No se debe tratar ese contenedor actual como un límite de privilegio para credenciales OAuth.
- `providers.api_key`, credenciales MCP y AEMET se guardan en SQLite sin cifrar. La API evita devolver parte de estos secretos, pero eso no cifra backups/volumen ni da autorización de usuario.
- 15B.1 demostró OpenSSH con usuario/clave restringidos y stdin en un contenedor desechable. Su receptor valida y consume la transacción, pero no persiste el payload; no demuestra un handoff al host de producción ni persistencia cifrada.
- No se inspeccionó ni modificó el host Portainer, su SSH, firewall, volúmenes o despliegues.

## Arquitectura recomendada

```text
Mac: helper SIWC oficial
  - loopback OAuth, PKCE/state/nonce, validación de issuer/audience/firma/scopes
  - destino SSH y host key aprobados fuera de banda
  - credenciales sólo en memoria; envío por stdin
        |
        | SSH host existente; usuario dedicado, key restringida,
        | ForceCommand, sin shell/PTY/forwarding, payload acotado
        v
Host Docker: receptor SSH restringido (UID nexo-import)
  - autentica transacción provisionada y schema; no posee Docker socket
  - no puede leer SQLite de NEXO ni ejecutar comandos arbitrarios
        |
        | IPC local dedicado, protocolo estrecho y permisos por UID
        v
Host: gestor de credenciales (UID separado)
  - SQLite propio con ciphertext AES-GCM y estado/replay transaccional
  - clave montada/provisionada fuera de DB, volumen y backups de ciphertext
  - sólo este componente descifra; ningún token en respuesta de estado
        ^
        | socket Unix dedicado montado en modo mínimo, identidad de peer
        | y sólo operaciones autorizadas; no Docker API/socket
        |
NEXO: API autenticada de administración -> autorización/confirmación explícita
NEXO: componente proveedor autorizado -> obtiene token en memoria al usarlo
```

El gestor/receptor no recibe autoridad sobre el contenedor, Docker socket o SQLite de NEXO. La comunicación host-contenedor futura sería un socket Unix dedicado a operaciones nominales, con permisos de filesystem y verificación de peer; no un puerto publicado, shell, `docker exec` ni montaje de `/data`. Preferiblemente el broker ejecuta con UID propio y sólo el proceso proveedor autorizado puede invocar `get-active-credential`. La aplicación actual no cumple esa separación: es root y no autentica administradores. El diagrama es destino de diseño, no una topología implementada o probada.

### Identidades y autorización

- `nexo-import`: cuenta de sistema dedicada en el host, no root, sin shell interactiva, TTY, forwarding, rc de usuario ni comandos elegidos por cliente. Sólo una clave dedicada, inscrita/revocada por operador fuera de banda. La clave SSH autoriza transportar una carga acotada; no equivale a usuario/admin NEXO.
- Administrador NEXO: principal autenticado de la aplicación, debe crear una transacción de importación de un uso, revisar identidad/metadata y huella, y confirmar explícitamente. Importar nunca activa.
- Gestor: única identidad con permiso de leer la clave externa y escribir la base cifrada. Fail closed si la clave falta, no se puede leer o no valida.
- Proveedor/runtime: consumidor autorizado de credenciales activas a través de API local mínima; nunca recibe acceso a la base/clave directamente ni expone tokens en HTTP/SSE/diagnóstico.
- Antes de construir una integración real se debe definir autenticación web, política de operador único/múltiple y autorización de `begin`, `confirm`, `status`, `use`, `renew` y `disconnect`. En la versión actual no hay principal autenticado que pueda llenar ese contrato.

## Almacenamiento y operación Docker/Portainer

| Alternativa | Persistencia y reinicio | Backups/recuperación/rotación | Evaluación |
| --- | --- | --- | --- |
| AEAD de aplicación, DB propia y clave externa | Ciphertext sobrevive reinicio en volumen persistente; sin key el gestor falla cerrado | Backup coordinado de DB cifrada más copia de clave bajo canal/administrador independiente. Pérdida de key implica reautorización. Rotación: leer con key antigua, validar/re-encriptar transaccionalmente con nueva, verificar y sólo entonces retirar la vieja; recuperación ensayada | **Recomendada como objetivo**: no depende de servicio externo y protege copias aisladas de SQLite/volumen. Requiere custodia real de key, hardening de proceso/IPC y runbook. |
| Gestor externo (Vault/KMS/secret manager) | Persistencia y disponibilidad dependen del servicio; exige conectividad e identidad de workload | Backup, recuperación, rotación y auditoría pueden centralizarse en el gestor; requiere configurar identidad/ACL, alta disponibilidad y recuperación del propio gestor | Mejor para despliegues con infraestructura operada, pero no existe ni está configurado en el entorno Portainer inspeccionado. No inventar que Portainer provee uno. Reevaluar si operador ya tiene servicio administrado. |
| Volumen restringido con cifrado de aplicación | Volumen Docker persiste, pero permisos del volumen no cifran datos; el ciphertext aún necesita key | Copia/restauración del volumen no basta. Si ciphertext y key van en el mismo backup/volumen, la protección se pierde. Borrado/rotación heredan límites del filesystem y snapshots | **No seleccionada como alternativa autónoma**. Permisos son control complementario, no sustituto de AEAD ni de key separada. |

La recomendación concreta para una instalación Portainer sin gestor existente es DB/ciphertext en almacenamiento persistente separado y key de 32 bytes suministrada como secreto de despliegue montado como archivo de sólo lectura, ubicado fuera del volumen y del backup de datos. Docker Swarm secrets no se deben asumir en un stack standalone; validar el modo real de Portainer y ACL/UID del bind mount antes de elegir provisión. La copia de recuperación de la key debe quedar bajo control administrativo y dominio de backup separado del ciphertext. No usar variables de entorno, Dockerfile, imagen, SQLite, misma carpeta/volumen de datos ni argumentos de proceso para la key.

Sin key al arrancar: no crear una nueva key automáticamente, no degradar a plaintext, no sobrescribir ciphertext; marcar secreto inaccesible y requerir recuperar la key válida o reautorizar. Copiar DB sin key no recupera credenciales. Copiar DB y key juntos al mismo backup sin protección independiente anula la separación. Revocar/rotar una key no elimina copias históricas ya capturadas.

## Contratos de recepción y almacenamiento

- Formato: versión explícita, transacción UUID provisionada, vencimiento corto (máximo 5 minutos), campos allowlisted y payload total <=16 KiB. Rechazar claves JSON duplicadas, campos desconocidos, tipos incorrectos, cadenas vacías/sobredimensionadas y expiración inválida. El helper valida OAuth/issuer/audience/scopes antes de empaquetar; el receptor no convierte bytes recibidos en prueba de validez OAuth.
- Destino: helper acepta sólo host/usuario/puerto y host key/CA aprobados; `StrictHostKeyChecking=yes`, sin forwarding, sin redirecciones/proxy de origen arbitrario, sin token en URL, argv, shell, archivo temporal, portapapeles persistente ni stdout. `ssh-keyscan` no autentica la clave inicial.
- Autorización de importación: admin autenticado emite una transacción aleatoria de un uso ligada a destino/operador; la solicitud sólo puede consumirla antes del vencimiento. El proceso SSH no puede crear transacciones ni confirmar activación.
- Cuarentena: el gestor persiste como `pending`; la respuesta de recepción sólo indica el ID no secreto/estado. No se permite obtener token pending.
- Activación: admin vuelve por canal autenticado, verifica metadata y huella estable del ciphertext/importación exacta y confirma. Huella distinta, principal incorrecto, estado ya consumido o repetición se rechazan. Una recepción no produce activación implícita.
- AEAD: AES-256-GCM, nonce aleatorio nuevo de 96 bits por cifrado; AAD incluye versión de formato y UUID interno de credencial. Persistir algoritmo/versión de forma versionada al implementar. No reutilizar nonce con la misma key. Alterar ciphertext/nonce/AAD falla la autenticación y se trata como credencial inaccesible, nunca como texto alternativo.
- Estado/replay: transacción SQL serializada; autorización de importación se consume al guardar ciphertext en la misma transacción y `transaction_id` tiene unicidad durable. Marcadores/estado no llevan token. Reinicio no rearma transacción ni activa carga pendiente.
- Estado: metadata sólo devuelve `pending`, `active`, `expired`, `needs_reauth`, `disconnected` o `absent`, fechas necesarias y huella de confirmación si aplica; nunca token, key, headers, cuerpo OAuth o ciphertext.
- Acceso: sólo componente de credenciales autorizado puede solicitar en memoria el conjunto activo y no expirado. No ofrecer endpoint público de lectura; no persistir tokens descifrados en caché/logs/traces.
- Renovar/sustituir: serializar refresh por cuenta. Mantener credencial activa hasta que respuesta válida nueva se haya validado y cifrado; reemplazar access/refresh/expiry/scope como unidad transaccional; no reintentar a ciegas un refresh rotatorio tras resultado ambiguo. Validar identidad/subject/client ID antes de sustituir y exigir confirmación para una cuenta distinta. Nunca escribir plaintext temporal.
- Expirar/errores: expiration impide entrega aunque ciphertext siga almacenado; fallos de refresh o scope faltante pasan a estado no utilizable/reautorización y no revelan bodies/headers. Los estados y mensajes externos son genéricos; el log puede incluir categoría/correlation ID no secreto.
- Desconectar/eliminar: desactivar primero para bloquear nuevas lecturas; intentar revocación oficial si aplica; borrar ciphertext/nonce y estado local sólo tras registrar resultado no sensible. Si revocación remota no confirma, decirlo y remitir al usuario a la configuración del proveedor. La eliminación lógica no garantiza borrado forense de WAL, snapshots, backups, SSD o memoria; la protección de backups es esencial. Con key compartida, destruir la key impide recuperar todas las credenciales y no es una operación selectiva de borrado.

No se registran tokens, credenciales completas, OAuth code, PKCE verifier, `Authorization`, cuerpo/respuesta del proveedor ni excepción cruda de librería. Evitar volcados de memoria/core y swap no cifrado en la política de host; el cifrado en reposo no protege un host/root comprometido ni la memoria del proceso durante uso.

## Modelo de amenazas

| Amenaza | Controles necesarios | Riesgo residual / estado de evidencia |
| --- | --- | --- |
| Clave SSH robada o transferencia no autorizada | Usuario/key dedicados, forced command, esquema/tamaño acotados, transacción provisionada de un uso, caducidad, revocación operativa | Una carga puede ocupar la transacción y causar DoS. 15B.1 probó restricciones SSH en Docker desechable; no host real. |
| Replay/condición de carrera/reinicio | Consumo y escritura de ciphertext en una transacción DB con unique ID; expiry comprobado bajo lock; confirmación one-shot | Prototipo cubre replay secuencial y persistencia; carga concurrente/múltiples procesos y crash-injection requieren prueba adicional. |
| Sustitución entre transferencia y confirmación | AEAD, ID ligado en AAD, huella revisada por administrador y confirmación de esa huella | Huella detecta diferencia, no demuestra que el OAuth sea legítimo; esa validación pertenece al helper OAuth. |
| Lectura de backup/volumen | AEAD con key fuera de la DB y del backup; ACL del host; evitar inclusión accidental de key en copia | Quien obtiene key y ciphertext descifra; root/host comprometido puede leer memoria o pedir descifrado. No verificado en backup Portainer. |
| Pérdida/rotación defectuosa de key | Backup recuperable independiente, versión/identificador de key, ensayo de restore y rotación con verificación previa | Pérdida de key implica reautorizar; no hay recuperación criptográfica. Sin runbook probado, NO-GO real. |
| Acceso de un usuario NEXO no autorizado | Authn/authz de NEXO y autorización por principal para todas las operaciones sensibles | Actualmente no existe authn/authz general. Bloqueador. |
| Compromiso del proceso/contenedor NEXO | Proceso broker separado, UID no-root, socket privado y autorización por peer; no montar DB/key en app web | Dockerfile actual ejecuta como root. En app comprometida puede haber lectura de respuestas en memoria; aislar y endurecer antes de Stage 15C. |
| MITM/host SSH falso | Host key/CA pin aprobada fuera de banda, strict check; SSH en el host | Pruebas previas sólo usaron fixture desechable. Aprovisionamiento real de fingerprint y rotación pendientes. |
| Payload malicioso/grande/inyección | Parseo estricto, límites bytes/campos, JSON duplicado, no shell, SQL parametrizado, errores sin echo | Prototipo cubre schema/tamaño/duplicados vía receptor previo y campos/UUID vía store; fuzzing y límites host completos pendientes. |
| Filtración de secretos en diagnósticos/logs | Diagnóstico allowlist, receipt redacted, sin body/headers/excepciones; pruebas de búsqueda con tokens sintéticos | Prototipo verifica diagnóstico y archivo DB; no prueba SSH productivo, crash dump, observabilidad o proceso NEXO. |
| Expiración/revocación y token rotatorio | Resolver de credencial comprueba expiry, refresh serializado y reemplazo atómico; desconexión local bloquea antes de revocar | No se habla con OAuth ni proveedor; formato/endpoints se deben revalidar con fuentes SIWC antes de uso. |

## Alternativas de transporte y frontera

Se conserva la decisión 15B.1: SSH exec por stdin, key dedicada, host key fijada y `ForceCommand` restringido en el host existente. No se instala SSH dentro del contenedor. SCP/SFTP crea archivo secreto temporal; shell SSH irrestricta concede ejecución arbitraria; HTTPS público requiere auth NEXO inexistente; túnel SSH añade listener/protocolo y no es necesario para carga de una vez. Ninguna alternativa concede acceso general al Docker API, contenedor o DB.

La frontera host-contenedor propuesta es un socket Unix dedicado y protocolo allowlisted, con permisos/UID y límites. No montar una ruta de datos o la base al usuario SSH. Antes de confiar en esta frontera hay que probar la semántica real de bind-mount de socket en el modo Docker/Portainer elegido, permisos después de recrear/reiniciar servicios, política MAC (si aplica), timeouts, límites, auditoría y recuperación. El prototipo de almacenamiento no implementa ni verifica ese IPC.

## Prototipo aislado y resultados

- `prototypes/credential_store.py`: SQLite separado con estados `pending`/`active`, importación de un solo uso, AES-GCM, AAD con ID, keyfile externa y modo POSIX restrictivo, confirmación admin + huella, consulta sin secretos, lectura sólo para nombre de componente permitido, expiración, eliminación y diagnósticos de conteo.
- `tests/test_stage15b2_credential_store.py`: seis pruebas con valores `FAKE_*`; prueba persistencia al recrear store, ausencia de token en el archivo SQLite, key equivocada, ciphertext manipulado, no activación previa, autorización/replay, confirmación y bloqueo tras borrar.
- Dependencia exclusiva del prototipo: `prototypes/requirements-stage15b2.txt` fija `cryptography==50.0.2`; no se añade al entorno principal de NEXO ni depende de una dependencia transitiva.
- Instalar y ejecutar la fase focal: `python -m pip install -r prototypes/requirements-stage15b2.txt` y `python -m unittest tests.test_stage15b2_credential_store -v`.

Lo demostrado se limita a lógica local con una sola base y un mismo filesystem. La autenticación `admin` y el nombre `credential-provider` son stubs contractuales del test, no autenticación real: cualquier caller de Python con acceso al objeto puede falsificarlos. El test de re-creación representa restart del objeto/proceso pero no mata/reinicia contenedor ni valida Portainer. No implementa el SSH, no hace handoff desde el receptor 15B.1, no usa OAuth, no hace refresh/revocación y no expone socket/API. No es un servicio, no debe ejecutarse como daemon ni distribuirse como importador funcional.

## Limitaciones y bloqueos antes de Stage 15C

1. Aprobar explícitamente identidad/autoridad administrativa de NEXO y autenticación de toda acción sensible. La red/LAN/SSH no sustituye un principal de aplicación.
2. Definir y ensayar separación de UID/proceso para receptor, gestor y proveedor; corregir el despliegue para no ejecutar el web runtime como root; impedir acceso al Docker socket, SQLite o key por el receptor SSH.
3. Decidir el mecanismo soportado de provisión/backup de la key en Portainer standalone concreto; probar backup/restauración, key ausente/incorrecta, rotación y pérdida; definir recovery con reautorización.
4. Diseñar el protocolo/ACL del IPC host-contenedor, concurrencia multiworker, crash/rollback, caducidad/limpieza de pending, límites/rate/conexiones y confirmación concurrente.
5. Revalidar elegibilidad y documentación SIWC, schemas y lifecycle actuales; probar localmente OAuth, validación OIDC/scopes, refresh rotatorio, cuenta equivocada, revocación y expiración con fixture/mock, nunca credenciales reales en esta etapa.
6. Pruebas de no filtración en salida del cliente, logs SSH, broker, aplicación, telemetría, diagnósticos, errores, backups y crash handling; pruebas negativas con fuzz/oversize, replay concurrente, cambio de huella, ciphertext/AAD y restore.
7. Confirmar host-key enrollment/rotación y operaciones del host por administrador. No modificar producción SSH ni desplegar durante 15B.2.

No cambiar SQLite/schema/migrations existente en esta etapa. La DB del prototipo no es una migration ni contrato de persistencia de NEXO.

## Recomendación

**GO** para revisión del diseño y mantener el prototipo sintético local. **NO-GO** para credenciales reales, despliegue o integración de proveedor hasta aprobar la arquitectura y satisfacer las condiciones anteriores. Stage 15C permanece explícitamente bloqueada; esta etapa se detiene aquí para revisión.
