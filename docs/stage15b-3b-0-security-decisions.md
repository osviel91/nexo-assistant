# NEXO Stage 15B.3B-0: Decisiones de seguridad para revisión

- Estado: propuesta arquitectónica; requiere aprobación del operador
- Base de diseño: `docs/stage15b-3a-administrative-security-foundation.md`
- Base de repositorio verificada: `f9e72ee398ca89d3985ead63d21129b544ef03c5`
- Stage 15B.3B-1 y Stage 15C: no iniciadas / bloqueadas
- Alcance: concretar decisiones para revisión, no implementar autenticación ni cambios de despliegue

## Resumen ejecutivo

Se propone un único administrador humano inicialmente, provisionado únicamente desde consola local; contraseña con Argon2id (o scrypt como alternativa stdlib tras revisión de parámetros); sesión opaca, persistida y revocable con cookie `__Host-`, TTL idle/absolute y CSRF sincronizador; permisos explícitos y deny-by-default; recuperación exclusivamente local; HTTPS same-origin terminado por un proxy operado y configurado explícitamente; acceso remoto preferentemente por VPN; migración transaccional que conserva datos existentes y asigna propiedad al administrador; y una identidad de servicio distinta con autoridad delegada y limitada.

Estas decisiones describen el comportamiento deseado. No se infiere que NEXO esté hoy detrás de proxy, use TLS, se publique sólo en LAN/VPN o comparta la topología de Compose del repositorio. Esa infraestructura debe confirmarla el operador antes de diseñar despliegue.

## Decisiones propuestas

### 1. Administrador inicial

**Recomendación:** un usuario `admin` en el MVP. Provisionamiento por comando local de administración ejecutado con acceso al host/volumen de datos, nunca por endpoint HTTP. El comando solicita username y passphrase mediante prompt sin eco (`getpass`), no acepta la passphrase en argv ni la registra en variables de entorno/log. Contraseña mínima de 12 caracteres; se recomienda passphrase larga. Guardar sólo hash Argon2id con sal individual y parámetros versionados/calibrados en el hardware soportado. scrypt de biblioteca estándar es alternativa aceptable si se evita introducir dependencia directa y los parámetros se calibran.

El bootstrap sólo funciona si no existe un administrador y auth state está en estado `uninitialized`. Se ejecuta en transacción, confirma resultado sin mostrar secretos y cambia estado irrevocablemente a `initialized`. Fallo de lectura/escritura, DB ambigua o auth state inconsistente significa fail-closed. No hay contraseña default, auto-login, registro público ni “claim admin” accesible desde la red. Si ya existe administrador, el comando de bootstrap no reemplaza credenciales; debe usarse recovery local.

Durante el bootstrap de una instalación existente, el operador debe cerrar el ingress público o mantener la instancia detenida hasta completar el provisionamiento y validar login. El bootstrap no abre el listener ni reconfigura firewall/proxy.

**Alternativas:** crear el primer usuario mediante secreto de un solo uso montado como archivo. Menos interactivo y automatizable, pero arriesga persistencia accidental en Portainer stack environment, backup o logs; no se elige para el MVP salvo que el operador requiera despliegue desatendido y aporte un secret store real.

**Confirmación del operador:** ¿hay acceso de consola/SSH al host para bootstrap? ¿Qué comando operacional puede ejecutarse con acceso al volumen sin entrar en la API? ¿Qué política de longitud de passphrase y hash soportan los hosts destino?

### 2. Sesiones

**Recomendación:** token opaco aleatorio de 256 bits; persistir hash del token, user id, `created_at`, `last_seen_at`, `idle_expires_at`, `absolute_expires_at`, `revoked_at` y metadatos mínimos. Nunca persistir token plano. Backend valida y actualiza sesión en cada petición protegida; expiración/revocación o error de store deniegan acceso. Login rota a un identificador completamente nuevo y no acepta cookies suministradas como identidad preautenticada.

Cookie del navegador: `__Host-nexo_session=<opaque>; Secure; HttpOnly; Path=/; SameSite=Lax`, sin atributo `Domain`. No guardar el bearer en localStorage, URL o contenido SSE. SameSite `Strict` queda como alternativa si las pruebas móviles/navegación confirman UX aceptable. Cookies `Secure` requieren HTTPS real desde navegador a la terminación de TLS. TTL recomendado para revisión: 30 minutos de inactividad y 12 horas absoluto; renovación deslizante sólo con actividad autenticada y sin superar absoluto. Cambio de contraseña o reset invalida todas las sesiones. Logout revoca servidor-side y elimina cookie.

