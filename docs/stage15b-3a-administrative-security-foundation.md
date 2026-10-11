# NEXO Stage 15B.3A: Fundamento de seguridad administrativa

- Estado: diseño y prototipo aislado; detenido para revisión
- Base verificada: `f9e72ee398ca89d3985ead63d21129b544ef03c5`
- Rama: `main`, tres commits adelantada de `origin/main`
- Alcance: auditoría del runtime, arquitectura, contrato de identidad/autorización y prueba local aislada
- Fuera de alcance: integración con rutas/runtime, SQLite/migraciones, frontend, proveedores, OAuth, SSH, Docker/Portainer productivo
- Stage 15C: bloqueada

## Decisión

**GO para revisión del diseño y el prototipo aislado. NO-GO para desplegar, recibir credenciales reales o integrar controles en el runtime en esta etapa.** La aplicación carece de autenticación y autorización de extremo a extremo; Compose publica el puerto HTTP y no configura TLS ni proxy. Hay acceso anónimo a configuración, claves funcionales, historial conversacional, acciones aprobables, ejecuciones de herramientas y datos RAG/diagnósticos. Antes de cualquier exposición fuera de un entorno local controlado se necesita un checkpoint separado de implementación y validación.

## Inspección y estado

- Se leyó `AGENTS.md`, `app/main.py`, módulos con rutas, Dockerfile, Compose normal/Portainer, README, documentación de desarrollo y diseños 15B previos.
- `HEAD` coincide exactamente con la base solicitada. Worktree inicial limpio; rama `main...origin/main [ahead 3]`.
- `git stash list` mostró `stash@{0}: On main: checkpoint: preserve pre-alignment worktree cce925b7`; se preservó intacto.
- No existe middleware, dependencia de autenticación ni sesión general. No hay WebSocket. Chat y reanudación de aprobaciones usan SSE mediante `StreamingResponse`.
- `app/main.py` combina registro de rutas, acceso SQLite, secretos, proveedores y AgentRuntime. Los módulos pueden añadir rutas: `attachments` añade `/api/files`; `decision-runtime` añade `/api/decisions` sólo si se habilita.
- `Dockerfile` ejecuta Uvicorn como root. Compose y Portainer publican el puerto del contenedor; no se observó proxy/TLS configurado en el repositorio. La configuración externa real no se inspeccionó.
- `providers.api_key`, AEMET y tokens MCP están en SQLite. Parte no se devuelve por la API, pero el DB, volumen y backups no quedan protegidos frente a lectura.
- Persistencia `/data` es volumen nombrado `nexo-data`. Qdrant tiene volumen separado. No se debe recrear/eliminar el volumen para probar autenticación.

## Inventario de superficie de ataque

Estado actual en toda ruta API enumerada: **anónima**, sin validación de sesión o principal. “Admin” significa la política recomendada; no es el comportamiento actual. `Sesión` designa la futura sesión humana iniciada, `Pública` sólo health mínimo y assets estáticos. Toda mutación bajo sesión requiere CSRF. No se considera autenticación el conocimiento de URL, red LAN, cookie del proxy o ejecución interna.

