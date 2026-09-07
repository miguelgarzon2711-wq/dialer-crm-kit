# Comportamiento del dialer

Este documento es el **contrato**: qué hace el sistema en cada situación y por qué.
Si vas a cambiar una regla, leé primero el "por qué" — casi todas nacieron de un
problema real de operación, no de una preferencia técnica.

Regla que atraviesa todo el diseño: **el servidor decide, el cliente solo pinta.**
Ni la consola web ni la app móvil deciden nada. Piden datos y muestran botones. Así
el comportamiento es idéntico en todos los dispositivos y una regla se cambia en un
solo lugar.

---

## 1. Estados del agente

| Estado | Qué significa | ¿Le entran llamadas? |
|---|---|---|
| Desconectado | no entró al sistema | no |
| Conectado / listo | disponible | sí |
| Con lead en pantalla | tomó un lead y todavía no lo cerró | **no** |
| En llamada | hablando | no |
| ACW (post-llamada) | colgó y todavía no calificó | no |
| En pausa | pausa manual (baño, almuerzo) | no |

**Por qué "con lead en pantalla" no recibe llamadas:** sin esta regla, al vendedor
le entra una llamada justo cuando iba a marcar, y pierde el lead que tenía abierto.
Se implementa pausando al agente en las colas cuando toma un lead y despausándolo
cuando lo cierra.

**El seguro:** un cron compara cada minuto el estado real de las colas contra el
estado deseado y corrige en los dos sentidos. Sin ese seguro, cualquier reinicio
deja gente pausada para siempre sin recibir leads, y nadie se da cuenta hasta que
alguien pregunta por qué no le llega trabajo.

---

## 2. Obtener un lead

El vendedor aprieta un solo botón. El servidor elige por él.

1. Busca entre **todas** las campañas activas del agente, no una sola.
2. Devuelve el lead de **mayor prioridad** (ver tabla abajo).
3. Si el lead tiene dueño y el dueño es otro, no se lo entrega.
4. Marca el lead como entregado a ese agente (reserva).
5. Lo pausa en las colas mientras lo tenga en pantalla.

**El mismo lead se le sigue entregando hasta que lo llame.** Si pide otro sin
llamar, le vuelve a salir el mismo: evita que la gente vaya salteando leads hasta
encontrar uno que le guste.

### Lead tomado y nunca llamado
Si pasan **10 minutos** sin marcarlo, vuelve a la cola:
- Si el lead **tiene dueño**, vuelve al dueño con su prioridad original.
- Si **no tiene dueño**, vuelve al pool general con **prioridad alta** para que el
  siguiente vendedor libre lo llame ya, y **se desasigna en el CRM** (se le había
  asignado al tomarlo, y no puede quedarse con un lead que nunca llamó).

---

## 3. Prioridades de marcación

El orden lo define `TIPO_ORDEN` en `crm_webhooks.py`. Menor número = se entrega primero.

| Orden | Tipo | Por qué está ahí |
|---|---|---|
| 0 | Recordatorio de cita | es hoy; si no se llama, se pierde la cita |
| 1 | Llamada perdida | el cliente llamó y nadie contestó: está esperando |
| 2 | Conversación pendiente | ya habló y quedó de confirmar algo |
| 3 | Callback agendado | pidió que lo llamen a una hora concreta |
| 4 | Respondió por mensaje | acaba de escribir: está disponible ahora |
| 5 | Lead nuevo | recién llegó |
| 6 | Seguimiento | intentos de días posteriores |

Dos detalles que importan:

**Los callbacks se inyectan un minuto antes de su hora**, no antes. Si no, un
callback de las 5 de la tarde te estorba toda la mañana.

**Las prioridades altas no se degradan.** Si un lead entró como "llamada perdida" y
después llega una inyección de "seguimiento" por el mismo contacto, mantiene la
prioridad alta durante 24 horas. Sin esta protección, el ruido del CRM entierra lo
urgente.

---

## 4. Llamar

- El vendedor **nunca ve el número completo**: la pantalla muestra `***-***-1234`.
  Marca por identificador de contacto. Es lo que impide que alguien se lleve la
  base de datos.
- El sistema llama primero al teléfono del vendedor y después marca al lead. Por eso
  el vendedor "recibe" su propia llamada saliente.
- El número se limpia a solo dígitos antes de marcar. Un `+` o un espacio mata la
  llamada en silencio.
- El caller ID lo elige el rotador (ver `05-TELEFONIA.md`): el mismo lead ve siempre
  el mismo número.

### Doble marcación
Llamar dos veces seguidas al mismo lead sube el porcentaje de contacto de forma
notable: mucha gente no contesta la primera pero sí la segunda.