CSRF: token synchronizer aleatorio ligado a cada sesión y guardado en servidor como hash. Entregarlo tras login/consulta de sesión y exigirlo en `X-CSRF-Token` para todo método mutante que use cookie. Validar también `Origin` exacto permitido; `SameSite` y CORS no sustituyen esta defensa. Token ausente, expirado o inválido rechaza antes de side effects. Login/logout/password change siguen políticas CSRF/Origin propias y explícitas.

La persistencia de sesión en SQLite permite que sobreviva reinicios normales y sirve en despliegue de un proceso/múltiples workers sobre la misma DB; escrituras y limitador deben ser atómicos. No se debe usar memoria local como store de producción. Fallo de SQLite no degrada a autenticación permisiva. Decidir limpieza periódica de sesiones expiradas sin eliminar evidencia de auditoría necesaria.

**Alternativas:** invalidar todas las sesiones en cada reinicio simplifica recovery, pero perjudica operación y no sustituye la revocación persistente; no recomendada. JWT stateless evita una consulta de sesión, pero complica revocación inmediata, cambio de permisos y exposición del token; no recomendada para un administrador único.

**Confirmación del operador:** aprobar TTL idle de 30 min / absolute 12 h, cookie Lax y persistencia a través de restart; confirmar dominio/origen HTTPS de navegador. Si se espera uso exclusivamente local HTTP, no se debe bajar `Secure` en producción: se debe resolver TLS o mantener acceso por un cliente local aislado sólo durante bootstrap.

### 3. Autorización

**Recomendación:** autorización en backend, deny-by-default. Cada ruta registrada, incluyendo rutas de módulos opcionales, declara una de cuatro políticas: `public`, `user:<permission>`, `admin:<permission>` o `service:<scope>`. Ruta sin política bloquea startup o falla el test de contrato; no existe modo de producción que desactive el guard. Autorización ocurre antes de leer/subir cuerpos grandes, consultar datos, disparar provider calls, abrir SSE o hacer cualquier efecto lateral.

MVP usa rol `admin` al que se asignan permisos de forma explícita, no un bypass mágico por ausencia de usuarios. Taxonomía inicial sugerida: `config:read/write`; `providers:read/write/test`; `secrets:metadata/write/rotate`; `agents:read/write/run`; `tools:read/write/approve`; `mcp:read/write/connect`; `rag:read/read_content/write/query/diagnostics`; `chat:read/write/delete`; `diagnostics:read/run`; `system:admin`. `system:admin` no se concede a procesos internos. Los nombres se materializan en una matriz método/ruta revisada junto al endpoint registry.

Las rutas de conversación, notebooks, approvals, interactions, runs y artifacts también exigen comprobación de ownership/ACL en la misma operación que consulta el recurso; UUID impredecible no equivale a autorización. En MVP todos los datos históricos se asignan explícitamente al único admin; esta asignación no debe convertirse en supuesto para futura multiidentidad.

Rutas públicas permitidas se limitan a assets estáticos y liveness mínimo (`status` sin dependencias, configuración, errores ni versión sensible). Readiness operacional queda en una red de monitorización privada. Cualquier nueva ruta añadida por módulos se protege igual que rutas core.

**Alternativas:** dos roles fijos (`admin`/`viewer`) son más simples, pero no dan el control necesario para aprobación de herramientas, secretos y laboratorio; puede ser fase posterior. Para el MVP conservar permisos nombrados aunque sólo exista un role binding evita que el código asuma que todas las operaciones son equivalentes.

**Confirmación del operador:** aprobar el catálogo y si la UI de laboratorio debe ser sólo admin. La decisión no autoriza endpoints actualmente públicos ni es instrucción para modificar runtime en esta etapa.

### 4. Recuperación administrativa

**Recomendación:** recovery sólo con control local del host y del volumen. Comando operacional separado del bootstrap (por ejemplo, `nexo admin reset-password`) verifica acceso a la instalación, solicita nueva passphrase sin eco, actualiza hash de forma atómica, revoca todas las sesiones y registra actor local/fecha/resultado sin guardar password. El comando no lee ni modifica proveedores, conversaciones o notebooks. Para no añadir puerta trasera, no existe password universal, usuario oculto, endpoint HTTP de reset, token estático de emergencia ni recuperación por email.