| Método y ruta | Función / datos tratados | Riesgo | Política recomendada |
|---|---|---:|---|
| `GET /api/health` | Liveness y estado/version de vector store | Bajo/Medio | Público sólo con `{status}` y versión no sensible; separar readiness detallada como admin/infra |
| `GET /api/settings/tools` | Límites y presencia de clave AEMET | Medio | `config:read` |
| `PUT /api/settings/tools` | Configuración runtime y clave AEMET | Crítico | `config:write` |
| `GET /api/settings/retrieval-benchmark` | Estado/resultado benchmark y notebook | Medio | `diagnostics:read` |
| `POST /api/settings/retrieval-benchmark` | Consume CPU, accede notebook/RAG | Alto | `diagnostics:run` |
| `GET /api/settings/embeddings` | Configuración, proveedores/modelos, salud RAG | Alto | `rag:read` + `providers:read`; evitar estado detallado para anónimo |
| `PUT /api/settings/embeddings` | Proveedor/modelo de embedding y parámetros/indexación | Crítico | `rag:configure` |
| `POST /api/settings/embeddings/test` | Llamada saliente a proveedor; latencia/coste | Alto | `providers:test` |
| `POST /api/settings/embeddings/test-reranker` | Envía consulta a proveedor configurado | Alto | `providers:test` |
| `PATCH /api/providers/{pid}/models/{model_id}` | Cambia capacidades del modelo | Alto | `providers:write` |
| `GET /api/providers` | Inventario de proveedores/modelos y existencia de secreto | Alto | `providers:read` |
| `POST /api/providers` | Crea proveedor, URL y API key | Crítico | `providers:write`, validación egress/SSRF y CSRF |
| `PUT /api/providers/{pid}` | Cambia endpoint/modelos/clave | Crítico | `providers:write` |
| `DELETE /api/providers/{pid}` | Borra proveedor/modelos relacionados | Alto | `providers:write`, reautenticación para acción destructiva |
| `POST /api/providers/{pid}/refresh` | Petición saliente a endpoint configurado; actualiza modelos | Alto | `providers:test` |
| `DELETE /api/providers/{pid}/models/{model_id}` | Elimina modelo referenciado | Alto | `providers:write` |
| `GET /api/agents` | Lista perfiles, instrucciones y capacidades | Alto | `agents:read` |
| `GET /api/agents/{profile_id}` | Lee configuración/instrucciones de perfil | Alto | `agents:read` |
| `POST /api/agents` | Crea perfil con herramientas y notebooks permitidos | Crítico | `agents:write` |
| `PATCH /api/agents/{profile_id}` | Cambia modelo, instrucciones y allowlist de herramientas | Crítico | `agents:write` |
| `DELETE /api/agents/{profile_id}` | Elimina perfil | Alto | `agents:write` |
| `GET /api/mcp/servers` | Endpoint, nombres/herramientas y presencia de auth | Alto | `tools:read` |
| `POST /api/mcp/servers` | Registra URL remota/token y opcionalmente activa servidor | Crítico | `mcp:write`; validar destino, egress y nunca devolver token |
| `PUT /api/mcp/servers/{server_id}` | Cambia endpoint/credencial/estado | Crítico | `mcp:write` |
| `PATCH /api/mcp/servers/{server_id}` | Habilita servidor | Crítico | `mcp:write` |
| `POST /api/mcp/servers/{server_id}/connect` | Contacta host remoto y descubre herramientas | Crítico | `mcp:connect`; proteger SSRF y recursos |
| `PATCH /api/mcp/servers/{server_id}/tools` | Habilita/deshabilita lote de herramientas | Crítico | `tools:write` |
| `PATCH /api/mcp/servers/{server_id}/tools/{tool_id}` | Autoriza ejecución y clasifica impacto | Crítico | `tools:write`; escalamiento de impacto con reautenticación |
| `DELETE /api/mcp/servers/{server_id}` | Elimina servidor y credenciales | Alto | `mcp:write` |
| `GET /api/modules` | Módulos/capacidades/estado externo | Medio | `config:read`; versión pública reducida si se justifica |
| `GET /api/tools` | Catálogo de herramientas disponibles | Medio/Alto | `tools:read` |
| `POST /api/decisions` (módulo opcional) | Envía estado/preguntas a proveedor de decisiones | Alto | Sesión; permiso `agents:run` o `diagnostics:run` según uso; revisar como ruta de módulo |
| `GET /api/preferences` | Preferencias globales actuales (no por usuario) | Bajo | `session`; futuro dato por usuario |
| `PATCH /api/preferences` | Cambia preferencias globales | Bajo/Medio | `session` + CSRF; particionar por user_id |
| `GET /api/conversations` | Lista títulos/estado de todas las conversaciones | Alto | `chat:read`; filtrar por dueño |
| `GET /api/conversations/{cid}` | Lee mensajes, adjuntos, citas, runtime y approvals | Crítico | `chat:read`; comprobar dueño en consulta |
| `POST /api/chat` | Persiste prompt/adjuntos, llama proveedor/RAG/tools, SSE | Crítico | `chat:write`; propietario y sesión; CSRF en handshake HTTP |
| `PATCH /api/conversations/{cid}` | Cambia título/perfil/notebook/modo y tools | Alto | `chat:write`; dueño y CSRF |
| `DELETE /api/conversations/{cid}` | Borra conversación/mensajes asociados | Alto | `chat:delete`; dueño, CSRF y reautenticación opcional |
| `POST /api/conversations/{cid}/branch/{message_id}` | Copia historial y citas a nueva conversación | Alto | `chat:write`; dueño y CSRF |
| `POST /api/approvals/{approval_id}` | Aprueba/rechaza ejecución de herramienta congelada | Crítico | `tools:approve`; sesión, dueño, CSRF, expiry y confirmación vinculada a usuario |
| `POST /api/interactions/{interaction_id}` | Resuelve interacción del AgentRuntime | Crítico | `agents:interact`; sesión/dueño/run, CSRF y expiry |
| `GET /api/notebooks` | Nombres y metadata de notebooks | Alto | `rag:read`; dueño/ACL |
| `GET /api/notebooks/{notebook_id}` | Metadata notebook | Alto | `rag:read`; dueño/ACL |
| `POST /api/notebooks` | Crea notebook | Alto | `rag:write`, CSRF |
| `PATCH /api/notebooks/{notebook_id}` | Cambia notebook | Alto | `rag:write`, dueño/ACL y CSRF |
| `DELETE /api/notebooks/{notebook_id}` | Borra notebook y referencias | Crítico | `rag:write`, confirmación y CSRF |
| `GET /api/notebooks/{notebook_id}/sources` | Lista fuentes/documentos | Alto | `rag:read`; dueño/ACL |
| `GET /api/notebooks/{notebook_id}/sources/{source_id}` | Metadata de fuente | Alto | `rag:read`; dueño/ACL |
| `GET /api/notebooks/{notebook_id}/sources/{source_id}/document` | Texto canónico extraído | Crítico | `rag:read_content`; dueño/ACL |
| `GET /api/notebooks/{notebook_id}/sources/{source_id}/index` | Estado/identidad/cantidad de vectores | Medio/Alto | `rag:read` |
| `GET /api/notebooks/{notebook_id}/sources/{source_id}/integrity` | Integridad y compatibilidad de índice | Medio | `rag:diagnostics` |
| `POST /api/notebooks/{notebook_id}/sources` | Sube archivo o registra URL web | Crítico | `rag:write`; límite, CSRF y defensa SSRF en URL |
| `DELETE /api/notebooks/{notebook_id}/sources/{source_id}` | Borra fuente/documento | Alto | `rag:write`, dueño/ACL y CSRF |
| `POST /api/notebooks/{notebook_id}/sources/{source_id}/ingest` | Extrae/ingiere documento | Alto | `rag:write` |
| `POST /api/notebooks/{notebook_id}/sources/{source_id}/index` | Llama embedding provider y persiste vectores | Alto | `rag:write` + proveedor configurado |
| `POST /api/notebooks/{notebook_id}/retrieve` | Devuelve pasajes recuperados | Crítico | `rag:query`; dueño/ACL |
| `POST /api/notebooks/{notebook_id}/retrieve/diagnostics` | Expone candidatos/ranks y coincidencias de contenido | Alto | `rag:diagnostics` |
| `POST /api/files` (módulo attachments) | Carga/extracción de archivo en memoria | Alto | `chat:write`; límite de tamaño, sesión y CSRF |
| `GET /api/lab/shadow` | Observaciones de decisiones/prompts derivadas | Alto | `diagnostics:read` |
| `GET /api/lab/diagnostics` | Diagnósticos recientes | Alto | `diagnostics:read`; respuesta sin datos sensibles |
| `GET /api/lab/traces` | Trazas runtime filtrables | Alto | `diagnostics:read` |
| `GET /api/lab/runs` | Runs y metadata de ejecución | Alto | `diagnostics:read` |
| `GET /api/lab/runs/{run_id}` | Run y eventos | Alto | `diagnostics:read` |
| `GET /api/lab/runs/{run_id}/events` | Eventos del run | Alto | `diagnostics:read` |
| `GET /api/lab/traces/{message_id}` | Trazas de mensaje | Alto | `diagnostics:read` |
| `GET /{path:path}`, `GET /assets/*` | Shell/frontend y recursos estáticos | Bajo | Público; no incluir secretos ni datos personales en assets |

