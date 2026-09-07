# Instrucciones para el agente que instala este kit

> Si sos un asistente de IA (Claude Code o similar) y te pidieron montar este dialer:
> **leé este archivo entero antes de escribir un solo comando.** Está escrito para vos.
> Cada regla de acá salió de romper algo en producción y tener que arreglarlo.

---

## 1. Qué es esto y qué NO es

Es un **kit de integración**, no un producto instalable de un click.

Asume que ya existe (o vas a instalar) un **OMniLeads** funcionando en un servidor
propio. OMniLeads es un contact center de código abierto sobre Asterisk + Django +
PostgreSQL + Redis, corriendo en contenedores Docker. Este kit le agrega encima:

| Lo que agrega | Para qué |
|---|---|
| Integración con un CRM (probado con GoHighLevel) | los leads entran solos y los resultados vuelven solos |
| Prioridades de marcación | llamar primero al que más probabilidad tiene de contestar |
| Dueño permanente del lead ("sticky") | que dos vendedores no se peleen el mismo cliente |
| Rotación de caller ID | que los números no se quemen como "Spam Likely" |
| Transcripción con IA | notas automáticas en el CRM y auditoría de buzones |
| API REST para clientes móviles | que un vendedor sin computador pueda trabajar |
| Persistencia de parches | que nada se pierda al reiniciar los contenedores |

**No incluye:** OMniLeads en sí, la app móvil, ni ninguna credencial. Todo lo que
diga `<ALGO_ASI>` lo tenés que reemplazar con datos del cliente.

---

## 2. Reglas duras — romper una de estas cuesta horas

Estas no son sugerencias. Cada una corresponde a un incidente real.

### 2.1 Nunca `docker compose up`
OMniLeads levanta contenedores con prefijos de hash. Un `docker compose up` los
mata y los recrea, y perdés todo lo que hay dentro. **Usá siempre operaciones
individuales:** `docker exec`, `docker cp`, `docker restart <nombre>`, `docker start/stop`.

### 2.2 Todo lo que edites dentro de un contenedor, guardalo afuera también
Los contenedores se recrean. Si parcheás un archivo con `docker exec` y no dejás
copia en el host, el próximo reinicio lo borra y el sistema vuelve a fallar sin
que nadie entienda por qué. **Flujo correcto:** editás la copia local en
`/opt/dialer-kit/patches/`, la copiás al contenedor con `docker cp`, y agregás el
bloque correspondiente a `restore_patches.sh`.

### 2.3 Validá la sintaxis ANTES de copiar al contenedor
Un archivo Python con un error de sintaxis tumba Django entero y el dialer queda
muerto hasta que alguien lo note.
```bash
python3 -c "import ast; ast.parse(open('archivo.py').read())" && echo OK
bash -n script.sh && echo OK
```
Hacelo siempre. Sin excepción.

### 2.4 En PostgreSQL, un error de SQL aborta la transacción entera
Aunque captures la excepción en Python. Si dentro de una petición hacés una
consulta que falla (por ejemplo a una columna que no existe), **todo lo que venga
después en esa misma petición falla también**, incluida la entrega del lead al
vendedor. El síntoma es desconcertante: "el lead se pierde".

La defensa es envolver cada consulta opcional en su propio savepoint:
```python
from django.db import transaction, connection
try:
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SELECT ...")
except Exception:
    pass   # esta consulta falló, pero la petición sigue viva
```

### 2.5 La central solo marca dígitos
Un teléfono con `+`, espacios o guiones **hace morir la llamada en silencio**: no
hay error, no hay log, simplemente no pasa nada y el vendedor cree que el número
no existe. Normalizá a 10 dígitos (o el largo que use el país) antes de marcar,
en TODOS los caminos: al inyectar el lead, al marcar desde la consola, al marcar
desde la API, y con un cron de red de seguridad.

### 2.6 Antes de cambiar algo del dialer en producción, avisá y esperá el OK
Contenedores, Django, Asterisk y base de datos son sistemas vivos con gente
trabajando encima. Explicá qué vas a tocar y qué puede pasar. Es preferible
preguntar de más.

### 2.7 Nunca toques el ruteo de nginx
Los encabezados de caché están bien. Las reglas de proxy y ruteo no: un cambio
ahí deja el webphone sin señal (error 502 en `/ws`) y nadie puede llamar.

### 2.8 Probá con una llamada REAL
Que la configuración "se vea bien" no significa nada. Un trunk mal nombrado, un
contexto de dialplan faltante o un prefijo mal puesto solo aparecen cuando marcás
de verdad y escuchás. Hacé la llamada, escuchá el audio de ida y de vuelta, colgá
y verificá que el registro quedó guardado.

---

## 3. Orden de instalación

No te saltes pasos ni cambies el orden. Cada uno depende del anterior.

