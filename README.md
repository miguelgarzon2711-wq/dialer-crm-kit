# Dialer + CRM Kit

Integración lista para producción entre un **dialer OMniLeads** y un **CRM**
(probado con GoHighLevel): los leads entran solos, se marcan por prioridad, y el
resultado de cada llamada vuelve al CRM sin que nadie copie y pegue nada.

Salió de un sistema real en operación con ~20 vendedores y ~80 leads nuevos por día.
Todo lo que hay acá corrió en producción; los errores que costaron caro están
documentados para que no los repitas.

---

## Qué resuelve

Un equipo de ventas que llama leads suele tener los mismos cinco problemas. Este kit
los ataca de raíz:

| Problema | Qué hace el kit |
|---|---|
| Los leads llegan al CRM y nadie los llama a tiempo | webhook los inyecta en la cola en segundos, ordenados por probabilidad de contestar |
| Dos vendedores llaman al mismo cliente y se pelean la comisión | el primero que conversa queda dueño del lead de por vida |
| Los números salen marcados como "Spam Likely" y nadie contesta | rotación de caller ID con tope diario por número y calentamiento gradual |
| Nadie sabe qué se habló en las llamadas | transcripción con IA, nota automática en el CRM y alerta si el vendedor dispuso un buzón como si fuera una persona |
| Se reinicia el servidor y hay que reconfigurar medio sistema | los parches se reaplican solos al arranque |

---

## Qué necesitás antes de empezar

- Un servidor propio con **OMniLeads** instalado (Docker). Referencia: 4 vCPU / 8 GB
  para unos 20 agentes simultáneos.
- Una cuenta en un **proveedor SIP** (el kit está probado con Telnyx) y números
  del área que vas a llamar.
- Una cuenta de **CRM** con acceso a su API. El kit trae la integración de
  GoHighLevel hecha; para otro CRM se reescribe una sola capa.
- Opcional: una clave de **OpenAI** si querés transcripción y notas automáticas.
- Alguien con acceso `root` al servidor.

---

## Cómo se instala

**Si estás usando un agente de IA (Claude Code o similar), decile que lea
[`CLAUDE.md`](CLAUDE.md) primero.** Ese archivo está escrito para él: trae el orden
correcto de instalación, las reglas que no se pueden romper y los errores conocidos.

Resumen del recorrido:

1. OMniLeads funcionando y probado.
2. **Cerrar el servidor** (firewall). Esto va temprano, no al final.
3. Telefonía: trunk, números, y una llamada real que suene.
4. Base de datos: `server/sql/schema.sql`.
5. Credenciales: copiar `.env.example` y completarlo.
6. Integración con el CRM.
7. Automatizaciones (cron).
8. Persistencia de parches y prueba de reinicio.

El detalle de cada paso está en [`docs/02-INSTALACION.md`](docs/02-INSTALACION.md).

---

## Documentación

| Documento | De qué trata |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | **Empezá acá.** Instrucciones para el agente que instala, reglas duras y errores conocidos |
| [`docs/01-ARQUITECTURA.md`](docs/01-ARQUITECTURA.md) | Cómo encajan las piezas y por dónde viaja una llamada |
| [`docs/02-INSTALACION.md`](docs/02-INSTALACION.md) | Paso a paso desde un servidor vacío |
| [`docs/03-INTEGRACION-CRM.md`](docs/03-INTEGRACION-CRM.md) | Webhooks, campos, prioridades y ciclo de asignación |
| [`docs/04-COMPORTAMIENTO.md`](docs/04-COMPORTAMIENTO.md) | Las reglas de negocio: qué hace el dialer y por qué |
| [`docs/05-TELEFONIA.md`](docs/05-TELEFONIA.md) | Trunk, rotación de números, entrantes y seguridad SIP |
| [`docs/06-TRANSCRIPCION-IA.md`](docs/06-TRANSCRIPCION-IA.md) | Notas automáticas y auditoría de buzones |
| [`docs/07-OPERACION.md`](docs/07-OPERACION.md) | Cron, persistencia, vigilancia y diagnóstico |
| [`docs/08-LECCIONES.md`](docs/08-LECCIONES.md) | Lo que costó caro aprender |
| [`docs/09-PROBLEMAS-CONOCIDOS.md`](docs/09-PROBLEMAS-CONOCIDOS.md) | Lo que sabemos que está imperfecto, y cómo resolverlo |

---

## Qué hay en cada carpeta

```
server/
  django/     módulos que se copian al contenedor de Django
              crm_webhooks.py       recibe leads del CRM
              crm_dispositions.py   devuelve resultados al CRM
              lead_ownership.py     dueño permanente del lead
              agent_api.py          API REST para clientes móviles
  asterisk/   dialplan y colas
  scripts/    tareas programadas y mantenimiento
  sql/        tablas propias del kit
  patches/    cambios a aplicar sobre archivos de OMniLeads
tools/        verificador de credenciales antes de publicar
docs/         documentación
```

---

## Seguridad

Este repositorio **no contiene ninguna credencial**. Todo secreto vive en
`/root/.env_dialer` (permisos 600) fuera del control de versiones.

Antes de subir cualquier cambio, corré:

```bash
bash tools/check_secrets.sh
```

Busca claves, tokens, IPs, teléfonos y dominios reales que se te hayan escapado.
Si encuentra algo, no subas nada hasta limpiarlo.

---

## Licencia y uso

Uso privado bajo autorización del autor. OMniLeads y Asterisk tienen sus propias
licencias: este kit no las incluye ni las redistribuye, solo se apoya en ellas.