No hay endpoint de descarga de archivos persistentes detectado. Fuente canónica y fragmentos RAG sí son descargas de contenido por respuesta JSON. No se detectó WebSocket.

### Clases de acceso

- **Público justificado:** health de liveness mínimo, `index.html` y assets sin datos privados. CORS no es control de acceso.
- **Sesión humana:** chat, preferencias, lectura/escritura de conversaciones propias, interacciones y approvals propios. En la etapa de administrador único se podría autorizar al único administrador, aunque conviene mantener permisos y ownership explícitos desde el contrato.
- **Sólo administración:** proveedores/modelos/secretos, configuración general, agentes, MCP/tools, notebooks/RAG, diagnósticos y laboratorio.
- **Interno de servicio:** llamadas de AgentRuntime a proveedor, retrieval/embedding, invocación de herramientas y ejecución de continuation. No son APIs humanas; deben autorizarse como operaciones con identidad de workload y contexto delegado, no por el mero hecho de ejecutarse dentro del proceso.
- **Health de infraestructura:** readiness detallado para monitor privado; no exponer URL, endpoint, errores de proveedor ni detalles de dependencias al público.

## Modelo de amenazas

Activos: API keys de proveedores, tokens MCP/AEMET, prompts/historial/adjuntos, documentos/notebooks/vectorización, instrucciones y allowlists de agentes, aprobación de operaciones mutativas, configuración de endpoints salientes, disponibilidad/coste y backups.

