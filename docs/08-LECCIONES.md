# Lecciones que costaron caro

Cada una de estas salió de un problema real en producción: horas de diagnóstico,
llamadas perdidas o dinero gastado. Leerlas ahora te ahorra repetirlas.

Están ordenadas por lo caro que salieron.

---

## 1. Un puerto SIP abierto es dinero de otro

**Qué pasó.** A los días de instalar el dialer, aparecieron softphones desconocidos
registrados en la central haciéndose pasar por extensiones de agentes. Estaban a un
paso de hacer llamadas internacionales a costa del cliente.

**Por qué.** La instalación por defecto deja el puerto SIP escuchando a internet, y
los escáneres automáticos prueban extensiones y claves las 24 horas.

**Qué hacer.** Cerrar los puertos **el día uno**, no cuando esté todo listo:
- Bloquear los mensajes de registro que no vengan del proxy interno.
- Cerrar al exterior todos los puertos internos (base de datos, memoria, interfaz de
  administración de la central). En Docker esto va en la cadena que filtra el
  tráfico hacia los contenedores; una regla normal de firewall no alcanza.
- Un cron que borre periódicamente cualquier registro que no venga del proxy.

**Cómo verificarlo.** Escaneá los puertos **desde afuera**, desde otra máquina. Desde
el propio servidor todo se ve bien siempre.

---

## 2. Un error de SQL te borra el lead del vendedor

**Qué pasó.** Los vendedores reportaban que el lead "se perdía": lo pedían, aparecía
un instante, y desaparecía.

**Por qué.** Una consulta a una columna que no existía. En PostgreSQL, un error de
SQL **aborta la transacción completa** aunque captures la excepción en Python. Todo
lo que venía después en esa misma petición fallaba, incluida la entrega del lead.

**Qué hacer.** Toda consulta opcional va en su propio savepoint:
```python
try:
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SELECT ...")
except Exception:
    pass
```
Y nunca asumas que una columna existe porque "debería".

---

## 3. Un "+" mata la llamada sin dejar rastro

**Qué pasó.** Leads que no se podían llamar. Sin error, sin log, sin nada: se marcaba
y no pasaba absolutamente nada.

**Por qué.** El número tenía formato internacional con `+`. El plan de marcación de
la central espera solo dígitos; con el `+` ninguna regla coincide y la llamada muere
en silencio.

**Qué hacer.** Normalizar en **todos** los caminos de entrada: al inyectar desde el
CRM, al marcar desde la consola, al marcar desde la API, en la carga masiva inicial.
Y además un cron cada 10 minutos como red de seguridad, porque siempre hay un camino
que no se te ocurrió.

**Lección general:** cuando algo falla en silencio, sospechá del formato de los datos
antes que de la lógica.

---

## 4. Lo que editás dentro de un contenedor, desaparece

**Qué pasó.** Parches que funcionaban dejaban de estar después de un reinicio, y el
sistema volvía a fallar como si nunca se hubiera arreglado.

**Por qué.** Los contenedores se recrean. Todo lo que esté adentro y no venga de la
imagen se pierde.

**Qué hacer.** Disciplina de tres pasos, sin excepción:
1. Editás la copia en el **host**.
2. La copiás al contenedor.
3. Agregás el bloque al script de persistencia que corre al arranque.

Y probá el reinicio de verdad. Un sistema que no sobrevive un reboot no está terminado.

---

## 5. Nunca `docker compose up`

Levanta y recrea contenedores que tienen estado, y perdés todo lo que hay adentro.
En un stack de contact center, eso es el sistema entero.

Usá siempre operaciones individuales: `docker exec`, `docker cp`, `docker restart <nombre>`.

---

## 6. El código que traés de otro cliente se roba las llamadas

**Qué pasó.** Al clonar un dialer que ya funcionaba para montar uno nuevo, las
llamadas del cliente nuevo salían por el proveedor del cliente viejo.

**Por qué.** Las configuraciones personalizadas del origen viajaron en la copia:
rutas de salida, credenciales de proveedor, identificadores.

**Qué hacer al clonar:**
- Vaciar todas las configuraciones personalizadas antes de empezar.
- Rotar **todos** los secretos: claves de la central, del proxy SIP, de la base, de
  la interfaz de administración.
