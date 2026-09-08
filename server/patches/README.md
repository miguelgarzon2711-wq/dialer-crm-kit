# Parches sobre OMniLeads

Este kit **no redistribuye OMniLeads**. Lo que hay acá son los cambios puntuales que
hay que aplicarle a los archivos que ya trae tu instalación, con el bloque de código
exacto y la explicación de por qué existe cada uno.

Los bloques están en:

- `patches_django.txt` — cambios a los archivos Python, plantillas y JavaScript de OMniLeads.
- `patches_dialplan.txt` — cambios al plan de marcación de Asterisk.

Cada bloque viene con el número de línea de referencia y sus líneas de contexto. Los
números de línea corresponden a **una** versión de OMniLeads: en la tuya van a estar
corridos. **No apliques por número de línea: buscá la función o el bloque por nombre.**

---

## Cómo aplicar un parche

1. Copiá el archivo original desde el contenedor al host:
   ```bash
   docker cp prod-env-django-app-1:/opt/omnileads/ominicontacto/<ruta>/<archivo>.py \
             /opt/dialer-kit/patches/<archivo>.py
   ```
2. Editá la copia del host aplicando el bloque correspondiente.
3. Validá la sintaxis. **Este paso no es opcional:** un error acá tumba Django entero.
   ```bash
   python3 -c "import ast; ast.parse(open('<archivo>.py').read())" && echo OK
   ```
4. Copiala de vuelta y reiniciá:
   ```bash
   docker cp /opt/dialer-kit/patches/<archivo>.py \
             prod-env-django-app-1:/opt/omnileads/ominicontacto/<ruta>/<archivo>.py
   docker restart prod-env-django-app-1
   ```
5. **Agregá el bloque a `restore_patches.sh`** para que sobreviva a los reinicios.
   Si te salteás este paso, el parche desaparece solo y nadie va a entender por qué.
6. Verificá que la aplicación levantó:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" https://<TU_DOMINIO>/accounts/login/
   ```
   Tiene que devolver `200`. Si no, revisá los registros del contenedor.

Para los cambios de JavaScript hay dos pasos extra después de copiar, porque los
archivos estáticos se sirven comprimidos:
```bash
docker exec prod-env-django-app-1 python3 /opt/omnileads/ominicontacto/manage.py collectstatic --noinput
docker exec prod-env-django-app-1 python3 /opt/omnileads/ominicontacto/manage.py compress --force
```

Para los cambios de plan de marcación no hace falta reiniciar nada:
```bash
docker exec prod-env-acd-1 asterisk -rx "dialplan reload"
```

---

## Qué hace cada parche y por qué

### En `models.py`

| Parche | Qué resuelve |
|---|---|
| **No auto-finalizar campañas** | OMniLeads cierra sola una campaña de tipo Preview cuando se le acaban los contactos. Con leads que entran de a poco durante el día, la campaña se cierra a media mañana y deja de recibir. |
| **Botón unificado de entrega** | De fábrica, el agente elige campaña y después pide lead. Con este cambio, un solo botón entrega el lead de mayor prioridad **entre todas** sus campañas. Menos decisiones para el vendedor, mejor orden de marcación. |
| **Prioridad pura** | Respeta estrictamente el campo de prioridad al elegir el siguiente lead, en vez de mezclarlo con otros criterios. |
| **Varios registros por contacto** | Con reinyecciones, un mismo contacto puede tener varios registros de relación agente-contacto. Sin esto, guardar la disposición de un lead reinyectado da error. |
| **Preservar el identificador externo** | Mantiene el id del contacto en el CRM dentro de la tarjeta del lead, para poder abrirlo desde la consola. |
| **Validar antes de finalizar** | Corre la validación de la disposición **antes** de cerrar la relación agente-contacto, no después. Si valida después, ya es tarde: el lead quedó cerrado con una disposición inválida. |

### Number masking (several files)

El vendedor nunca ve el teléfono completo del lead: ve `***-***-1234`. Hay que
enmascarar en **cinco** lugares, y si te olvidás de uno el número se filtra por ahí:

1. La tarjeta del lead (`views_campana_preview.py`).
2. El formulario de disposición (`views_calificacion_cliente.py`).
3. El teléfono del navegador, que recibe el número por la señalización (plan de marcación).
4. Al marcar desde la consola: llega enmascarado y hay que **restaurar el real** desde
   la base antes de llamar (`views_agente.py`).
5. Al guardar la disposición: mismo caso, restaurar el real (`views_calificacion_cliente.py`).

### Otros

| Parche | Archivo | Qué resuelve |
|---|---|---|
| **Solo dígitos al marcar** | `views_agente.py` | Un `+` o un espacio mata la llamada en silencio. Limpia el número pase lo que pase con el dato de origen. |
| **Dispositions in alphabetical order** | `forms_base.py` | Out of the box they come sorted by internal id. Reps pick them by position; if they move around, wrong dispositions get filed. |
| **Dominio propio permitido** | `settings` | Sin esto, cualquier envío de formulario desde tu dominio devuelve error 403. |
| **Duración del token de API** | `settings` | El token de API dura 9 horas de fábrica. Para webhooks que corren para siempre, se extiende a un año. |
| **Botón unificado (interfaz)** | `campanasPreviewAgente.js` | La parte visual del botón único. Requiere los dos comandos de archivos estáticos de arriba. |
| **Liberar lead en pausa** | `agent_activity.py` | Si el agente entra en pausa con un lead abierto, el lead se libera en vez de quedar bloqueado. |
| **Cuatro procesos de aplicación** | `oml_uwsgi.ini` | De fábrica viene con uno solo: con varios agentes, una petición lenta bloquea a todos. |

### In the dialplan (`patches_dialplan.txt`)

| Parche | Qué resuelve |
|---|---|
| **Selector de caller ID** | Antes de marcar, consulta al servicio de rotación qué número usar para ese lead. Tiene tiempo de espera corto y una lista de respaldo: si el servicio no responde, la llamada igual sale. |
| **Formato internacional garantizado** | Algunos proveedores exigen el `+` con código de país. Se asegura sin depender de la configuración de prefijos. |
| **Enmascarado hacia el agente** | En el tramo hacia el vendedor, el número viaja enmascarado. El tramo hacia el proveedor y los registros mantienen el número real. |
| **Ruta de salida manual** | El generador de rutas de OMniLeads no arma bien este contexto. Hay que escribirlo a mano (está en `../asterisk/extensions_outbound_route.conf`). |
| **Números de prueba internos** | Desvía un rango de números falsos a contextos locales que simulan eco, buzón de voz y nadie-contesta. Sirve para probar el sistema sin gastar minutos ni molestar a nadie. Muy recomendable para las demostraciones. |

---

## Recomendación

Aplicá los parches de a uno, verificando después de cada uno. Si aplicás cinco
juntos y algo se rompe, no vas a saber cuál fue.
