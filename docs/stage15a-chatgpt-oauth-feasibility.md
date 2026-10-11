# Stage 15A: ChatGPT OAuth Provider Feasibility

- Estado: evaluación; no autoriza implementación
- Fecha de consulta: 2026-10-11
- Alcance: Sign in with ChatGPT (SIWC), uso del plan ChatGPT y Responses API
- Fuera de alcance: Codex app-server, credenciales internas de Codex CLI y cambios funcionales

## Decisión

La integración es **viable con restricciones** para una aplicación open source/local usando el flujo oficial SIWC de ChatGPT plan usage y la Responses API. No es una variación de la API key ni un proveedor Chat Completions. Los tokens OAuth autorizan inferencia elegible en nombre de la cuenta; no garantizan todos los modelos ni capacidades y no dan acceso a conversaciones/contexto de ChatGPT.

Para la topología habitual de Nexo (contenedor en ZimaOS/Portainer y navegador en otro dispositivo), un inicio OAuth web-only directamente contra el contenedor **no es viable**: el callback requerido a `127.0.0.1` llega al equipo que ejecuta el navegador. OpenAI documenta para VM remota terminar OAuth localmente y transferir el registro protegido al VM por un canal seguro. Nexo no tiene actualmente autenticación de usuario que permita recibir esa transferencia de forma segura. Stage 15B debe quedar bloqueado hasta elegir y diseñar un cliente local/helper y un canal autenticado apropiado, o hasta adoptar otra modalidad oficialmente soportada. No se debe reemplazar el callback por una URL pública, usar `localhost`, ni extraer tokens de Codex.

El uso de SIWC está documentado para apps open source y alojadas localmente. Para ofrecerlo en una app de pago o alojada remotamente, OpenAI indica solicitar acceso mediante su formulario de interés. Revisar elegibilidad y términos antes de distribuir la función; el carácter self-hosted del despliegue por sí solo no sustituye esa revisión.

## Fuentes oficiales

Consultadas el 2026-10-11:

- [SIWC para apps open source](https://developers.openai.com/siwc/token-sharing-open-source/)
- [Registro y sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Modelos e inferencia](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Limitaciones de preview](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [VMs self-hosted](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)
- [Cuentas, sesiones y refresh](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)

Los endpoints y alcances descritos abajo son específicos de este flujo oficial SIWC. No son una recomendación para usar endpoints internos de ChatGPT.

## Arquitectura actual

| Área | Implementación actual | Consecuencia |
| --- | --- | --- |
| Provider CRUD | `ProviderIn` en `app/main.py`; `providers` contiene `name`, `base_url`, `api_key`; `models` es provider-scoped (`app/migrations.py`) | No existe tipo de auth, protocolo, cuenta OAuth, vencimiento ni identidad de cuenta. `ProviderIn.api_key` también permite el formulario manual. |
| Descubrimiento | `POST /api/providers/{pid}/refresh` llama `base_url + /models`, espera `data[].id` y normaliza capacidades | El catálogo SIWC es `GET https://api.openai.com/v1/models`, con `models[].slug`, `display_name` y `visibility`. Necesita descubrimiento y persistencia específicos; no cabe en el parser existente. |
| Chat | `/api/chat` fija `base_url + /chat/completions`, construye Authorization Bearer desde `providers.api_key` y crea `OpenAICompatibleModelAdapter` (`app/main.py`) | URL, credencial y protocolo están mezclados en la ruta; Chat Completions no puede representar el contrato SIWC Responses. |
| Adaptador | `ModelAdapter.stream(messages, tools, temperature)` y `ModelStreamChunk` en `app/agent_model.py` | El seam devuelve texto, reasoning, fragmentos de tool calls, finish reason, usage y TTFT; es una buena frontera para otro adaptador, pero los mensajes/herramientas internos todavía son Chat Completions-shaped. |
| Ejecución | `EffectiveRunConfiguration` y `AgentRuntime` en `app/agent.py` congelan instrucciones, grounding, herramientas y límites por ejecución | Preservar este snapshot y el runtime. No acoplar políticas, RAG ni ejecución de herramientas al protocolo de red del proveedor. |
| Herramientas | `EffectiveToolSet.definitions()` en `app/tools.py` serializa funciones Chat Completions. `AgentRuntime` acumula fragmentos por índice y añade mensajes `assistant.tool_calls` y `role=tool` | Responses tiene items/eventos/ciclo de function call distintos. La compatibilidad requiere traducir ida y vuelta o abstraer el turno; no basta con reserializar el objeto `tools`. |
| SSE | El adaptador analiza `data:` y `choices[0].delta`; `/api/chat` traduce eventos del agente a SSE JSON propio; `web/assets/app.js` consume ese contrato | El parser Responses debe consumir eventos nativos internamente y emitir el contrato de Nexo. No exponer eventos OpenAI directamente al navegador. |
| Adjuntos | `app/modules/attachments.py` devuelve imágenes como data URL y extrae PDF/DOCX/texto como texto. `/api/chat` convierte imágenes a `image_url` de Chat Completions | El adaptador Responses necesita mapear texto e imágenes al esquema input correspondiente. La extracción de archivos puede continuar como texto; Files API no está soportada por esta ruta SIWC. |
| Otros consumidores provider | Embeddings, reranking y otras configuraciones leen `base_url`/`api_key` directamente (`app/main.py`, `app/embeddings.py`, `app/reranking.py`) | Un provider OAuth no debe aparecer automáticamente como válido para embedding/reranking. Separar disponibilidad del provider, protocolo y capacidad por operación/modelo. |
| Secretos | API keys y credenciales MCP/AEMET se guardan en texto claro en SQLite; serialización de provider sólo devuelve `has_api_key` (`app/main.py`, `app/migrations.py`) | Existe una frontera de no-retorno al navegador, pero no cifrado en reposo. OAuth aumentaría el impacto de una copia de DB porque incluye refresh token de larga duración. |
| Acceso | Las rutas API revisadas no muestran authn/authz de usuario; app publica Uvicorn en `0.0.0.0:8787` | Quien pueda alcanzar la app puede, en principio, invocar chat o cambios administrativos. La conexión compartida no puede considerarse protegida por ocultar el token. |
| Despliegue | Docker/Portainer monta `nexo-data:/data`; no hay listener/helper local ni transferencia de credenciales | El volumen permite persistencia tras reinicios, pero no resuelve callback remoto, cifrado, control de acceso ni importación segura. |

### Dependencias que asumen Chat Completions

- URL `/chat/completions`, payload `{model, messages, stream}`, `temperature`, `thinking` y `tools/tool_choice` en `app/main.py` y `app/agent_model.py`.
- Parser SSE `choices[].delta`, `finish_reason`, `usage` y `[DONE]` en `OpenAICompatibleModelAdapter`.
- Tool calls fragmentadas como `delta.tool_calls[index]`; continuación del agente como assistant `tool_calls` y respuesta `role=tool` en `app/agent.py`.
- `system` messages para perfiles, grounding y tool guidance. SIWC Responses requiere `instructions` o developer messages; mensajes `system` explícitos son rechazados.
- Historial se recompone desde mensajes conversacionales y no usa `previous_response_id`; esto es compatible con la regla SIWC HTTP de enviar el contexto completo, si se convierte al esquema `input`.
- Imágenes se serializan como `image_url`; PDF/DOCX ya llegan como texto extraído. El formato de input de Responses debe adaptarse.
- La telemetría actual reconoce `prompt_tokens`/`completion_tokens`; Responses entrega `input_tokens`/`output_tokens` en el evento terminal. Normalizar métricas sin fingir valores ausentes.

Las referencias se encuentran principalmente en `app/main.py` (ProviderIn, rutas provider y `/api/chat`), `app/agent_model.py`, `app/agent.py`, `app/tools.py`, `app/modules/attachments.py`, `app/embeddings.py`, `app/reranking.py` y `app/migrations.py`.

## SIWC oficial y login remoto

### Requisitos de registro

- Para primer registro, iniciar con `client_id=dynamic_agent_client`, `agent_name_hint` estable con el nombre real de la app y `ext_agent_host_id` opaco y persistente para el host.
- El callback nuevo retorna el `client_id` emitido para esa cuenta/workspace. Guardar ese ID; `dynamic_agent_client` sólo inicia registro y no se usa para el intercambio posterior.
- Es un public client: el flujo OSS descrito no requiere client secret ni partner API key.
- Solicitar scopes de identidad `openid profile email` y los de plan `offline_access resource.invoke chatgpt.tokens.use.direct`. La concesión debe comprobarse en el token response; sin `chatgpt.tokens.use.direct` no iniciar inferencia de plan.
- Usar `https://auth.openai.com/api/accounts/authorize`, Authorization Code + PKCE S256, `state`, OIDC `nonce`, y recurso `https://api.openai.com/v1`.
- Usar callback loopback HTTP en `127.0.0.1`, con esquema, host y path requeridos; el URI exacto (incluyendo puerto elegido) debe repetirse en intercambio. La documentación dice que `localhost` no es sustituto.
- Validar `state`, firma del ID token contra JWKS, issuer, audience igual al client ID emitido, expiración, nonce, subject e identidad seleccionada. No activar/reemplazar cuenta antes de validar.
- Intercambiar código en `https://auth.openai.com/api/accounts/oauth/token` con form-urlencoded, client ID emitido, code, code_verifier, redirect_uri y resource.
- Reusar el issued client ID por registro de cuenta y el mismo host ID para el mismo host. Identidades/cuentas deben mantenerse separadas, aunque compartan email.

### Topología Docker/Portainer

La guía oficial [Self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms) confirma que el callback loopback llega al equipo del navegador, no al VM. Documenta este procedimiento:

1. Crear y persistir el host ID del VM.
2. Completar OAuth localmente con el mismo tool/client, usuario y workspace que se usarán en el VM.
3. Transferir el archivo protegido de credenciales al VM por canal seguro, por ejemplo SSH, preservando el host ID del VM.
4. Dejar que el VM realice renovaciones posteriores.

No existe en la guía consultada un callback OAuth remoto para el servidor de Nexo ni una transferencia web integrada predefinida. Por tanto:

- **Flujo web-only desde el Nexo remoto:** no soportado bajo la instrucción de callback loopback; detenerse aquí, no construir callback público ni proxy OAuth alternativo.
- **Ruta oficial de VM:** viable en principio con OAuth en un cliente local y transferencia segura de las credenciales protegidas. Para evitar comandos manuales dentro del contenedor se necesitaría una herramienta/helper local de Nexo y un canal de importación autenticado al servidor. Eso es diseño pendiente, no un endpoint de callback implícito ni una capacidad que el producto actual ya tenga.
- **Bloqueo de seguridad actual:** Nexo no ofrece authn/authz general. No aceptar un archivo/token mediante endpoint accesible sin autenticación. En un servidor compartido también se necesita identificar qué usuario puede activar, usar, renovar y desconectar la cuenta.
- En esta modalidad transferida, OpenAI indica que atribución de uso específica por host y revocación de acceso al plan para sesiones transferidas todavía no están disponibles. Hay que comunicar esta limitación al usuario y ofrecer desconexión desde la configuración de ChatGPT.

La clave de cifrado debe ser administrada fuera de SQLite. Esto exige una decisión operativa sobre provisión/backup del secreto del despliegue Portainer, recuperación tras reinicios y comportamiento si falta la clave. El volumen persistente por sí solo no es cifrado ni gestión de secretos.

## Contrato propuesto del proveedor

No es un esquema final de base de datos. Es el límite lógico recomendado para Stage 15B:

```text
ProviderConnection
  id, display_name
  provider_kind: "openai_chatgpt_plan"
  auth_kind: "siwc_oauth"
  protocol: "responses"
  account_label                 # datos de UI, p. ej. email validado si el usuario lo permite
  issued_client_id              # nunca dynamic_agent_client tras completar registro
  stable_host_id
  granted_scopes
  status: connected | needs_reauth | disconnected
  models[]: { slug, display_name, visibility, discovered_at }

OAuthCredential (server-side only)
  provider_connection_id
  encrypted access_token, refresh_token, id_token
  token_expires_at, saved_at
  validated issuer, subject
```

Contrato operativo:

- `GET /v1/models` con bearer token; mostrar sólo los elementos con `visibility == "list"`, usando `display_name` y enviando `slug` como model. El catálogo es específico de cuenta y no sustituye una comprobación de entitlement por inferencia.
- `POST /v1/responses` con bearer access token, `stream=true`, `store=false`, `model` de catálogo y `input` explícito con contexto de Nexo. Esperar `response.completed` para considerar éxito.
- El adaptador no acepta base URL configurable para esta conexión: usar endpoint público documentado. No aceptar URLs de autorización/callback entrantes del usuario.
- Credenciales sólo se resuelven en backend. API de administración devuelve metadata, modelos y estado, nunca tokens/client secrets ni cabeceras.
- No reutilizar el CRUD genérico para enviar un token en `api_key`; separar la conexión SIWC del secreto de providers API-key.
- Errores de scope no concedido, modelo no autorizado, límite de uso, revocación, expiración, `response.failed`, `response.incomplete` y stream sin terminal deben mapearse a estados/errores seguros y diferenciados. No registrar body/headers que puedan contener tokens o contenido privado.

## Adaptador Responses en AgentRuntime

Crear un `ResponsesModelAdapter` distinto del `OpenAICompatibleModelAdapter`, ambos cumpliendo inicialmente el contrato `ModelAdapter`. La ruta seleccionaría explícitamente el adaptador por tipo/protocolo de conexión; no cambiar la selección existente para providers URL/API key. El parser nativo Responses queda dentro del adaptador y normaliza a `ModelStreamChunk` y eventos del agente. El SSE público de Nexo permanece intacto.

La separación de protocolo y capacidad requiere dos fuentes distintas:

- **Protocolo/operación:** Chat Completions versus Responses; ruta, esquema de entrada, autenticación y parser.
- **Capacidad de modelo:** tool calling, imágenes y reasoning sólo cuando catálogo/contrato/modelo y prueba de ejecución lo confirmen. La pertenencia a Responses o el nombre del modelo no infieren capacidades.

`EffectiveRunConfiguration` debe continuar siendo el snapshot de agente, KnowledgeOutcome, grounding, herramientas, parámetros y límites. El adaptador Responses convierte ese snapshot al wire format sin mover RAG, política o ejecución al provider. `ToolSelector`/`ExposurePolicy`, `AgentRuntime`, `ProgressGuard`, `ToolResultPipeline`, approvals y límites de llamadas continúan decidiendo qué se ejecuta y procesando resultados localmente.

El seam de stream ya abstrae delta de texto, reasoning, tool calls, usage y TTFT, pero la continuación de tools está acoplada al formato Chat Completions en `AgentRuntime`. La implementación debe demostrar que los function-call items de Responses se pueden reducir a llamadas normalizadas conservando call ID, nombre y argumentos completos, y enviar los resultados de vuelta en el input de la ronda siguiente. No debilitar policy ni tratar una approval-required como ejecución aprobada. Si no se puede mapear sin alterar esas garantías, tools se desactivan para esta integración y se informa claramente; texto básico puede implementarse primero.

### Capacidades verificadas

“Verificada” aquí significa que la guía SIWC revisada documenta la condición; no significa que Nexo ya la implemente ni que cada cuenta/modelo la soporte.

| Capacidad | Estado según SIWC | Tratamiento para Nexo |
| --- | --- | --- |
| Autorización ChatGPT plan | Documentada con `chatgpt.tokens.use.direct` concedido | Comprobar scope otorgado, no inferirlo del ID token. |
| Catálogo de modelos | Documentado en `/v1/models`, filtro `visibility=list`, campos `slug`/`display_name` | Implementar catálogo distinto del `/models` compatible actual; inferencia confirma el acceso real. |
| Text input y salida streaming | Documentados en Responses; `stream=true`, terminar en `response.completed` | Adaptable al stream de Nexo con parser separado. |
| Persistencia del proveedor | `store=false` requerido; HTTP no usa `previous_response_id`, enviar contexto completo | Mantener historial reconstruido por Nexo e incluirlo en cada solicitud. |
| Imágenes/archivos | Texto, imagen y archivos soportados si el modelo los acepta; Files API no soportada | Mapping de imagen requiere prueba. Preferir texto ya extraído para PDF/DOCX. No afirmar soporte universal. |
| Function/custom tools | La guía preview las describe con restricciones (namespaces o `additional_tools`) y ejecución client-side | **No verificado en el contrato Nexo**. Requiere experimento/documentación puntual del wire format y prueba de rondas/seguridad. No exponer hasta entonces. |
| Web search | Sujeto a política de cuenta/workspace y modelo; no garantizado | No inferir elegibilidad por OAuth ni asumir que la herramienta local `web_search` equivale a una herramienta hosted. |
| Reasoning/thinking | El endpoint puede emitir datos de reasoning, pero la guía de preview no garantiza su acceso/display para cada modelo/cuenta | No activar desde `reasoning-content` heredado; tratar como capacidad no verificada hasta confirmar forma autorizada y política de exposición. |
| `temperature` y otros parámetros | `temperature` y varios campos explícitamente no soportados en preview | Omitirlos para SIWC aunque `EffectiveRunConfiguration` los contenga; no reescribir preferencias ni comportamiento de otros proveedores. |
| Embedding, reranking, audio/video, hosted tools | No autorizados/documentados para este flujo; Audio/video, Files API, transcription y varios hosted tools están excluidos | No ofrecer estas operaciones mediante la conexión SIWC. Configuración RAG continúa usando providers compatibles existentes. |
| Uso/cuota | Límites de plan pueden fallar como `response.failed`; uso Plus comparte ventana con otras apps | Surfacing seguro de límite y enlace a uso ChatGPT; nunca prometer cuota independiente. |

Las listas de modelos y capabilities pueden cambiar. El producto debe mostrar disponibilidad observada y manejar rechazo real sin afirmar compatibilidad futura.

## Credenciales y seguridad

### Amenazas observadas

- SQLite contiene secrets sin cifrar; un backup o acceso al volumen expone todos los API keys almacenados actualmente y potencialmente expondría refresh tokens de SIWC.
- La API no presenta authn/authz general en las rutas examinadas. Un atacante de red podría iniciar chat a cuenta conectada, cambiar provider config, enumerar historial o desconectar credenciales.
- TLS depende del proxy/operador cuando Nexo se expone por red; el Compose local publica un puerto HTTP directo. No hay secure-channel handshake con un cliente local.
- Logs/traces registran provider/model/endpoint y error codes. El diseño no debe pasar nunca access/refresh/id token, PKCE verifier, Authorization header, OAuth code, `id_token_hint` completo o body sensible a funciones de diagnostic/logging.

### Diseño mínimo exigido antes de usar tokens

- Definir acceso autenticado a Nexo o una frontera equivalente antes de habilitar una conexión que autorice consumo del plan. Limitar connect, activate, chat, refresh y disconnect al principal autorizado.
- Cifrar tokens en reposo con cifrado autenticado, clave fuera de SQLite y fuera de backups de DB. Fuente de clave externa al volumen de datos (secret de despliegue); documentar rotación, backup y recuperación. No guardar claves de cifrado junto a ciphertext en la misma DB/volumen.
- Mantener tokens cifrados incluso durante el flujo normal de app; descifrar sólo en memoria lo necesario para una petición. No devolverlos ni reflejarlos en traces/errores.
- Persistir access/refresh/id tokens, scopes y expiración como un conjunto coherente. Escribir renovación rotatoria atómicamente; serializar refresh por cuenta para no competir con tokens rotatorios. No reintentar ciegamente un refresh rechazado.
- Mantener `state`, nonce y PKCE verifier temporalmente del lado del proceso que ejecuta autorización; state de un solo uso, vencimiento corto y comparación constante. Borrar pendiente en éxito/error/timeout. En modalidad VM, proteger también el canal y la importación local; nunca enviar bundle en URL ni mostrar tokens al browser.
- Refresh form-urlencoded contra el token endpoint, con issued client ID, refresh token, resource, scope omitido. Actualizar conjuntamente access token, expiry, scopes y refresh token rotado. Tratar revocación y errores de renovación según SIWC.
- Desconexión: intentar revocar el refresh token vía revocation endpoint del discovery document; dejar de usar tokens y borrarlos localmente. Si no se confirma revocación por fallo de red/5xx, informar que revocación remota no fue confirmada y enlazar los controles de ChatGPT.
- Al faltar la key de cifrado tras un restart, fallar cerrado como `needs_reauth`/credencial inaccesible; nunca caer a plaintext. Preservar host ID en almacenamiento persistente. Considerar que la guía advierte que sesiones transferidas no tienen todavía revocación/atribución host-specific.

La guía SIWC requiere almacenamiento protegido, no prescribe cifrado SQLite específico. Cifrar tokens es una condición de seguridad de Nexo derivada del modelo de amenaza, no una afirmación de que OpenAI provea/gestione la clave.

## Riesgos de compatibilidad

1. **Protocolo distinto:** Responses no acepta la serialización Chat Completions. Cambiar sólo URL/Authorization rompe formato de mensajes, tool calls, finish y uso.
2. **Conversión del runtime:** `AgentRuntime` construye `system` messages y muta historial con `tool_calls`/`role=tool`. Requiere traducción completa por request o extracción futura de un historial/turno neutral. Riesgo alto de romper Profile, grounding, approvals y reanudación.
3. **Parámetros incompatibles:** SIWC preview rechaza `temperature` y otros campos. Omitirlos sólo en esta conexión puede producir respuestas distintas respecto de configuración visible; hacer el límite visible y no alterar providers actuales.
4. **Funciones/herramientas:** formas y eventos de Responses requieren prueba. IDs parciales, call completion, argumentos inválidos, límites de rondas y reenvío de outputs deben conservar las garantías internas. Sin eso, no habilitar tools.
5. **Reasoning:** datos/capacidades heredadas de proveedores no demuestran que el scope/plan/modelo SIWC los autorice o que Nexo deba mostrarlos.
6. **Catálogo versus entitlement:** modelo listado no garantiza que la cuenta pueda usarlo cuando se invoque. Contemplar 403, límites y cambios de cuenta.
7. **Modelo compartido y acceso a la instancia:** la cuota de plan puede compartirse con otras apps; toda persona con acceso al Nexo expuesto podría consumirla si no existe autorización.
8. **Callback y Portainer:** browser y proceso remoto tienen loopback distintos; reboot, volúmenes, proxy TLS, host ID y transferencia local deben estar en la definición de producto antes de código.
9. **RAG no se migra a OAuth:** embedding y reranker conservan proveedores/API keys propios. La generación con Responses puede recibir el grounding como contexto, sujeto a límites de input/modelo, pero no habilita el stack de embeddings con el token SIWC.
10. **Cambios de preview/eligibilidad:** endpoint, scopes y límites deben revalidarse antes de Stage 15B y antes de publicar; no convertir el preview en promesa contractual permanente.

## Plan incremental

1. **Resolver bloqueo de despliegue:** aceptar explícitamente modalidad VM oficial con un helper local de Nexo y transferencia autenticada/protegida, junto con identidad/autorización del servidor; o declarar la función fuera de alcance para el despliegue remoto. No codificar OAuth web-only remoto.
2. **Cerrar threat model y operación:** definir usuario autorizado, TLS, canal de importación, key encryption externa, recovery/backup, revocación, errores y UX del estado quota. Confirmar elegibilidad/licencia.
3. **Contrato/model catalog:** separar provider kind, protocolo, operación y capabilities; probar catálogo SIWC account-specific y manejo de `visibility`, slug y display name. La ausencia de DB change en 15A no anticipa una migración concreta.
4. **Auth lifecycle:** cliente local oficial con state/nonce/PKCE, registro dinámico, validación OIDC/scopes, transferencia protegida, persistencia cifrada, refresh rotatorio y desconexión. Pruebas negativas antes de inferencia.
5. **Responses texto:** adaptador independiente, `store=false`, streaming obligatorio, contexto completo, conversion de instrucciones a `instructions`/developer input, terminales success/failure/incomplete, usage y errores. Mantener SSE de Nexo.
6. **Capacidades incrementales:** validar imagen; después, function tools sólo con formato documentado/observado y pruebas que cubran ToolSelector, límites, ToolResultPipeline, ProgressGuard, approval-required, reanudación y telemetría. No habilitar reasoning no verificado.
7. **Regresión y rollout:** providers existentes sin cambios de URL/payload; RAG/embeddings y reranking permanecen como están; validar reinicios y key loss en Docker/Portainer. Mantener SIWC opt-in y ofrecer desconectar.

## Criterios de aceptación para Stage 15B

- [ ] Decisión aprobada para despliegue remoto: helper/cliente local y transferencia segura oficial definida, o función descartada en topología VM; no callback alternativo.
- [ ] Elegibilidad, términos, scopes, endpoints y preview revisados contra documentación SIWC vigente en fecha de implementación.
- [ ] No dependencia ni lectura de `~/.codex/auth.json`, Codex CLI ni app-server.
- [ ] Nexo tiene control de acceso suficiente antes de habilitar la conexión; connect/use/refresh/disconnect no quedan disponibles anónimamente en red compartida.
- [ ] Token y metadata sensible cifrados con clave externa a SQLite; restart con volumen persistente conserva conexión y restart sin key falla cerrado.
- [ ] OAuth prueba state replay/mismatch, nonce/audience/issuer/firma inválidos, denied scope, callback erróneo, refresh rotatorio/concurrente, revocación y cuenta equivocada.
- [ ] Ninguna respuesta HTTP, browser storage, SSE, log, trace, diagnostic o error incluye tokens, OAuth code, PKCE verifier o Authorization header.
- [ ] Catálogo usa endpoint/shape SIWC; el modelo se presenta como disponible sólo según catálogo de cuenta y un request real aún puede fallar por entitlement/cuota.
- [ ] Responses adapter independiente usa `store=false` y `stream=true`, envía historial completo y sólo reporta éxito tras `response.completed`; errores/incomplete/interrupción son distinguibles.
- [ ] Capacidades no soportadas/no verificadas se omiten; sin herramientas si el mapping no mantiene límites, policy, approvals y tool results actuales.
- [ ] `AgentProfile`, `EffectiveRunConfiguration`, AgentRuntime, RAG/KnowledgeOutcome, ToolSelector, ProgressGuard, ToolResultPipeline y SSE de Nexo mantienen sus invariantes.
- [ ] Pruebas confirman que los requests y comportamiento Chat Completions de oMLX y otros providers no cambian.
- [ ] Docker/Portainer documenta provisión/backup de encryption key y helper/importación; no requiere comandos manuales dentro del contenedor.
- [ ] Quality gate `./scripts/nexo check` pasa y `git diff --check` no reporta errores.

Stage 15A se detiene aquí para revisión arquitectónica. No se modificó código ni esquema de base de datos.