Antes del reset, el operador detiene o restringe el ingress, comprueba instancia/ruta de datos, conserva backup consistente bajo su política y confirma explícitamente el reset. Tras completar, se valida login, se reabre el ingress según política y se revisa auditoría. Si falta/corrompe el auth store, no crear admin automáticamente: mantener privadas las APIs y recuperar desde backup o ejecutar procedimiento local de reparación documentado y auditado. Perder acceso root/host implica que el recovery no está disponible; quien controla el host/Portainer ya está fuera de la frontera que la app puede proteger.

**Alternativas:** recrear la DB completa para reset es inaceptable porque destruye conversaciones y configuración. Desactivar auth temporalmente tampoco es opción. Recuperación multioperador con dos personas puede añadirse si la operación exige separación de deberes.

**Confirmación del operador:** identificar quién puede ejecutar comandos locales en el host/Portainer, qué evidencia de auditoría debe preservarse y dónde reside el backup que permite recovery sin incluir plaintext de credenciales.

### 5. HTTPS y proxy, Docker/Portainer y móvil

**Recomendación:** mismo origen HTTPS en navegador, con proxy inverso de confianza administrado por el operador terminando TLS y encaminando al contenedor por una red Docker privada. No publicar el puerto del contenedor al host si el proxy puede comunicarse por red interna; si el host proxy necesita acceso, enlazar sólo a loopback/ACL local cuando sea compatible con la instalación. La UI y API comparten origen para que cookies HttpOnly y CSRF funcionen también en móvil sin CORS amplio.

Proxy permite sólo Host configurado, elimina/reescribe `Forwarded` y `X-Forwarded-*` recibidos del cliente y los vuelve a insertar. Uvicorn confía únicamente en la IP/CIDR exacta del proxy, nunca en `*`; NEXO ignora cabeceras forwarded de conexiones directas. Validar TLS/certificados en el proxy, renovación automática, redirección HTTP->HTTPS sin procesar credenciales en HTTP, HSTS sólo después de confirmar dominio y TLS estables, no cache de `/api/*` o SSE, límites de body, timeout de stream adecuado y logs que no incluyan cookie, CSRF, cuerpos ni Authorization. No confiar en `X-Forwarded-Proto` para fijar cookie Secure si esa cabecera es controlable por cliente.

Para móvil, usar el mismo FQDN/origen y cookie same-origin; probar Safari/Chrome móvil, bloqueo de third-party cookies no es relevante en same-origin. No exponer API por CORS wildcard ni usar localStorage para sortear cookies. SSE debe preservar respuesta stream y buffering desactivado en el proxy.

Docker Compose actual y archivo Portainer son ejemplos del repo, no prueba de la topología productiva. Portainer standalone no implica Docker Secrets ni proxy específico. No se debe asumir Traefik, Nginx, Caddy, Swarm, cert resolver, red, dominio o protección de firewall. El proxy existente, si lo hay, y el modo Portainer se deben inspeccionar/confirmar antes de configurar.

**Alternativas:** TLS dentro del propio contenedor elimina proxy, pero requiere operación de certificados y no coincide con la topología actual documentada; viable sólo si el operador no dispone de reverse proxy. Proxy con TLS al origen añade protección ante segmentos internos no confiables; decidir según aislamiento de red.

**Confirmación del operador:** proxy/producto/version; dónde termina TLS; red y puerto actual; dominio/certificado; IP/CIDR proxy; Portainer standalone o Swarm; si NEXO debe seguir accesible directamente en puerto host; si el cliente móvil usa VPN, DNS privado o dominio público.

### 6. Acceso remoto

**Recomendación:** perfil inicial restringido a loopback durante instalación y luego LAN controlada o VPN de confianza, además de autenticación de aplicación. Firewall permite sólo redes necesarias; no interpretar LAN como identidad. Para móvil remoto, preferir VPN y el mismo origen HTTPS. Deshabilitar/filtrar exposición directa del puerto 8787 al WAN.

Exposición a Internet sólo como decisión explícita posterior a checkpoint operativo: TLS y dominio mantenidos; MFA (TOTP/WebAuthn o identidad upstream evaluada, sin quitar auth NEXO), rate limiting por cuenta/origen, protección contra fuerza bruta, actualización/monitorización, alertas de login y cambios sensibles, backup cifrado con restore ensayado, proxy hardening, CSP/XSS auditado, respuesta a incidentes y revisión de superficie completa. MFA no es requisito técnico de esta propuesta MVP de LAN/VPN, pero sí gate recomendado antes de internet abierto. No asumir que una IP privada, DDNS o URL secreta es control de acceso.