| Amenaza | Impacto | Controles de destino | Residual |
|---|---|---|---|
| Acceso anónimo o internet/LAN no confiable | Lectura/alteración completa; ejecuciones con secretos | Authn en backend para toda ruta no pública, deny-by-default, ingress privado | XSS/compromiso del browser autenticado mantiene autoridad de sesión |
| CSRF contra administrador | Cambios de proveedor/MCP y approvals | Cookie SameSite, token CSRF sincronizador ligado a sesión, Origin/Referer allowlist como defensa adicional | No protege XSS |
| Robo/replay/fijación de sesión | Suplantación admin | Token aleatorio opaco, hash en almacenamiento servidor, rotación al login, expiración idle/absolute, revocación, sin aceptar ID pre-login | Dispositivo/endpoint comprometido |
| Fuerza bruta/credential stuffing | Toma de cuenta/DoS | scrypt/Argon2id, rate limit por cuenta+origen en backend, respuesta uniforme, backoff y auditoría sin secreto | IP spoofing depende de proxy correctamente confiado |
| Escalamiento por endpoints olvidados/módulos | Operación sensible sin autorización | Guard común/dependencia central y prueba que recorre todas las rutas registradas; módulos deben heredar guard | Rutas añadidas fuera del registro requieren pruebas de contrato |
| IDOR entre conversaciones, runs, notebooks y approvals | Lectura o aprobación de datos/acciones ajenos | ownership/ACL comprobado en cada operación, antes de efectos laterales | Aislamiento de usuario requiere cambio de esquema y migración posterior |
| Abuso de MCP/proveedor como SSRF | Acceso a metadata/red interna o acciones remotas | permiso admin, egress allowlist/resolución IP, bloquear loopback/link-local/private no permitidos, redirects controlados, tool allowlist/approval | DNS rebinding y redes operator-managed necesitan validación runtime |
| Compromiso de runtime/servicio interno | Uso de credenciales o herramientas | identidad workload separada, privilegios mínimos, interfaz interna estrecha, no inferir service trust de cookie | Mismo proceso/root reduce efectividad de separación lógica |
| Exfiltración por SSE/diagnósticos/logs | Prompts, secretos, tool args/results | sesión antes de iniciar stream, ownership; evitar secretos en eventos; redacción allowlist y retención mínima | buffers/proxy y capturas requieren verificación operativa |
| Robo del DB/volumen/backups | Claves API, conversaciones y configuración | ACL/backup cifrado; futuro cifrado de secretos con key fuera de DB; restore probado | Una key accesible al proceso no protege host/root comprometido |
| Proxy/TLS mal configurado | Cookie sin Secure, spoof de IP/esquema, header trust | HTTPS obligatorio en ingress, no confiar `X-Forwarded-*` salvo proxy exacto, TLS al origen según riesgo | Configuración de proxy real desconocida/no incluida |
| Denegación de servicio | SSE, parsing, benchmarks, indexing | auth antes de cómputo, límites por cuenta/IP/concurrencia, límites tamaño/tiempo | límites de infraestructura y cuotas deben decidirse por despliegue |

Fuera de alcance para el único admin: registro abierto, OAuth externo, multitenancy, recuperación remota autónoma. Un administrador capaz de resetear el volumen o leer secretos del host puede recuperar control; el sistema no puede protegerse de root/Portainer owner. La recuperación debe ser operación local documentada y auditada, no cuenta maestra oculta.