### Paso 0 — Entender el negocio antes de tocar código
No arranques hasta poder responder esto. Preguntale al cliente:

1. ¿De dónde vienen los leads? (formulario, WhatsApp, anuncios, base vieja)
2. ¿Cuántos por día? ¿Cuántos vendedores?
3. ¿Cuál es el resultado que busca una llamada? (agendar cita, vender, calificar)
4. ¿Los vendedores compiten por los leads o cada uno tiene los suyos?
5. **¿Un lead que ya habló con un vendedor debe quedarse con ese vendedor?**
   (casi siempre sí, y define toda la lógica de "sticky")
6. ¿Qué resultados posibles tiene una llamada? Esa lista son las disposiciones.
7. ¿En qué horario se llama? ¿Qué zona horaria?
8. ¿Cuántas veces se intenta un lead antes de rendirse?

Escribí las respuestas en un archivo del proyecto. Vas a volver a ellas todo el tiempo.

### Paso 1 — Servidor y OMniLeads
- Servidor propio (no compartido). Referencia de tamaño: 4 vCPU / 8 GB para ~20
  agentes simultáneos. Ojo con la zona horaria del sistema.
- Instalá OMniLeads siguiendo su documentación oficial.
- Verificá que entrás a la consola web y que un agente puede loguearse.
- **Todavía no toques nada de este kit.**

### Paso 2 — Cerrar el servidor (hacelo AHORA, no al final)
Un dialer recién instalado con puertos abiertos es un blanco. En un caso real,
atacantes registraron softphones haciéndose pasar por agentes en menos de un día.
```bash
bash server/scripts/firewall.sh          # cierra puertos internos al exterior
bash server/scripts/purge_sip_intruders.sh   # borra registros que no vengan del proxy
```
Leé los dos scripts antes de correrlos y adaptá el nombre de la interfaz de red.
Dejá el firewall como servicio de systemd para que sobreviva a los reinicios.

### Paso 3 — Telefonía
Contratá el proveedor SIP, comprá los números y configurá el trunk.
Leé `docs/05-TELEFONIA.md` completo: ahí están los errores clásicos que hacen
perder un día entero (el nombre del endpoint del trunk, el formato E.164, y el
contexto de ruta saliente que hay que crear a mano porque el generador nativo
de OMniLeads no lo arma bien).

**Terminá este paso con una llamada real que suene y se escuche en los dos sentidos.**

### Paso 4 — Base de datos
```bash
docker exec -i prod-env-postgresql-1 psql -U omnileads -d omnileads < server/sql/schema.sql
```

### Paso 5 — Credenciales
```bash
cp .env.example /root/.env_dialer
chmod 600 /root/.env_dialer      # importante
```
Completá cada variable. Nunca las escribas dentro del código ni las subas a git.

### Paso 6 — Integración con el CRM
Copiá los módulos de `server/django/` al contenedor de Django, agregá las rutas y
reiniciá. Leé `docs/03-INTEGRACION-CRM.md` para la estructura exacta de los
webhooks y probá con un lead de mentira antes de conectar el flujo real.

### Paso 7 — Automatizaciones
Instalá los crons de `server/scripts/`. La tabla completa está en
`docs/07-OPERACION.md`. Empezá por los de seguridad y persistencia.

### Paso 8 — Persistencia
Adaptá `server/scripts/restore_patches.sh` a tu instalación y agregalo al arranque:
```
@reboot sleep 90 && bash /opt/dialer-kit/scripts/restore_patches.sh
```
**Probalo de verdad:** reiniciá el servidor y verificá que el dialer vuelve solo.
Un kit que no sobrevive un reinicio no está terminado.

### Paso 9 — Prueba de fuego antes de entregar
- Llamada saliente real, con audio en los dos sentidos.
- Llamada entrante que llega al vendedor correcto.
- Un lead entra por el webhook y aparece en la cola.
- El vendedor lo toma, lo llama, lo dispone, y el resultado aparece en el CRM.
- Reiniciar el servidor y que todo siga funcionando.
- Dos vendedores tomando leads a la vez sin pisarse.

---

## 4. Cómo adaptarlo a un cliente distinto

### 4.1 Lo que SIEMPRE hay que cambiar
| Qué | Dónde |
|---|---|
| Credenciales del CRM, proveedor SIP, IA | `/root/.env_dialer` |
| Tipos de lead y prioridades | `TIPO_ORDEN` en `crm_webhooks.py` |
| Disposiciones y cuáles cuentan como "contestó" | `CONTESTO` / `NO_CONTESTO` en `crm_dispositions.py` |
| Tags que se escriben en el CRM | `crm_dispositions.py` |
| Números y tope diario por número | `did_picker.py` |
| Colas y campañas | `server/asterisk/queues.conf.example` |
| Idioma y contexto de la transcripción | `transcribe_calls.py` |
| Zona horaria | sistema y `settings` de Django |