**Alternativas:** acceso público con sólo password es posible técnicamente pero no se recomienda para esta aplicación que conserva credenciales de herramientas/proveedores y permite efectos laterales. Túnel privado gestionado puede sustituir VPN si el operador confirma identidad, ACL, logging y límites equivalentes.

**Confirmación del operador:** alcance objetivo LAN/VPN/Internet; subredes permitidas; si se necesita uso externo móvil; proveedor VPN/túnel; decisión sobre MFA obligatorio para ese alcance.

### 7. Migración y preservación de datos

**Recomendación:** migración aditiva y transaccional; backup consistente verificado antes. No borrar, regenerar ni recrear `nexo-data`, Qdrant, providers, models, AgentProfiles, conversaciones, mensajes, approvals, notebooks, sources, canonical documents, vectores, citas ni traces. Añadir luego tablas/columnas para usuarios, sesiones, propietario y estado de bootstrap mediante migración versionada separada y probada sobre copia.

Durante bootstrap de única identidad, asignar todos los datos históricos compartidos al `admin` explícitamente, con reporte de conteos y sin exponer texto ni secretos. Preferencias globales actuales pasan a preferencias del admin, preservando valores. Agentes, configuración de proveedor y notebooks se conservan como recursos administrados del sistema en el MVP; multiusuario futuro requerirá política de ACL/compartición, no convertirlos implícitamente en privados o globales. Aprobaciones/interacciones/run pendientes se invalidan o cancelan al activar auth, salvo que asociación y permiso original puedan verificarse; preferible invalidar pendiente sin ejecutar acciones y marcar motivo no sensible. No reinterpretar aprobación previa como permiso del admin autenticado.

Auth habilitada y guard deben llegar en el mismo release: sin estado inicial, base incompatible o migración parcial, startup falla cerrado y no arranca endpoints privados. No hay release de transición que mantenga rutas admin abiertas. Rollback de software conserva DB migrada y mantiene el ingress cerrado/restringido; nunca reabrir código pre-auth en red. Ensayar backup/restore sobre copia aislada y rollback operativo antes de staging. Qdrant es índice derivado según la arquitectura del repo, pero no borrar/reindexar automáticamente como parte de auth.

**Alternativas:** importar datos históricos de forma selectiva requiere que operador indique qué se conserva; es más complejo y arriesga pérdida. Asignar todo al admin es la opción mínima dado el modelo inicial de usuario único. Reset de DB o borrar todas las conversaciones es rechazado.

**Confirmación del operador:** aceptar que el administrador inicial heredará propiedad/acceso a todo el histórico; decidir retención y cancelación de approvals/runs abiertos; nombrar procedimiento y ubicación de backup/restore probado; confirmar que existe ventana de mantenimiento con ingress restringido.

### 8. Identidad de servicio y AgentRuntime

**Recomendación:** distinguir tipos de principal en el contrato: `user` (ID, permisos) y `service` (ID de workload, scopes y delegación/correlation al usuario que originó la tarea). La autenticación humana no es una credencial de servicio; nombres de proceso, UUID de run o ejecución dentro de FastAPI tampoco prueban por sí solos una identidad.

En toda petición de agente se autentica al usuario y se autoriza `agents:run`/acceso a conversación, perfil, notebook, provider y herramientas. El runtime recibe contexto delegado inmutable y limitado al snapshot aprobado: actor, conversation/run, scopes operacionales mínimos, tools allowlist y límites. No recibe `system:admin`. Cada tool approval verifica sesión actual, dueño y permiso `tools:approve`, snapshot/fingerprint y estado one-shot antes de ejecutar. Resume/interactions revalidan usuario/ownership; un ID de approval/interacción no funciona como bearer token. Llamadas a proveedores son egress interno con provider seleccionado por configuración autorizada, no endpoints browser-expuestos de servicio.

Mientras API y AgentRuntime compartan proceso, el contexto de servicio es una frontera lógica para diseño y auditoría, no aislamiento contra compromiso de Python/root. No emitir tokens internos autoafirmados ni añadir socket/credencial pretendiendo que separa al mismo proceso. Si una futura separación de procesos se justifica, definir credencial workload de corta duración, audience/service allowlist, canal privado y scopes estrechos; jamás montar Docker socket o reutilizar password admin.