- Buscar el nombre del cliente viejo en todo el árbol y en la base de datos.
- Probar con una llamada real y verificar por dónde salió.

---

## 7. Las fechas en la zona horaria equivocada arruinan los reportes

Guardá siempre en tiempo universal, y convertí a la zona del cliente **solo al
mostrar**. Una hora mal convertida hace que un callback de las 5 de la tarde se
inyecte a las 10 de la mañana, o que un reporte diario incluya llamadas de otro día.

Ojo con el caso frecuente: el servidor en un país, el cliente en otro, y el operador
en un tercero.

---

## 8. Cobrás por números aunque no los uses

Los números telefónicos se pagan **por mes, cada uno**, aunque no hagas ni una
llamada. Un pool de 80 números cuesta unos 80 dólares mensuales fijos.

Antes de comprar, hacé la cuenta: marcaciones por día ÷ tope diario por número. Y
revisá cuándo cobra tu proveedor: puede ser por mes calendario o por aniversario de
compra. Si tenés recarga automática con saldo insuficiente, la renovación falla y te
quedás sin números de un día para otro.

---

## 9. La transcripción se paga por audio, no por llamada

**Qué pasó.** El costo de transcribir se disparó sin que subiera el volumen de llamadas.

**Por qué.** El proceso reprocesaba las mismas grabaciones una y otra vez.

**Qué hacer.** Registrar qué audio ya se transcribió (por identificador único de
llamada), verificar antes de mandar, y usar un candado para que dos corridas
simultáneas no dupliquen trabajo. En el caso real, el ahorro fue de unos 35 dólares
al mes con volumen bajo; con volumen alto es mucho más.

---

## 10. Que la configuración "se vea bien" no significa nada

Un trunk mal nombrado, un contexto de plan de marcación faltante o un prefijo mal
puesto se ven perfectos en el panel y no fallan hasta que marcás de verdad.

Probá siempre con **una llamada real**: que suene, que se escuche en los dos
sentidos, que al colgar quede el registro guardado.

---

## 11. Sin sticky, tu equipo se pelea

Dos vendedores llamando al mismo cliente el mismo día es la forma más rápida de
quedar mal con el cliente y generar conflictos internos por la comisión.

La regla que lo resuelve: **el primero que conversa se queda con el lead para
siempre.** Que después no conteste no cambia nada.

---

## 12. Todo automatismo debe repararse en los dos sentidos

Un cron que solo aplica un cambio en una dirección deja el sistema roto en cuanto
algo se reinicia. Ejemplo real: la pausa que evita que a un vendedor le entren
llamadas mientras tiene un lead abierto. Si el cron solo pausa y nunca despausa, un
reinicio deja gente pausada para siempre sin recibir trabajo, y nadie se entera.

Los automatismos deben **comparar el estado real contra el deseado y corregir en
ambas direcciones**, sin asumir que ninguna orden previa llegó.

---

## 13. Una pieza opcional nunca puede tumbar la llamada

El selector de caller ID es un servicio auxiliar. Si se cae y el plan de marcación
lo espera sin alternativa, se cae todo el sistema de llamadas.

Poné tiempo de espera corto y una alternativa por defecto en cada consulta a un
servicio externo. Es mejor una llamada con un número menos óptimo que ninguna llamada.

---

## 14. En móvil, el audio arranca cuando el sistema operativo lo dice

**Qué pasó (aplica si construís un cliente móvil).** Sonaba un timbre y el audio se
cortaba.

**Por qué.** La aplicación contestaba la llamada antes de que el sistema operativo
activara la sesión de audio. El motor de audio arrancaba sobre una sesión que después
cambiaba, y quedaba mudo.

**Qué hacer.** Esperar el aviso de "sesión de audio activada" del sistema y recién
ahí contestar. Con un tope de tiempo, para no quedarse esperando para siempre.

---

## 15. Cuando algo falla, buscá la causa raíz antes de cambiar de táctica

La tentación es probar otra cosa. Casi siempre sale más barato leer el registro real,
capturar el tráfico o comparar contra un caso que sí funciona.

Un ejemplo de esta misma operación: durante horas se persiguió un problema de audio
en la aplicación móvil. Capturar el tráfico de red mostró que el audio fluía perfecto
en los dos sentidos: el problema era el número al que se estaba llamando, que
contestaba y colgaba a los pocos segundos.