## Arquitectura propuesta

```text
Browser -- HTTPS --> Reverse proxy confiable --> NEXO API
                         |                       |
                headers normalizados       AuthN + AuthZ común
                                                 |-- sesiones persistidas/revocables
                                                 |-- owner_id / permisos por recurso
                                                 |-- API humana (CSRF)
                                                 `-- contexto de servicio separado
                                                        |-- AgentRuntime
                                                        |-- proveedores/tools/RAG
                                                        `-- APIs internas estrechas
```

- Fase inicial: exactamente un `user_id=admin`, creado localmente durante bootstrap explícito. Sin signup ni contraseña default. Bootstrap se habilita sólo si no existe admin, requiere consola/operación local con stdin/secret file protegido; falla si la identidad ya existe y se desactiva tras éxito. No exponer un endpoint HTTP de “primer usuario” basado en que no haya sesiones.
- Contraseña: Argon2id (biblioteca directa/pinned si se adopta) o scrypt stdlib como baseline de prototipo; sal individual, parámetros versionados/calibrables y comparación de hash constante. Mínimo de longitud y passphrase; no imponer reglas de composición frágiles. Evitar guardar contraseña en env/logs/argv.
- Backend: dependencia central de FastAPI para autenticar y exigir permiso. Política `deny by default`; declaración explícita pública/admin/session/service. Nunca permitir bypass por configuración de producción.
- Evolución multiusuario: `users`, `sessions`, `roles/permissions` o asignación equivalente, `owner_id`/ACL para conversaciones, notebooks, runs y artifacts; permisos nombrespaced (`providers:read`, `providers:write`, `tools:approve`, `rag:write`, `diagnostics:read`, etc.). No usar booleanos sueltos por endpoint.
- Separación humana/servicio: `Principal(kind="user", user_id, permissions)` frente a `Principal(kind="service", service_id, scopes, delegation_id)`. El workload recibe sólo scopes nominales requeridos; no se acepta una cookie humana como credencial entre servicios. Una operación ejecutada por AgentRuntime conserva identidad de iniciador y permiso delegado, con límites del run snapshot.
- Auditoría: actor, acción, recurso, resultado, timestamp/correlation id; nunca password, session/CSRF token, API key, prompt completo o argumentos/resultados sensibles. Acciones destructivas/secret rotation pueden pedir reautenticación reciente.

## Contratos de autenticación y autorización

| Contrato | Propuesta |
|---|---|
| `POST /api/auth/login` | Sólo recibe username fijo del admin y password sobre HTTPS; respuesta genérica para usuario/clave incorrectos; rate limited. Emite cookie de sesión y token CSRF separado. No devuelve permisos/secretos innecesarios. |
| `GET /api/auth/session` | Requiere sesión; devuelve `{authenticated, user, permissions}` mínimo y CSRF actual; sin session ID ni datos secretos. |
| `POST /api/auth/logout` | Requiere sesión + CSRF; revoca inmediatamente servidor-side y borra cookie con mismos atributos. |
| `POST /api/auth/password` | Reautenticación, CSRF, política de nueva passphrase y revoca todas las sesiones. No hay recuperación HTTP por email. |
| API protegida | 401 sin sesión, 403 con identidad sin permiso, 404 para recurso fuera del ownership cuando evitar enumeración aplique. Errores uniformes, no filtrar existencia de credencial. |
| Mutación browser-cookie | CSRF sincronizador ligado a sesión y comparado en servidor; `Origin` exacto como defensa adicional. No depender sólo de SameSite. |
| API interna de servicio | No accesible desde browser; credencial de workload separada o principal creado por frontera de proceso explícita. En prototipo no se emite identidad de servicio. |

Permisos mínimos previstos: `config:read/write`; `providers:read/write/test`; `secrets:read_metadata/write/rotate` (nunca read plaintext para UI); `agents:read/write/run`; `tools:read/write/approve`; `mcp:read/write/connect`; `rag:read/read_content/write/query/diagnostics`; `chat:read/write/delete`; `diagnostics:read/run`; `system:admin`. MVP mapea al administrador único un rol `admin` con todos estos permisos explícitos. `system:admin` no debe ser implícito en servicios.

## Modelo de sesiones

