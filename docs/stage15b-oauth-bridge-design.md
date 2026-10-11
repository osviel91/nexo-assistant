# Stage 15B: Secure Local OAuth Bridge Prototype

- Estado: prototipo aislado; detenido para revisión arquitectónica y de seguridad
- Fecha: 2026-10-11
- Base verificada: `74513730e0712f6925f0106052bd6a50e78e967c`
- Alcance: flujo SIWC local y transferencia protegida a un NEXO self-hosted remoto
- No incluye: OAuth real, tokens reales, proveedor ChatGPT, Responses API, rutas/runtime de NEXO, SQLite, migraciones ni despliegue

## Decisión

**GO condicional para investigar una transferencia local-a-VM; NO-GO para integrar o activar el puente en NEXO.** La documentación SIWC permite que una app open-source complete OAuth localmente y transfiera credenciales protegidas al VM por un canal seguro, por ejemplo SSH. No documenta ni certifica este protocolo de emparejamiento HTTPS. El prototipo prueba únicamente algunas invariantes de una posible transferencia; no demuestra una implementación de transporte segura ni convierte la instancia actual de NEXO en un destino autorizado.

No debe iniciarse Stage 15C hasta revisar y aprobar explícitamente el diseño, resolver autenticación/autorización de NEXO y definir un canal de transferencia real y operable. La modalidad web-only con callback OAuth al host remoto sigue siendo NO-GO: el callback loopback `127.0.0.1` vuelve al equipo que ejecuta el navegador.

## Fuentes oficiales consultadas

Consultadas directamente el 2026-10-11:

- [Sign in with ChatGPT: overview](https://developers.openai.com/siwc/token-sharing-open-source/)
- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)

Requisitos confirmados:

- El flujo directo open-source usa un public client; no requiere client secret ni partner API key. Apps pagadas o alojadas remotamente deben consultar el formulario de interés y confirmar elegibilidad antes de distribuir la función. La modalidad self-hosted debe seguir siendo realmente operada por el usuario.
- Primer registro: `client_id=dynamic_agent_client`, nombre real y estable de la app (`agent_name_hint`) y `ext_agent_host_id` opaco, estable y único por host. El callback entrega el `client_id` emitido, que debe guardarse y reutilizarse para ese registro; no guardar `dynamic_agent_client` como ID emitido.
- Scopes: identidad `openid profile email`; uso de plan `offline_access resource.invoke chatgpt.tokens.use.direct`. Comprobar scopes concedidos en la respuesta del token; un ID token válido no basta para inferir autorización de inferencia.
- OAuth Authorization Code con PKCE `S256`, `state` aleatorio por intento, nonce OIDC, callback HTTP loopback en `127.0.0.1` y coincidencia exacta de `redirect_uri` en intercambio. `localhost` no sustituye a `127.0.0.1`.
- Intercambio en `https://auth.openai.com/api/accounts/oauth/token`; validar state, firma JWKS, issuer, audience contra el client ID emitido, expiración, nonce y `sub` antes de aceptar/activar una identidad o reemplazar credenciales.
- Renovación con el client ID emitido, refresh token y resource; serializar renovaciones por sesión porque el refresh token puede rotar. Guardar el conjunto actualizado de forma coherente. Revocación mediante el endpoint de revocación publicado en discovery; si no se confirma, informar y remitir a ChatGPT Settings.
- Para VMs self-hosted, la guía prescribe OAuth local, transferencia del archivo protegido por un canal seguro como SSH, preservación del host ID del VM y renovación posterior en el VM. La guía no prescribe un endpoint HTTP de pairing propio. En sesiones transferidas no están disponibles atribución de uso ni revocación de acceso al plan específicas del host.

La referencia de registro y las guías de VM respaldan la transferencia protegida de una sesión; no se interpreta esto como aprobación de OpenAI para este código experimental o para una instancia compartida sin control de acceso.

## Controles de acceso actuales

La inspección de `app/main.py` y sus rutas no encontró middleware ni dependencia general de autenticación/autorización. La aplicación sirve API y UI en una instancia compartida y Compose puede publicarla en red; el usuario que alcanza la instancia no se puede identificar como principal autorizado. La configuración de TLS depende del proxy/operador. Una clave de pairing no corrige esta carencia: antes de un producto real, iniciar y confirmar pairing, activar/desactivar conexión, usar/chat y gestionar credenciales deben requerir un principal autenticado con autorización definida.

**Precondición de producto:** autenticación/autorización explícita, al menos un principal operador autenticado y una política documentada de quién puede conectar y consumir la cuenta. No asumir que estar en la LAN, conocer la URL o poseer acceso al browser equivale a una identidad segura.

