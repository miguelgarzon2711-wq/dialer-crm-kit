# Problemas conocidos del kit

Cosas que sabemos que están imperfectas en este código. Están acá para que no las
descubras en producción y para que decidas si te importan antes de instalar.

Ninguna impide que el sistema funcione. Todas tienen solución conocida.

---

## 1. La carga inicial de leads necesita un paso manual

`backfill_prepare.py` escribe el resultado en `/root/backfill_leads.json`, pero
`backfill_inject.py` lo lee de `/tmp/backfill_leads.json`. Entre los dos pasos hay
que copiar el archivo a mano:

```bash
python3 backfill_prepare.py --dias 30
cp /root/backfill_leads.json /tmp/backfill_leads.json     # <-- este paso falta
python3 backfill_inject.py
```

**Solución:** unificá la ruta en los dos archivos, o dejá el `cp` documentado en tu
runbook. Es un proceso que corre una sola vez, así que tampoco es grave.

---

## 2. El modo "asignar" del backfill no hace nada

`backfill_prepare.py` acepta un modo `asignar` (mandar cada lead viejo al vendedor
que ya lo había atendido) pero en la práctica siempre marca los leads como `pool`.
El modo existe en la documentación y en `backfill_inject.py`, pero quien genera la
lista nunca produce el otro valor.

**Consecuencia:** hoy la carga inicial siempre manda todo al pool general.

**Solución:** si necesitás el modo asignar, hay que completar la lógica en
`backfill_prepare.py`. Si no lo necesitás, ignoralo: el modo pool es el que se usó
en producción y funciona.

---

## 3. La lista de disposiciones "que contestaron" está duplicada

El mismo conjunto de disposiciones está definido dos veces, en dos archivos
distintos:

- `CONTESTO` en `crm_dispositions.py` — decide si se asigna el lead en el CRM.
- `DISPOS_CONVERSACION` en `lead_ownership.py` — decide si el vendedor queda dueño.

Hoy son idénticas. Si editás una y te olvidás de la otra, el sistema empieza a
comportarse de forma incoherente: el lead queda asignado en el CRM pero sin dueño en
el dialer, o al revés. Y cuesta mucho de diagnosticar.

**Solución recomendada:** al adaptar el kit, definí la lista **una sola vez** en un
módulo compartido e importala en los dos lugares. Si preferís no tocarlo, dejá un
comentario en ambos archivos que apunte al otro.

---

## 4. Los números de línea de los parches no van a coincidir

Los bloques de `server/patches/` traen números de línea de la versión de OMniLeads
donde se hicieron. En tu instalación van a estar corridos.

**Solución:** buscá siempre por nombre de función o por el texto del bloque, nunca
por número de línea.

---

## 5. El control de "solo contestó si conectó" viene apagado

Existe una validación que impide guardar una disposición de conversación si la
llamada nunca fue contestada. Viene desactivada a propósito: los buzones de voz
cuentan como "contestada" para la central, así que el control no distingue entre
una persona y un contestador automático, y bloquearía disposiciones legítimas.

**Solución:** activalo solo después de tener la transcripción funcionando, que sí
distingue buzón de persona.

---

## 6. Los scripts asumen los nombres de contenedor de OMniLeads

Los scripts llaman a los contenedores por nombre fijo (`prod-env-django-app-1`,
`prod-env-acd-1`, `prod-env-postgresql-1`, `prod-env-redis-1`). Si tu instalación
usa otros nombres, hay que reemplazarlos.

**Solución:** verificá los nombres reales con `docker ps --format '{{.Names}}'` y
hacé un reemplazo global antes de instalar. Conviene sacarlos a variables al
principio de cada script.

---

## 7. Los mensajes están en español

Los mensajes que ve el vendedor, los registros y los comentarios del código están en
español. Si el equipo de tu cliente habla otro idioma, hay que traducirlos.

**Solución:** los textos visibles están concentrados en pocos lugares. Los comentarios
del código conviene dejarlos: explican decisiones, y traducirlos automáticamente
suele perder el matiz de por qué existe cada regla.
