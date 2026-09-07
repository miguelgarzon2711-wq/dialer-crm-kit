-- ============================================================================
--  Tablas propias del kit (se agregan a la base de OMniLeads, no la reemplazan)
--  Aplicar con:
--    docker exec -i prod-env-postgresql-1 psql -U omnileads -d omnileads < schema.sql
--  Es idempotente: se puede correr varias veces sin romper nada.
-- ============================================================================

-- ── Dueño permanente del lead ("sticky de por vida") ────────────────────────
-- Un lead que alguna vez CONVERSÓ con un vendedor queda de ese vendedor para
-- siempre: los rediscados, los callbacks y las llamadas entrantes de ese
-- número vuelven siempre a la misma persona. Sin esto, dos vendedores llaman
-- al mismo cliente y se pelean la comisión.
CREATE TABLE IF NOT EXISTS dialer_lead_owner (
    contacto_id integer PRIMARY KEY,
    agente_id   integer NOT NULL,
    since       timestamptz NOT NULL DEFAULT now(),
    motivo      text                      -- qué disposición lo hizo dueño
);

-- ── Mapeo agente del dialer  <->  usuario del CRM ───────────────────────────
-- Necesario para asignar el contacto al vendedor correcto dentro del CRM
-- cuando toma el lead o cuando lo dispone como "contestó".
CREATE TABLE IF NOT EXISTS dialer_agent_crm_map (
    agente_id       integer PRIMARY KEY,  -- ominicontacto_app_agenteprofile.id
    -- Id del usuario dentro del CRM. Se llama ghl_user_id porque el kit nacio
    -- integrado a GoHighLevel; el contenido es "el id del vendedor en el CRM que
    -- uses". Si la renombras, actualiza tambien las consultas de crm_webhooks.py,
    -- crm_dispositions.py y backfill_inject.py.
    ghl_user_id     text NOT NULL,
    vendedor_nombre text
);

-- ── Auditoría de llamadas transcritas (Whisper + IA) ────────────────────────
-- Una fila por llamada grabada y transcrita. Sirve para tres cosas:
--   1. no volver a transcribir el mismo audio (ahorra dinero de API),
--   2. detectar que el vendedor habló con un buzón y lo dispuso como si
--      hubiera hablado con una persona (columna alerta),
--   3. guardar la nota redactada por la IA para mostrarla en la consola.
CREATE TABLE IF NOT EXISTS dialer_call_audit (
    callid            text PRIMARY KEY,   -- uniqueid de Asterisk
    contacto_id       integer,
    agente            text,
    disposicion       text,
    campana_id        integer,
    duracion          integer,            -- segundos
    es_buzon          boolean,            -- la IA detectó contestador automático
    idioma            text,               -- es | en | ...
    alerta            boolean DEFAULT false,
    transcript_inicio text,               -- primeros segundos, para revisar
    fecha             timestamptz DEFAULT now(),
    nota_crm          text                -- nota redactada por la IA
);

CREATE INDEX IF NOT EXISTS dialer_call_audit_contacto_idx
    ON dialer_call_audit (contacto_id, fecha DESC);
CREATE INDEX IF NOT EXISTS dialer_lead_owner_agente_idx
    ON dialer_lead_owner (agente_id);

-- ============================================================================
--  ROTACIÓN DE CALLER ID
--  Reparte las llamadas salientes entre varios números para que ninguno se
--  queme y termine marcado como "Spam Likely" en los celulares.
--  Tres reglas, en este orden:
--    1. Sticky: al mismo lead se le muestra siempre el mismo número.
--    2. Cupo diario con calentamiento: un número nuevo hace pocas llamadas los
--       primeros días y va subiendo.
--    3. Nunca bloquear: si todos llegaron al tope, usa el menos usado igual.
--       Es preferible una llamada con un número cansado que ninguna llamada.
-- ============================================================================

-- Pool de números disponibles. Cargar acá los que compraste.
CREATE TABLE IF NOT EXISTS numbers_pool (
    did             varchar(15) PRIMARY KEY,   -- formato E.164 sin el "+"
    status          varchar(20) DEFAULT 'active',  -- active | paused | retired
    first_used_date date,                      -- se completa solo al primer uso
    notes           text
);

-- Qué número le tocó a cada lead (sticky).
CREATE TABLE IF NOT EXISTS did_sticky (
    telefono   varchar(15) PRIMARY KEY,        -- teléfono del lead, 10 dígitos
    did        varchar(15) NOT NULL,
    updated_at timestamptz DEFAULT now()
);

-- Registro de uso, para contar cuántas llamadas hizo hoy cada número.
CREATE TABLE IF NOT EXISTS did_usage (
    id       bigserial PRIMARY KEY,
    did      varchar(15) NOT NULL,
    telefono varchar(15),
    ts       timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_did_usage_did_ts ON did_usage (did, ts);

-- ── El selector ─────────────────────────────────────────────────────────────
-- Devuelve qué número usar para llamar a un teléfono dado, y registra el uso.
-- La rampa de calentamiento está en el CASE: día 1 = 10 llamadas, día 2 = 25,
-- día 3 = 50, día 4 en adelante = 65. Ajustá esos valores a tu operación.
CREATE OR REPLACE FUNCTION pick_did(p_tel text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  v_tel TEXT := right(regexp_replace(p_tel, '\D', '', 'g'), 10);
  v_did TEXT;
BEGIN
  -- 1) sticky: mismo número para el mismo lead (si ese número sigue activo)
  SELECT s.did INTO v_did
    FROM did_sticky s
    JOIN numbers_pool n ON n.did = s.did AND n.status = 'active'
   WHERE s.telefono = v_tel;

  IF v_did IS NULL THEN
    -- 2) número activo con cupo disponible según su edad (rampa)
    SELECT n.did INTO v_did FROM numbers_pool n
     WHERE n.status = 'active'
       AND (SELECT count(*) FROM did_usage u
             WHERE u.did = n.did AND u.ts >= CURRENT_DATE) <
           (CASE COALESCE(CURRENT_DATE - n.first_used_date, 0)
              WHEN 0 THEN 10 WHEN 1 THEN 25 WHEN 2 THEN 50 ELSE 65 END)
     ORDER BY (SELECT count(*) FROM did_usage u
                WHERE u.did = n.did AND u.ts >= CURRENT_DATE) ASC,
              random()
     LIMIT 1;

    -- 3) todos llegaron al tope: usar el menos cargado igual
    IF v_did IS NULL THEN
      SELECT n.did INTO v_did FROM numbers_pool n
       WHERE n.status = 'active'
       ORDER BY (SELECT count(*) FROM did_usage u
                  WHERE u.did = n.did AND u.ts >= CURRENT_DATE) ASC,
                random()
       LIMIT 1;
    END IF;

    IF v_did IS NULL THEN RETURN ''; END IF;   -- pool vacío

    INSERT INTO did_sticky(telefono, did) VALUES (v_tel, v_did)
      ON CONFLICT (telefono) DO UPDATE
        SET did = EXCLUDED.did, updated_at = now();
  END IF;

  UPDATE numbers_pool SET first_used_date = COALESCE(first_used_date, CURRENT_DATE)
   WHERE did = v_did;
  INSERT INTO did_usage(did, telefono) VALUES (v_did, v_tel);
  RETURN v_did;
END;
$$;

-- Cargar el pool de números (ejemplo):
--   INSERT INTO numbers_pool (did) VALUES ('15551000001'), ('15551000002')
--     ON CONFLICT DO NOTHING;