### 4.2 Lo que probablemente sirve tal cual
El sticky de dueño, la normalización de teléfonos, el firewall, la persistencia
de parches, la liberación de leads abandonados y la estructura de la API.

### 4.3 Si el CRM no es GoHighLevel
El diseño ya separa las capas. Reescribí solo las funciones que hablan HTTP con
el CRM (`_try(...)`, las llamadas a `requests` en `crm_dispositions.py` y
`crm_webhooks.py`). La lógica de prioridades, sticky y disposiciones no depende
del CRM y se queda igual. Está detallado al final de `docs/03-INTEGRACION-CRM.md`.

---

## 5. Decisiones de diseño que conviene respetar

Podés cambiarlas, pero entendé primero por qué están así.

**El servidor decide, el cliente solo pinta.** Ni la consola web ni la app móvil
deciden nada: piden y muestran. Así el comportamiento es idéntico en todos los
dispositivos y una regla se cambia en un solo lugar.

**El número del lead nunca sale del servidor.** El cliente recibe `***-***-1234`.
El vendedor marca por id de contacto, no por número. Es lo que impide que alguien
se lleve la base de datos en el celular.

**Un lead que conversó tiene dueño para siempre.** Basta una sola conversación real
para que ese lead sea de ese vendedor: rediscados, callbacks y entrantes vuelven
siempre a la misma persona. Sin esto se pelean las comisiones. Que después no
conteste no le quita el dueño.

**Sin calificar la última llamada no hay llamada nueva.** Si no, aparecen llamadas
huérfanas sin resultado y las métricas quedan inservibles.

**Sesión única por vendedor.** El último dispositivo que entra saca al anterior.
Dos sesiones del mismo agente compiten por el mismo teléfono SIP y ninguna funciona bien.

**Con un lead en pantalla no entran llamadas.** Si no, al vendedor le entra una
llamada mientras está por marcar y pierde el lead que tenía.

**Todo cron debe auto-repararse en los dos sentidos.** No asumas que una orden
previa llegó. Compará el estado real contra el deseado y corregí en ambas
direcciones: así el sistema se recupera solo de un reinicio de cualquier pieza.

---

## 6. Errores que ya cometimos — no los repitas

| Síntoma | Causa real |
|---|---|
| La llamada muere en silencio, sin error | el número llevaba `+` o espacios |
| El lead entregado "se pierde" | un error SQL abortó la transacción entera |
| Suena un timbre y el audio se corta | en móvil, se contestó antes de que el sistema activara el audio |
| El webphone dice "SIP Proxy no responde" | certificados TLS del proxy SIP mal configurados |
| Un parche desaparece solo | se editó dentro del contenedor sin copia en el host |
| Fechas con horas raras | se guardó en UTC y se mostró sin convertir a la zona del cliente |
| Llamadas salientes que nadie hizo | puerto SIP abierto: alguien se registró como agente |
| Las campañas se cierran solas | OMniLeads auto-finaliza campañas Preview al agotar contactos |
| Dos vendedores llamando al mismo lead | falta el sticky de dueño |
| El agente no recibe leads y nadie sabe por qué | quedó pausado por una pausa que nunca se levantó |

---

## 7. Problemas conocidos del propio kit

Antes de instalar, leé [`docs/09-PROBLEMAS-CONOCIDOS.md`](docs/09-PROBLEMAS-CONOCIDOS.md).
Son cosas que sabemos que están imperfectas y que conviene decidir antes, no
descubrir en producción. Ninguna impide que el sistema funcione.

Las tres que más te van a afectar:
- Los nombres de contenedor están fijos en los scripts: verificá los tuyos con
  `docker ps --format '{{.Names}}'` y reemplazalos antes de instalar.
- La lista de disposiciones que cuentan como "contestó" está duplicada en dos
  archivos. Si editás una, editá la otra.
- Los números de línea de los parches no van a coincidir con tu versión de
  OMniLeads: buscá por nombre de función, nunca por número de línea.

---

## 8. Antes de decir "listo"

- [ ] Llamada real saliente con audio en los dos sentidos.
- [ ] Llamada real entrante al vendedor correcto.
- [ ] Un lead recorre el ciclo completo: CRM → dialer → llamada → disposición → CRM.
- [ ] Reinicio del servidor: todo vuelve solo.
- [ ] `grep -rn` buscando credenciales en el código: sin resultados.
- [ ] Puertos internos cerrados (verificalo desde afuera, no desde el servidor).
- [ ] Los crons corren y escriben en sus logs.
- [ ] El cliente sabe qué mirar cuando algo falla.

Si alguno no está, no está listo. Decilo en vez de entregarlo a medias.