- Token opaco aleatorio de al menos 256 bits; cookie `__Host-nexo_session` con `Secure; HttpOnly; Path=/; SameSite=Lax` (usar `Strict` si UX valida; nunca `Domain`). No guardar bearer token en localStorage.
- Persistir sólo hash del token, `user_id`, creación, última actividad, expiración absoluta, revocado y metadata mínima. SQLite persistente asegura reinicio sin invalidación accidental; invalidar sesiones en bootstrap/password rotation. DB no sustituye control filesystem/backup.
- TTL configurable dentro de límites de política, por ejemplo idle 30 min y absolute 12 h; renovar sólo tras autenticación válida, rotación de ID, evitar sliding infinito. El prototipo prueba TTL absoluto y revocación; no implementa persistencia/idle timeout.
- Login siempre emite ID nuevo; no aceptar session ID aportado antes del login. Al cambiar permisos o contraseña, revocación selectiva/global inmediata.
- CSRF synchronizer random, no en cookie autoenviada; obtenerlo por endpoint autenticado y enviarlo en header `X-CSRF-Token` para mutaciones. SSE es GET una vez creado pero su petición inicial `POST /api/chat` debe autenticar y verificar CSRF antes de mandar headers/abrir generación. Cookie se envía con EventSource/fetch del mismo origen.
- SSE `POST /api/chat` y continuation de approval: autorizar antes de crear respuesta stream; asociar stream/run y approval al principal iniciador; cada reconexión/resume revalida sesión, ownership, estado y permiso. El browser abort/cierre no equivale a logout. Eventos no llevan credenciales ni contenido sensible de tool salvo lo requerido al dueño.
- Rate limit: auth por cuenta (no evadir alternando IP), egress IP si proxy confiable, contador persistente/transaccional para multiworker, ventana y bloqueo temporal; no bloquear permanentemente por DoS. Proteger también password change y bootstrap.

## Estrategia de migración/compatibilidad

1. Añadir schema/versionado de auth y propietarios como migración posterior, sin asumir autoría de datos históricos. Toda API privada se cierra por defecto en el mismo release; fallo/migración incompleta impide startup/readiness, no vuelve a modo abierto.
2. Para instancia existente, el primer arranque con autenticación habilitada requiere bootstrap local y confirmación del operador. Las conversaciones/notebooks previos se asignan explícitamente al único admin mediante migración transaccional, con backup pre-migración y plan rollback restaurando backup, nunca cambiando modo auth a abierto.
3. El primer usuario se provisiona desde consola/archivo secreto local y se elimina el secreto de bootstrap tras éxito. Despliegue sin bootstrap válido permanece cerrado; no se crea contraseña conocida ni endpoint “claim admin” expuesto a la red.
4. Deploy gradual no puede exponer una ventana de endpoints admin sin auth. Orden: backup offline; preparar proxy/origen HTTPS; deploy bloqueado al inicio; bootstrap por canal local; probar login y rutas; abrir ingress sólo tras checklist. Si rollback de software, conservar auth DB y mantener ingress cerrado/restringido; no restaurar app vulnerable públicamente.
5. Datos antes globales: primera versión los asigna a admin. Migración a múltiples usuarios exige decisión sobre compartir/copiar conversación/notebook y evitar fuga cross-user; no inferir compartición por ID opaco.
6. Chat y perfiles no cambian su semántica funcional inicialmente; añaden principal y ownership. Preferencias hoy globales pasan a clave por `user_id` o se asignan al admin.
7. Las aprobaciones existentes se invalidan al instalar auth o requieren asociación verificada al propietario de conversación; jamás asumir que UUID impredecible es autorización.

## Compatibilidad con SSE y AgentRuntime

- `POST /api/chat` crea/recupera conversación antes del stream. Autenticar, autorizar conversación y CSRF antes de persistir mensaje/run o iniciar provider request. SSE debe responder 401/403 HTTP antes de `StreamingResponse`; no enviar errores de auth como evento 200.
- Crear run con `initiating_user_id`/principal auditado; al reanudar aprobación, volver a validar usuario, ownership, expiración, tool fingerprint, permiso y decisión one-shot. La autorización de tool approval es distinta del booleano runtime `approved` y del sistema actual de aprobación de herramientas.
- Tool handlers reciben contexto interno con `actor_id`, run y scopes delegados limitados. No deben poder convertir una petición anónima en una acción interna válida ni heredar permiso global admin de AgentRuntime.
- Las interacciones `/api/interactions/{id}` se vinculan al run/usuario original y caducan como hoy; autenticar cada respuesta. No tomar el ID de interacción como bearer capability.
- Upload `/api/files`, subida multipart de fuentes, ingest/index y retrieve requieren sesión antes de leer cuerpos grandes o contactar proveedores; mantener límites actuales de `NEXO_MAX_UPLOAD_MB`.
- Al reinicio, sesión persistida puede permanecer válida hasta expiración/revocación. Debe decidirse el uso de key de sesión/rotación; la autoridad deriva del hash persistido. Runs pendientes requieren reautenticación al continuar, no replay de cookies guardadas en continuación.
- Móvil same-origin funciona con cookie HttpOnly y CSRF header. Para clientes externos/CLI, diseñar mecanismo separado (token scoped y revocable) más adelante; no relajar cookies ni CORS para móvil.

