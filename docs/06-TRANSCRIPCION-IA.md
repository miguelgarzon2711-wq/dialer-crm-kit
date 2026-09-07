# 06 — Transcripción con IA

Este documento explica qué hace `server/scripts/transcribe_calls.py` (el cron
de transcripción), cómo adaptarlo a un cliente distinto y qué revisar cuando
algo no aparece. Está escrito a partir del código real del script, de
`server/scripts/run_transcribe.sh` y de `server/sql/schema.sql`. Donde el
código no dice algo, este documento tampoco lo inventa — se marca como
ambiguo o como "no incluido en el kit".

---

## 1. Para qué sirve

Dos cosas, con el mismo trabajo (transcribir el audio de la llamada):

1. **Nota automática en el CRM.** Después de que el vendedor dispone una
   llamada, el sistema baja la grabación, la transcribe y le pide a un modelo
   de lenguaje que redacte una nota en primera persona del agente ("hablé
   con...", "me dijo que..."). El vendedor no escribe nada — la nota queda
   sola en el contacto del CRM.
2. **Auditoría de buzón de voz.** La central telefónica cuenta un buzón de
   voz contestado como "llamada contestada". Un vendedor puede (por error o
   a propósito) disponer esa llamada como si hubiera hablado con una
   persona. El script lee la transcripción, detecta si en realidad era un
   contestador automático, y si la disposición dice lo contrario, deja una
   alerta.

El valor de negocio: mejores registros en el CRM sin depender de que el
vendedor tenga la disciplina de escribir notas, y una forma de detectar
disposiciones infladas que ninguna otra parte del sistema puede ver (la
central solo sabe si la llamada "conectó", no si conectó con una persona).

---

## 2. Cómo funciona el proceso, paso a paso

El script corre por cron (ver `run_transcribe.sh`) y en cada corrida:

### 2.1 Buscar qué disponer

`get_dispositions()` trae de la base de OMniLeads todas las calificaciones
(`ominicontacto_app_calificacioncliente`) modificadas en las **últimas 48
horas**, para las campañas `1,2,3,4,5`, con el teléfono, el nombre del
agente y el `id_externo` del contacto en el CRM (si ya se conoce).

Se descartan las disposiciones que están en `SKIP_DISPOSITIONS` (en el kit,
`"No contesto"` y `"Numero equivocado"`) — esas no aportan conversación y
las maneja otra automatización aparte del script.

### 2.2 Buscar la grabación

Por cada disposición, `find_recordings()` busca en
`reportes_app_llamadalog` llamadas del mismo contacto y campaña, con
`duracion_llamada > 30` segundos, con evento `COMPLETEAGENT` o
`COMPLETEOUTNUM` (llamada que conectó), dentro de una ventana de **90
minutos antes hasta 5 minutos después** de la hora de la disposición.

Si no aparece ninguna grabación, el script espera 3 minutos (por si la
disposición es demasiado reciente) y después publica en el CRM una nota
simple sin transcripción: `"📞 <disposición> — <agente>"`. Esto pasa, por
ejemplo, cuando la llamada fue tan corta que no llegó al mínimo de 30
segundos.

### 2.3 Descargar el audio

Las grabaciones viven en **MinIO** (almacenamiento de objetos compatible
con S3), bucket `omnileads`. `download_recording()` arma varios nombres de
archivo candidatos —el campo `archivo_grabacion` de la base si existe, o
patrones generados como `previewCall-<callid>`, `click2Call-<callid>` o
`Inbound-<callid>`— y prueba la carpeta de fecha de la disposición y un día
antes/después (por si el reloj del servidor y el de la grabación no
coinciden exactamente). El primer nombre y fecha que exista en el bucket es
el que se usa.

### 2.4 Transcribir y detectar idioma

`transcribe_audio()` manda el mp3 al endpoint de transcripciones de OpenAI
(`whisper-1`), **sin fijar el parámetro `language`** — Whisper detecta el
idioma solo. Esto importa porque un buzón de voz puede saludar en un idioma
distinto al de la conversación real (por ejemplo, mercado latino en
EE. UU. con saludos de buzón en inglés). El idioma detectado se guarda para
la fila de auditoría. El script manda además un `prompt` de contexto de
negocio para ayudar a Whisper con vocabulario específico (nombres de
producto, jerga del rubro) — en el kit trae un ejemplo armado para un
concesionario de autos en Miami que hay que reemplazar.

### 2.5 Generar la nota

`generate_crm_note()` llama al chat completion de OpenAI (`gpt-4o-mini`)
con un system prompt distinto según la llamada sea saliente
(`SYSTEM_PROMPT_OUTBOUND`) o entrante (`SYSTEM_PROMPT_INBOUND`, campaña
`campana_id == CAMPAIGN_INBOUND`). Le pasa la disposición, la duración
total y la transcripción completa (si hubo varias grabaciones para la misma
disposición, se concatenan con `\n---\n`). El prompt le pide texto corrido,
en primera persona del agente, con reglas específicas del negocio (qué
datos nunca omitir, tono, largo máximo). El prompt de inbound además le
permite al modelo responder literalmente `"skip"` cuando el audio es
ininteligible o silencio — **el prompt de outbound, tal como está en el
kit, no tiene esa instrucción explícita**, aunque el código sí revisa si la
respuesta es `"skip"` para cualquiera de los dos casos.

### 2.6 Publicar en el CRM (y guardar copia local)

`post_ghl_note()` publica la nota en el contacto del CRM vía
`POST /contacts/{id}/notes` de la API de GoHighLevel, con el texto:
`"<emoji> <disposición> — <agente>\n\n<nota>"` (📞 saliente, 📲 entrante).
Si el contacto no tenía todavía `id_externo` guardado en el dialer,
`lookup_ghl_by_phone()` lo busca por teléfono antes de publicar.

Además, `guardar_nota_local()` escribe la misma nota en la columna
`nota_crm` de `dialer_call_audit`, para que una consola o app propia del
dialer la muestre al instante sin tener que consultar al CRM.

---

## 3. Control de costo

Se paga por audio procesado en Whisper y por tokens en la nota de GPT, así
que el script está armado para **transcribir cada audio una sola vez en
toda su vida**, sin importar cuántas veces corra el cron:

- **Registro por `callid`** (el `uniqueid` de Asterisk, no el id de la
  disposición): `STATE_FILE` (`/var/log/transcripciones_procesadas.json`)
  guarda el conjunto de `callids` ya procesados con éxito. Antes de bajar o
  transcribir un audio, el script descarta los `callids` que ya estén en
  ese conjunto.
- **Caché de transcripciones**: `CACHE_FILE`
  (`/var/log/transcripts_cache.json`) guarda el texto transcrito por
  `callid`, apenas se obtiene, **antes** de intentar generar la nota o
  publicarla. Si la publicación en el CRM falla y el `callid` no llega a
  marcarse como procesado, la próxima corrida reintenta la nota y la
  publicación, pero **no vuelve a pagar Whisper** — toma el texto de la
  caché.
- **Candado contra corridas simultáneas**: `run_transcribe.sh` envuelve la
  ejecución en `flock -n /tmp/transcribe_calls.lock`. Si el cron dispara una
  corrida nueva mientras la anterior sigue viva, la nueva sale en silencio
  sin hacer nada — evita que dos procesos bajen y transcriban el mismo
  audio a la vez.
- Otros filtros que también bajan el costo: `MIN_DURATION_SECS = 30`
  (no se transcriben llamadas cortísimas), la ventana de 48 horas en la
  consulta de disposiciones, y `SKIP_DISPOSITIONS` (disposiciones sin
  conversación real nunca entran al flujo de transcripción).

Por qué importa: sin este mecanismo, cada corrida del cron podría volver a
transcribir el mismo audio si la nota falla en publicarse o si el estado se
pierde, multiplicando el gasto de API sin ningún beneficio adicional.

---

## 4. Detección de buzón de voz

`BUZON_PATTERNS` es una lista de expresiones regulares, en español e
inglés, de frases típicas de saludo de contestador automático (ej.:
`"deje su mensaje"`, `"después del tono"`, `"buzón de voz"`,
`"leave a message"`, `"after the beep"`, `"mailbox"`, `"press one"`).
`es_buzon()` busca esos patrones **solo en los primeros 600 caracteres** de
la transcripción, porque el saludo del buzón siempre está al principio de
la llamada; basta un solo patrón encontrado para marcarla como buzón.

Cuando el vendedor dispuso la llamada con una disposición que **no** está
en `SKIP_DISPOSITIONS` (es decir, la marcó como algún tipo de conversación)
pero el audio resultó ser un buzón, `registrar_audit()` marca
`alerta = true` en `dialer_call_audit` y agrega una línea a
`/var/log/voicemail_alerts.log` con fecha, agente, disposición, contacto,
campaña, `callid`, duración, idioma detectado y los primeros 120 caracteres
de la transcripción. También imprime un aviso en el log del cron
(`/var/log/transcripciones.log`, según cómo esté configurado
`run_transcribe.sh`).

Este mecanismo es **gratis en costo adicional**: reutiliza la transcripción
que ya se pagó para la nota del CRM, no hace una llamada extra a ninguna
API.

---

## 5. La tabla de auditoría

`dialer_call_audit` (definida en `server/sql/schema.sql`, y recreada de
forma idempotente por `ensure_audit_table()` dentro del script si no
existiera todavía):

| Columna | Qué guarda |
|---|---|
| `callid` (PK) | `uniqueid` de Asterisk — identifica la llamada de forma única. |
| `contacto_id` | Id del contacto en el dialer. |
| `agente` | Nombre completo del agente que dispuso la llamada. |
| `disposicion` | Nombre de la disposición elegida por el agente. |
| `campana_id` | Campaña a la que pertenece la llamada. |
| `duracion` | Duración de la llamada en segundos. |
| `es_buzon` | `true` si la IA detectó saludo de contestador automático. |
| `idioma` | Idioma detectado por Whisper (`es`, `en`, ...); puede quedar vacío si la fila viene de caché sin idioma nuevo. |
| `alerta` | `true` si el agente dispuso como conversación real algo que era un buzón. |
| `transcript_inicio` | Primeros 300 caracteres de la transcripción, para revisar sin ir a buscar el audio. |
| `fecha` | Cuándo se registró la fila (`now()` al insertar). |
| `nota_crm` | La nota redactada por la IA, para mostrarla en una consola propia sin depender del CRM. |

Sirve para tres cosas: no volver a transcribir el mismo audio (sección 3),
detectar buzones disfrazados de conversación (sección 4), y alimentar
reportes propios (el comentario del código menciona un `call_report.py`
que lee esta tabla, **no incluido en este kit**).

**Ojo al adaptar:** `ensure_audit_table()` crea la tabla sin la columna
`nota_crm` si tuviera que crearla desde cero (esa columna solo está en
`schema.sql`). Como el orden de instalación del kit exige aplicar
`schema.sql` antes de correr el script (ver `CLAUDE.md`, Paso 4), en la
práctica la tabla siempre nace completa. Si por algún motivo el script
llegara a correr primero, `guardar_nota_local()` fallaría al intentar
escribir en una columna que no existe — el error queda capturado y solo se
imprime en el log, no rompe el script, pero la nota local no se guarda.

---

## 6. Cómo adaptarlo

| Qué cambiar | Dónde |
|---|---|
| Idioma y tono de la nota | `SYSTEM_PROMPT_OUTBOUND` / `SYSTEM_PROMPT_INBOUND` |
| Contexto del negocio (rubro, qué se vende, qué datos son críticos) | Mismos dos prompts — reemplazar el ejemplo de concesionario de autos |
| Vocabulario específico para ayudar a Whisper | El `prompt` dentro de `transcribe_audio()` |
| Formato de la nota (párrafos, largo máximo, reglas críticas) | Dentro de cada `SYSTEM_PROMPT_*` |
| Qué disposiciones NO transcribir | `SKIP_DISPOSITIONS` |
| Qué campañas auditar | El `IN (1,2,3,4,5)` de `get_dispositions()` y `CAMPAIGN_INBOUND` (hoy en `-1`, es decir, sin inbound montado — hay que ponerle el id real de la campaña inbound del cliente si aplica) |
| Duración mínima para transcribir | `MIN_DURATION_SECS` |
| Cada cuánto corre | La línea de cron que invoca `run_transcribe.sh` |
| Frases de buzón si el idioma o mercado cambia | `BUZON_PATTERNS` |
| Zona horaria usada para las fechas del texto y las carpetas de MinIO | La variable `BOGOTA` (el nombre quedó así por el operador original en Bogotá; lo que importa es el valor de `ZoneInfo(...)`, no el nombre de la variable) |
| Credenciales de MinIO, OpenAI y CRM | `/root/.env_dialer` — nunca hardcodeadas en el script |

Sobre "cada cuánto corre": la línea de cron tiene que respetar la sintaxis
estándar de rangos horarios. Un rango que cruza medianoche escrito como
`12-02` **no es válido** en cron (el primer número tiene que ser menor que
el segundo); hay que partirlo en dos, por ejemplo `0-2,12-23`.

---

## 7. Costos y consideraciones

Se paga por dos llamadas a la API de OpenAI:

- **Transcripción** (`whisper-1`): se cobra por minuto de audio procesado.
- **Redacción de la nota** (`gpt-4o-mini`): se cobra por tokens de entrada
  (la transcripción completa) y de salida (la nota, tope de 200 tokens).
  Se hace **una sola llamada de nota por disposición**, aunque haya varias
  grabaciones agrupadas.

El script no trae precios hardcodeados — hay que consultar la tarifa
vigente de OpenAI para ambos modelos al momento de instalar. Para estimar
el gasto mensual: duración promedio de llamada transcribible (>30s) ×
número de llamadas que se transcriben por día (después de descontar
`SKIP_DISPOSITIONS` y las que no encuentran grabación) × 30 días, para el
costo de Whisper; más una nota de `gpt-4o-mini` por cada una de esas
llamadas para el costo de la parte de redacción (normalmente centavos por
nota, dado el tope de 200 tokens de salida).

**Si `OPENAI_API_KEY` no está configurada en `/root/.env_dialer`**, el
script imprime `"OPENAI_API_KEY no configurada — abortando"` y termina
inmediatamente (`sys.exit(0)`, sin error de cron). No se transcribe nada,
no se genera ninguna nota ni fila de auditoría — pero como este script es
un cron independiente del resto del dialer, el resto del sistema (marcado,
colas, disposiciones, escritura al CRM que no depende de IA) sigue
funcionando sin ningún problema.

---

## 8. Privacidad

El audio de cada llamada que se transcribe se envía a la API de OpenAI (un
proveedor externo, fuera de la infraestructura del dialer) para
transcripción y para la redacción de la nota. Eso implica que voces de
clientes y vendedores, y cualquier dato que se mencione en la llamada
(nombres, cifras, situación personal), salen del servidor propio hacia un
tercero.

Vale la pena avisarle al cliente esto explícitamente antes de activar el
módulo, para que decida si quiere comunicarlo a sus propios clientes o
ajustar su aviso de grabación de llamadas existente. Este script no agrega
ni quita nada al consentimiento de grabación de la llamada en sí (eso ya
debería estar resuelto en la capa de telefonía) — solo agrega un
procesamiento adicional de un audio que ya se estaba grabando.

---

## 9. Diagnóstico

Si no aparecen notas nuevas en el CRM, revisar en este orden:

1. **¿Corrió el cron?** Ver `/var/log/transcripciones.log` (o el log que
   defina el cron real) y confirmar que hay una línea con la fecha/hora
   esperada y el conteo de disposiciones encontradas.
2. **¿Quedó un lock viejo?** Si un proceso anterior murió a mitad de
   camino, `/tmp/transcribe_calls.lock` puede seguir tomado y
   `flock -n` hace que todas las corridas siguientes salgan en silencio
   sin hacer nada. Confirmar si el proceso dueño del lock sigue vivo.
3. **¿Está `OPENAI_API_KEY` en `/root/.env_dialer`?** Sin ella el script
   no hace nada (sección 7).
4. **¿Está `GHL_READ_TOKEN`?** Si el contacto no tenía `id_externo`
   guardado y no hay token de lectura, `lookup_ghl_by_phone()` nunca
   encuentra el id del CRM y esa disposición se marca como procesada sin
   publicar nada (para no reintentarla en loop). Revisar
   `GHL_TAGS_API_KEY` / `GHL_API_TOKEN` también, que son los que usa
   `post_ghl_note()` para publicar (son variables separadas del token de
   lectura).
5. **¿La disposición cae en la ventana de 48 horas y en las campañas
   filtradas?** `get_dispositions()` solo mira `campana_id IN (1,2,3,4,5)`
   y `cc.modified` de las últimas 48h.
6. **¿La disposición está en `SKIP_DISPOSITIONS`?** Esas nunca se
   transcriben por diseño.
7. **¿Se encontró grabación?** Si `find_recordings()` no encuentra nada
   (duración menor a 30s, evento distinto de `COMPLETEAGENT`/
   `COMPLETEOUTNUM`, o fuera de la ventana de 90 min antes / 5 min
   después), el resultado es una nota simple sin contenido de IA — no la
   ausencia total de nota. Si ni siquiera aparece esa nota simple, revisar
   el punto 4 (falta de `ghl_id`).
8. **¿El audio se pudo descargar de MinIO?** Si `download_recording()` no
   encuentra ninguno de los nombres candidatos en ninguna de las tres
   fechas que prueba, después de 15 minutos de edad se publica también
   solo la nota simple. Revisar que el nombre de archivo en
   `archivo_grabacion` o el patrón (`previewCall-`, `click2Call-`,
   `Inbound-`) coincida con cómo OMniLeads nombra realmente las
   grabaciones en ese servidor.
9. **¿Pasaron al menos 3 minutos desde la disposición?** El script espera
   ese margen a propósito, para dar tiempo a que la grabación termine de
   subirse a MinIO. Es normal no ver nota inmediatamente después de
   disponer.
10. **¿El modelo respondió `"skip"`?** Es un resultado válido (audio
    ininteligible o sin voz humana) — revisar el log de esa corrida, dice
    explícitamente `"GPT: skip"`.
11. **¿Se perdió el archivo de estado o de caché?** Si `STATE_FILE` o
    `CACHE_FILE` se corrompen o se borran, el script vuelve a tratar todo
    como nuevo dentro de la ventana de 48h — puede generar notas
    duplicadas en el CRM (no duplica el gasto de Whisper si la caché
    sobrevivió, pero si ambos archivos se perdieron sí se vuelve a pagar
    todo). Revisar permisos y espacio en disco de `/var/log/`.
12. **¿Existe la tabla `dialer_call_audit` con la columna `nota_crm`?**
    Ver la nota al final de la sección 5.