## Diseño propuesto para revisión

1. El helper local es el cliente SIWC: ejecuta el flujo oficial en el Mac, con callback loopback, state/nonce/PKCE y validación de ID token/scopes. Nunca usa ni lee `~/.codex/auth.json`, Codex CLI o app-server.
2. El host remoto crea previamente su `ext_agent_host_id` estable y abre una sesión de pairing temporal vinculada al principal NEXO autenticado. El secreto de pairing es aleatorio, de alta entropía, de un uso, con expiración corta; se muestra fuera de URL y logs. Sólo se conserva su verificador.
3. El helper se configura con un origen remoto explícito HTTPS. No sigue redirecciones, no acepta esquemas/hosts arbitrarios, valida certificado/hostname TLS, y no envía credenciales en URL, query, browser storage ni diagnósticos. La validación de hostname debe ser exacta y el origen no debe tomarse de un callback no confiable.
4. El helper cifra y autentica el transporte usando TLS normal con verificación completa. La posesión del código autoriza sólo la carga a esa sesión; no identifica por sí misma al helper ni al usuario. Limitar intentos y tamaño, invalidar el código tras una carga válida y no aceptar una segunda carga.
5. El servidor mantiene el bundle exclusivamente en cuarentena temporal. El principal que inició debe volver a confirmar en una sesión autenticada antes de que cualquier componente lo considere conectable. La confirmación consume una sola vez la carga y la asociación queda vinculada a ese principal. El pairing no debe habilitar automáticamente un proveedor.
6. Ante expiración, reinicio, caída de red o fallo de confirmación, descartar la carga parcial y exigir un pairing nuevo. No persistir datos OAuth en SQLite durante la fase de prototipo.
7. En una implementación real, el bundle de credenciales debe terminar en almacenamiento protegido del host, con permisos mínimos y cifrado autenticado cuya clave viva fuera de SQLite/backups. El prototipo no implementa almacenamiento ni cifrado en reposo.

### Amenazas y límites

| Amenaza | Mitigación propuesta | Límite restante |
| --- | --- | --- |
| Robo/adivinación del código | 256 bits aleatorios, expiración, hash/verificador en servidor, un intento de transferencia aceptado y consumo único | Quien roba un código vigente puede enviar primero una carga y causar denegación de servicio; la confirmación explícita impide activación silenciosa. Rate limiting real pendiente. |
| Replay | Estado de transferencia y confirmación de un solo uso; borrar sesión al consumir/expirar | Requiere almacenamiento transaccional compartido si NEXO corre con varios workers/replicas. |
| Confusión de usuario | Vincular sesión al principal autenticado y exigir confirmación por ese mismo principal | No es implementable correctamente con la instancia actual, que no tiene authn/authz. |
| MITM o servidor falso | HTTPS obligatorio, CA del sistema y verificación de hostname; origen allowlisted por configuración local | El prototipo no abre conexiones TLS; esta propiedad no fue probada. Certificados privados/proxy requieren decisión operacional, no `verify=False`. |
| SSRF/destino cambiado | Origen fijado y validado antes de transmitir; nunca seguir redirects ni aceptar URLs del callback | Resolución DNS/rebinding y egress policy deben probarse en el helper real. |
| Filtración en navegador/URL/logs | El token SIWC permanece en memoria del helper y el cuerpo va por canal TLS; código no es token OAuth; redacción por diseño | El prototipo no implementa HTTP, UI, logger ni captura de tráfico. No constituye evidencia de ausencia en esos canales. |
| Reboot/interrupción | Sesión efímera; fallar cerrado y reiniciar pairing | Un pairing en curso se pierde; no hay recuperación/reanudación parcial. |
| Refresh/revocación | Renovar en el VM, serializar refresh rotatorio y borrar localmente al desconectar | La atribución/revocación host-specific de sesiones transferidas no está disponible según la guía. |
| Compromiso del código | La carga queda pendiente hasta confirmación autorizada; código expirado y consumido deja de servir | Código comprometido puede causar DoS. Si también se compromete la sesión autenticada o el equipo del operador, pairing no lo contiene. |

## Prototipo

Código: `prototypes/oauth_bridge.py`, fuera de `app/` y del runtime.

Implementa una máquina de estados in-memory para el contrato propuesto:

- `begin` exige un principal, genera identificador/código aleatorios y conserva sólo SHA-256 del código.
- `transfer` exige origen HTTPS configurado, código válido y vigente, y rechaza segunda carga.
- `confirm` exige el mismo principal, carga previa y sesión vigente; entrega la carga ficticia una sola vez.
- `diagnostics` sólo devuelve un contador. El módulo no registra valores ni guarda nada en SQLite.