## Despliegue y seguridad operativa

- No publicar el puerto NEXO directamente a redes no confiables. Proxy inverso termina TLS, HSTS (sólo dominio HTTPS operativo), limita body/rate/timeout, desactiva caching de API/SSE y no loguea cuerpos/cookies/headers sensibles. HTTPS al origen también si la red host/proxy no es confiable.
- Cookie `Secure` sólo con HTTPS. No permitir opción de producción para desactivar auth/TLS. Desarrollo local HTTP debe ser perfil inequívocamente no productivo y jamás servir backup de producción.
- Proxy confiable explícito: lista de IPs/segmento exacto y configuración Uvicorn; ignorar `X-Forwarded-For`, `X-Forwarded-Proto`, `Forwarded` de clientes directos. No derivar Secure/host/origin de cabeceras no confiables. Limitar Host y origin/CSRF al dominio configurado.
- Portainer standalone no implica Docker secrets. Secretos iniciales en secreto montado/archivo legible sólo por operador, no valores por defecto, imagen o compose versionado. `nexo-data` backup cifrado y controlado; restore ensayado, credenciales de proveedor rotadas si backup se expone. No poner session secret en mismo backup si se introduce firma; el modelo opaco con hashes evita un secreto global de firma.
- Ejecutar web app como UID no-root y filesystem/data access mínimos en checkpoint de despliegue; no montar Docker socket. Esto no resuelve por sí solo aislamiento de secretos dentro del mismo proceso.
- Acceso remoto: preferir LAN filtrada/VPN al proxy; dominio externo sólo con TLS vigente, MFA o control equivalente evaluado, monitorización, rate limit, backup y runbook. MFA queda como checkpoint por decidir antes de exposición a internet.
- Health: endpoint público liveness mínimo, sin excepción cruda, endpoint o topología. Readiness de DB/provider/vector store sólo monitor privado y sin exponer al usuario anónimo.
- Respaldo/recuperación: backup cifrado, prueba periódica de restauración, recovery local por operador con verificación fuera de banda y reset de password que revoca todas las sesiones. No hay “backdoor admin” ni reset remoto no autenticado.

## Prototipo aislado

Archivos: `prototypes/admin_security.py` y `tests/test_stage15b3a_admin_security.py`. No importados por `app/`, sin FastAPI, SQLite, sockets, endpoints ni dependencia nueva.

Demuestra con datos de prueba `FAKE_*`: alta inicial constructora (no registro/login de cuenta arbitraria), contraseña con scrypt, rechazo de credenciales incorrectas, bloqueo temporal, sesión opaca en memoria, vencimiento, revocación, CSRF para mutaciones, permiso `admin:*` explícito y rechazo de permiso no otorgado. `authorize_service` falla cerrado: identidad de servicio es otra frontera y no se puede fingir con la sesión humana.

Límites intencionales: almacén de sesiones/limitador en memoria, una instancia/proceso, sin cookies HTTP, middleware, persistencia, rotación de password, owner checks, MFA, idle TTL, auditoría, proxy, concurrencia/rate limit distribuido ni identidad de servicio real. No demuestra seguridad de producción. El algoritmo de prototipo usa scrypt stdlib (`N=2^14,r=8,p=1`); benchmarking/calibración de despliegue y revisión de parámetros siguen pendientes. El objeto Python no representa una frontera de confianza.

Prueba focal: `python -m unittest tests.test_stage15b3a_admin_security -v`.

## Riesgos y bloqueos