La regla es: **después de guardar la disposición se puede volver a marcar al mismo
lead**, las veces que haga falta. El lead sale de la pantalla recién cuando el
vendedor pide el siguiente.

### Sin calificar no hay llamada nueva
Si la última llamada no está calificada, el botón de llamar no funciona, ni para el
mismo lead ni para otro. Sin esta regla aparecen llamadas huérfanas sin resultado y
las métricas dejan de servir.

Se aplica en dos capas: el servidor responde con un error específico, y el cliente
además bloquea el botón. La del servidor es la que manda.

---

## 5. Disposicionar

Al colgar, el agente entra en ACW y se le abre la pantalla de calificación. Guardar
la disposición:

1. Registra el resultado ligado al identificador único de esa llamada.
2. Saca al agente del ACW.
3. Dispara la escritura al CRM (tags, notas, dueño).
4. Habilita volver a marcar.

**Las disposiciones deben ir ordenadas alfabéticamente**, no por identificador
interno. Los vendedores las eligen por memoria de posición; que se muevan de lugar
genera errores de calificación.

### Control opcional: solo "contestó" si la llamada conectó
Existe la posibilidad de exigir que, para guardar una disposición de conversación,
la llamada haya sido efectivamente contestada. Evita que alguien marque "agendó
cita" en una llamada que nunca conectó.

Viene **apagado** por una razón: los buzones de voz cuentan como "contestada" para
la central, así que el control no distingue persona de contestador. Con la
transcripción activa se puede prender con criterio.

---

## 6. Dueño del lead ("sticky de por vida")

**Un lead que conversó con un vendedor es de ese vendedor para siempre.**

- Basta **una** disposición de conversación real para fijar al dueño.
- Desde ese momento, rediscados, callbacks y llamadas entrantes de ese número van
  siempre a la misma persona.
- Que después no conteste **no** le quita el dueño.
- Si el lead nunca conversó con nadie, sigue rotando libre en el pool.
- El dueño no se pierde ni si el vendedor se desactiva o se borra: sus leads quedan
  en cola a su nombre. Reasignar es una decisión manual, no automática.

**Por qué:** sin esto, dos vendedores llaman al mismo cliente, el cliente se molesta,
y adentro se pelean la comisión. Es la regla que más conflictos evita.

En el CRM esto se refleja como asignación del contacto al vendedor. El ciclo completo:

| Momento | Qué pasa en el CRM |
|---|---|
| El vendedor toma el lead | se le asigna, para que pueda entrar y ver el historial |
| Dispone "contestó" | queda asignado de forma permanente |
| Dispone "no contestó" y el lead no tiene dueño | se desasigna y vuelve a rotar |
| Dispone "no contestó" pero ya tiene dueño | **no** se toca: sigue siendo del dueño |
| Lo tomó y nunca lo llamó (10 min) | se desasigna si no tenía dueño |

---

## 7. Sesión única

Un vendedor, un dispositivo. El último que entra saca al anterior, en las dos
direcciones (computador ↔ celular, y celular ↔ celular).

**Por qué:** dos sesiones del mismo agente compiten por el mismo teléfono SIP.
Ninguna de las dos funciona bien y el diagnóstico es dificilísimo: las llamadas
"desaparecen" sin error.

---

## 8. Llamadas entrantes

- Si el número que llama **tiene dueño**, la llamada va solo a la cola de ese
  vendedor. Si está ocupado, en ACW o desconectado, timbra unos segundos y corta:
  no se le pasa a otro.
- Si **no tiene dueño**, va al grupo: suena en los vendedores libres.
- Si nadie contesta, el lead entra a la cola de marcación con prioridad alta como
  "llamada perdida". El cliente llamó: está esperando.
- Un número desconocido crea contacto nuevo automáticamente.

---

## 9. Qué pasa cuando algo se cae

El sistema está diseñado para recuperarse solo. Ningún cron asume que una orden
previa llegó: **comparan el estado real contra el deseado y corrigen en los dos
sentidos.**

| Se cae | Qué pasa |
|---|---|
| Django (aplicación) | las llamadas en curso siguen; los agentes se re-loguean solos |
| Asterisk (central) | las pausas se restauran desde el estado deseado en el próximo minuto |
| Redis (memoria) | un vigilante restaura las claves críticas |
| El servidor entero | al arrancar, el script de persistencia reaplica todos los parches |
| El selector de números | un chequeo lo reinicia; el dialplan tiene lista de respaldo |

Esa última parte importa: si el selector de caller ID no responde, el dialplan usa
una lista de números de respaldo en vez de dejar la llamada sin identificador. Nunca
dejes que una pieza opcional pueda tumbar la llamada.