La prueba usa únicamente valores ficticios `FAKE_ACCESS`/`FAKE_REFRESH`. No realiza OAuth, no contacta OpenAI, no importa credenciales, no usa HTTP/TLS y no habilita proveedor. En consecuencia, la asociación de principal, protección TLS, autenticación del endpoint, comportamiento de proxy, redacción de logs y cifrado en reposo son contratos/precondiciones, no resultados demostrados por el prototipo. No distribuir ni ejecutar este código como servicio.

## Mac + Docker/Portainer

- **Mac autenticador:** ejecutar helper nativo local, callback sólo en loopback, abrir navegador del Mac; validar respuesta y mantener bundle en memoria hasta completar transferencia. No escribir tokens en historial de shell, archivos temporales, portapapeles persistente o logs.
- **NEXO remoto:** acceso únicamente por HTTPS terminado en proxy correctamente configurado. El proxy no debe loguear cuerpos ni query sensibles; limitar tamaño, frecuencia y duración. No publicar endpoint HTTP directo del contenedor. La app debe autenticar al principal independientemente del TLS del proxy.
- **Reinicio:** host ID debe sobrevivir en configuración persistente; pairing/carga pendiente se invalida y se reinicia. La futura conexión requiere secreto de cifrado externo a SQLite y recuperación definida; sin clave se falla cerrado, nunca se degrada a texto plano.
- **Pérdida de conexión:** no reintentar ciegamente una transferencia/confirmación después de consumo incierto. Consultar un estado sin secretos o iniciar pairing nuevo. Tokens OAuth nunca se pasan por URL.
- **Renovación/revocación:** deben vivir en el host remoto tras confirmación; el prototipo no las ejecuta. Reautorización usa client ID emitido, host ID remoto estable, state/nonce/PKCE nuevos y validación de identidad antes de reemplazo. Refresh concurrente se serializa y credenciales rotatorias se actualizan como unidad. Ante revocación o fallo de renovación, desactivar y pedir reautorización; no reintentar grant inválido con el mismo refresh token.
- **Código comprometido:** retirar/caducar la sesión y crear otra. Si hubo transferencia, exigir revisar/cancelar la sesión; ante tokens reales comprometidos, desconectar la app desde ChatGPT Settings. La confirmación reduce activación no autorizada, no evita DoS.

## Resultado y recomendación

- La ruta local OAuth + transferencia a VM está justificada por la guía oficial para apps OSS/self-hosted; callback remoto web-only no lo está.
- La guía sí nombra un canal seguro como SSH, pero no documenta el protocolo de pairing HTTPS propuesto. Requiere revisión y, si se pretende distribuir fuera del uso OSS/self-hosted, confirmación de elegibilidad con OpenAI.
- NEXO no identifica actualmente al usuario que autorizaría el consumo. No desplegar el puente ni transferir tokens reales a esta versión.
- El prototipo sólo verifica lógica de estado en proceso único. No verifica transporte cifrado/autenticado, persistencia distribuida, autenticación de NEXO, custodia de clave, logs de servidor, ni renovación/revocación.
- No almacenar tokens OAuth reales en SQLite; no cambiar proveedores existentes; no integrar proveedor ChatGPT.
- Recomendación: aceptar sólo como experimento de diseño. Stage 15C queda bloqueado hasta revisión arquitectónica/seguridad, autenticación/autorización, canal y custodia de secretos aprobados, y pruebas de transporte real con credenciales sintéticas.

## Verificación

Resultados al 2026-10-11:

- Pruebas específicas: `python3 -m unittest tests.test_stage15b_oauth_bridge` -> 6 tests, OK.
- Quality gate: primer `./scripts/nexo check` -> 247 passed, 1 fallo intermitente en `tests/test_tool_selection.py::ToolSelectionTests::test_catalog_scaling_benchmark` (p95 131 ms con catálogo de 500); segundo intento -> 248 passed y regresiones frontend OK. No se modificó el test ni el código de selección.
- `git diff --check` -> OK; `git status --short` muestra sólo los tres archivos nuevos de esta etapa.
- Worktree inicial limpio; archivos de esta etapa: 3 (`prototypes/oauth_bridge.py`, `tests/test_stage15b_oauth_bridge.py`, `docs/stage15b-oauth-bridge-design.md`). Sin commit, push ni despliegue.

Archivos modificados en esta etapa: 3 (prototipo, prueba y este documento).

No se hizo commit, push, despliegue, ni cambio funcional fuera del prototipo OAuth y este documento.