1. No hay autorización por endpoint ni dueño de recursos; cualquier persona con reachability es admin efectivo. Bloquea exposición y Stage 15C.
2. SQLite contiene secretos y datos privados sin protección a nivel de aplicación; auth web no protege del host/root/backups.
3. Mismo proceso web ejecuta API y runtime como root; identidad interna requerirá modelo de amenaza y separación de privilegios real, no etiqueta en contexto.
4. Estado de proxy, TLS, firewall, dominio y política real de Portainer no observado; validar con operador, sin modificar despliegue aquí.
5. Proteger todos los endpoints añadidos por módulos exige guard de registro/contrato y test de superficie; el inventario actual no es policy automatizada.
6. Ownership de conversaciones/notebooks y multiusuario necesita schema, migración, reglas de sharing y auditoría, no sólo RBAC.
7. Persistencia de sesión en SQLite debe diseñarse para múltiples workers, cleanup, transacciones, fail-closed, backups y rotación.
8. Password bootstrap/recovery requiere canal local y runbook verificado; no diseñar endpoint de claim abierto ni contraseña inicial fija.
9. Los secret stores actuales (provider, MCP, AEMET) siguen plaintext; cifrado/key custody es checkpoint independiente antes de afirmar protección en reposo.
10. CSP/XSS y sanitización UI deben revisarse: XSS permite operar desde sesión aunque cookie sea HttpOnly.

## Plan de implementación por checkpoints

| Checkpoint | Entrega | Gate para continuar |
|---|---|---|
| A. Decisiones | Aprobar política de admin bootstrap, TTL, MFA remoto, trust proxy, recovery y categorías de endpoint | Operador aprueba amenaza/operación; no hay ambigüedad de quién puede acceder |
| B. Auth core aislado | Reemplazar prototipo por módulo runtime testeable: hash, sessions store persistido, rate limit, CSRF, cookie y auditoría | Pruebas unit/integration, multiworker/restart, concurrencia y fallo de DB cierran acceso |
| C. Guard + inventario ejecutable | Guard central, declaración explícita de cada ruta, módulos cubiertos, test que enumera rutas y exige política | Cero rutas privadas sin declaración/guard; health y assets son únicos públicos justificados |
| D. Ownership + migración | `user_id` para conversaciones/preferencias/notebooks/runs/approvals; asignación datos históricos a admin | Backup/restore, migración transaccional y tests IDOR/cross-user |
| E. SSE/AgentRuntime | Propagar principal/delegación; gate chat, approval, interaction, tools, uploads y resume | Tests anónimo/propietario/no propietario, CSRF, expiración y stream sin auth parcial |
| F. Admin surfaces | Aplicar permisos a providers, secrets, agents, tools/MCP, RAG, diagnostics/lab | Matriz endpoint completa, tests negativos por permiso, revisión SSRF/side effects |
| G. Operación segura | UID no-root, proxy TLS/headers/cookies, secreto bootstrap, backups/recovery, rate limits | Staging real Portainer, restore y prueba desde LAN/VPN/exterior acordado; no exposición antes del gate |
| H. Revisión para 15C | Auditoría, threat review, pentest focal y checklist de secretos | Aprobación explícita independiente; sólo entonces desbloquear Stage 15C |

## Verificación y decisión final

Resultados ejecutados en el worktree de esta etapa:

- `.venv/bin/python -m unittest tests.test_stage15b3a_admin_security -v` -> 4 tests, OK. El comando `python -m unittest ...` no pudo ejecutarse porque `python` no está en PATH; se repitió con el Python de entorno virtual soportado.
- `./scripts/nexo check` -> 262 passed, 1 skipped; frontend regressions OK. Se observaron warnings deprecados de FastAPI `on_event`, Starlette y Node module type; no fallaron pruebas.
- `git diff --check` -> OK.
- `git status --short` -> sólo `docs/stage15b-3a-administrative-security-foundation.md`, `prototypes/admin_security.py` y `tests/test_stage15b3a_admin_security.py` sin seguimiento. No hay cambios en archivos existentes.
- El stash inicial `stash@{0}` no fue alterado. No se realizó commit, push, despliegue ni uso de credenciales reales. No se tocó la base SQLite ni la configuración del runtime.

Ningún resultado local equivale a validación de producción.

**Recomendación: GO sólo para revisión arquitectónica del fundamento y prueba aislada; NO-GO para implementación en producción/exposición adicional. Stage 15C permanece bloqueada hasta cierre y aprobación de los checkpoints.**