**Alternativas:** permisos humanos heredados completos permitirían al agente actuar como admin; se rechaza. Ejecución sin identidad del usuario impediría auditoría/ownership; se rechaza. Split en microservicios ahora aumenta superficie y no es requisito del primer checkpoint.

**Confirmación del operador:** revisar si tools mutativas/destructivas requieren reautenticación siempre o sólo aprobación por ejecución; decidir si cualquier herramienta debe quedar deshabilitada hasta configuración explícita; aceptar que un proceso web comprometido puede usar secretos actualmente cargados en memoria.

## Decisiones recomendadas frente a alternativas

| Área | Recomendada | Alternativa evaluable | No recomendado / rechazado |
|---|---|---|---|
| Bootstrap | consola local + prompt sin eco, sólo primer admin | secret file de un uso con custodia real | contraseña default, signup o claim HTTP |
| Password | Argon2id directo/pinned, parámetros calibrados | scrypt stdlib calibrado | plaintext, reversible, hash rápido sin KDF |
| Session | token opaco persistido por hash, TTL idle+absolute | invalidación global controlada si hay razón operacional | JWT stateless como única revocación |
| CSRF | synchronizer + Origin exacto + SameSite | double-submit sólo si amenaza/rotación están documentadas | confiar sólo en CORS o SameSite |
| AuthZ | permisos por endpoint + ownership + deny-by-default | admin/viewer como roles iniciales mapeados a permisos | proteger sólo UI o prefijos escogidos |
| Recovery | comando local, revocar todas, auditar | flujo de dos operadores en operación regulada | endpoint anónimo, backdoor o borrar DB |
| HTTPS | proxy administrado, same-origin, egress limitado | TLS servido directamente por NEXO | HTTP WAN o forwarded headers confiados globalmente |
| Remoto | VPN/LAN controlada; MFA antes de Internet | túnel privado con ACL revisada | Internet sólo con password |
| Histórico | conservar y asignar admin en migración transaccional | migración selectiva instruida por operador | reset/drop/recrear datos |
| Servicio | principal de servicio con scopes/delegación, sin admin | proceso separado si threat model lo requiere | runtime como admin implícito |

## Cuestiones que requieren aprobación del operador

1. ¿El objetivo de acceso será LAN privada, VPN, túnel privado o Internet? ¿Qué redes se deben permitir y cuál será el requisito MFA?
2. ¿Qué proxy termina TLS en la instalación real? Confirmar producto, origen, FQDN/certificado, red Docker, trusted proxy IPs/CIDR y si el puerto NEXO está hoy publicado directamente.
3. ¿Portainer es standalone o Swarm y qué método real de provisión de secretos soporta? Confirmar acceso a consola/SSH del host para bootstrap y recovery.
4. ¿Aprobar Argon2id con dependencia directa/pinned o preferir scrypt stdlib tras calibración? ¿Passphrase mínima de 12 caracteres y política adicional?
5. ¿Aprobar TTL de sesión: 30 min idle / 12 h absolute, cookie `SameSite=Lax` y mantener sesión a través de restart?
6. ¿Aprobar catálogo de permisos y que rutas de laboratorio/diagnóstico sólo sean administrativas?
7. ¿Aprobar que todos los datos existentes se asignen al primer admin y qué hacer con approvals, interacciones y runs pendientes durante la activación?
8. ¿Quién está autorizado a restablecer contraseña en host y qué auditoría/backup necesita el procedimiento?
9. ¿Aceptar que el proceso único/root de hoy no separa realmente el compromiso del runtime de las credenciales? ¿Se requiere no-root/proceso separado como gate antes de habilitar herramientas mutativas?
10. ¿Qué herramienta MCP/native puede ejecutar acciones destructivas y qué nivel de aprobación/reautenticación exige el operador?

No se deben resolver estas preguntas por conjetura basada en Compose o README. El operador responde con hechos de su infraestructura; los documentos del repositorio son sólo configuración de referencia.

## Siguiente checkpoint y parada

Al aprobar estas decisiones, el próximo alcance sería preparar una especificación/checklist de implementación aislada para Stage 15B.3B-1, incluyendo matriz endpoint-permission versionada, bootstrap/recovery, sesión/CSRF y escenarios de migración. **Este documento no inicia ese checkpoint.** No se modifica runtime, SQLite, frontend, providers, AgentRuntime ni archivos Compose. Stage 15C continúa bloqueada hasta revisión explícita y gates posteriores.
