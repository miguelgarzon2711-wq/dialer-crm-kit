-- ============================================================================
--  Tables owned by this kit (added on top of the OMniLeads database, not
--  replacing it).
--  Apply with:
--    docker exec -i prod-env-postgresql-1 psql -U omnileads -d omnileads < schema.sql
--  It is idempotent: safe to run more than once.
--
--  NOTE ON NAMING: column names are in Spanish (contacto_id, agente_id,
--  telefono) because OMniLeads defines them that way and the kit code queries
--  them directly. Renaming them breaks the system.
-- ============================================================================

-- -- Permanent lead ownership ("lifetime sticky") ----------------------------
-- A lead that ever had a real CONVERSATION with a rep belongs to that rep
-- forever: redials, callbacks and inbound calls from that number always come
-- back to the same person. Without this, two reps call the same customer and
-- fight over the commission.
CREATE TABLE IF NOT EXISTS dialer_lead_owner (
    contacto_id integer PRIMARY KEY,
    agente_id   integer NOT NULL,
    since       timestamptz NOT NULL DEFAULT now(),
    motivo      text                      -- which disposition granted ownership
);

-- -- Dialer agent <-> CRM user mapping ---------------------------------------
-- Needed to assign the contact to the right rep inside the CRM when they take
-- the lead or when they disposition it as "answered".
CREATE TABLE IF NOT EXISTS dialer_agent_crm_map (
    agente_id       integer PRIMARY KEY,  -- ominicontacto_app_agenteprofile.id
    -- The user id inside the CRM. The column is named ghl_user_id because the
    -- kit was born integrated with GoHighLevel; the content is "the rep's id in
    -- whatever CRM you use". If you rename it, update the queries in
    -- crm_webhooks.py, crm_dispositions.py and backfill_inject.py as well.
    ghl_user_id     text NOT NULL,
    vendedor_nombre text
);

-- -- Transcribed call audit (Whisper + AI) -----------------------------------
-- One row per recorded and transcribed call. It serves three purposes:
--   1. never transcribe the same audio twice (saves API money),
--   2. detect that a rep talked to a voicemail and dispositioned it as if they
--      had spoken to a person (the alerta column),
--   3. store the AI-written note so the console can display it.
CREATE TABLE IF NOT EXISTS dialer_call_audit (
    callid            text PRIMARY KEY,   -- Asterisk uniqueid
    contacto_id       integer,
    agente            text,
    disposicion       text,
    campana_id        integer,
    duracion          integer,            -- seconds
    es_buzon          boolean,            -- the AI detected an answering machine
    idioma            text,               -- es | en | ...
    alerta            boolean DEFAULT false,
    transcript_inicio text,               -- first seconds, for review
    fecha             timestamptz DEFAULT now(),
    nota_crm          text                -- note written by the AI
);

CREATE INDEX IF NOT EXISTS dialer_call_audit_contacto_idx
    ON dialer_call_audit (contacto_id, fecha DESC);
CREATE INDEX IF NOT EXISTS dialer_lead_owner_agente_idx
    ON dialer_lead_owner (agente_id);

-- ============================================================================
--  CALLER ID ROTATION
--  Spreads outbound calls across several numbers so none of them burns out and
--  ends up flagged as "Spam Likely" on customers' phones.
--  Three rules, in this order:
--    1. Sticky: the same lead always sees the same number.
--    2. Daily cap with warm-up: a new number places few calls on its first
--       days and ramps up.
--    3. Never block: if every number hit its cap, use the least-used one
--       anyway. A call from a tired number beats no call at all.
-- ============================================================================

-- Pool of available numbers. Load the ones you bought here.
CREATE TABLE IF NOT EXISTS numbers_pool (
    did             varchar(15) PRIMARY KEY,       -- E.164 format without the "+"
    status          varchar(20) DEFAULT 'active',  -- active | paused | retired
    first_used_date date,                          -- filled in automatically on first use
    notes           text
);

-- Which number each lead was assigned (sticky).
CREATE TABLE IF NOT EXISTS did_sticky (
    telefono   varchar(15) PRIMARY KEY,   -- the lead's phone, 10 digits
    did        varchar(15) NOT NULL,
    updated_at timestamptz DEFAULT now()
);

-- Usage log, used to count how many calls each number placed today.
CREATE TABLE IF NOT EXISTS did_usage (
    id       bigserial PRIMARY KEY,
    did      varchar(15) NOT NULL,
    telefono varchar(15),
    ts       timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_did_usage_did_ts ON did_usage (did, ts);

-- -- The picker --------------------------------------------------------------
-- Returns which number to use when calling a given phone, and records the use.
-- The warm-up ramp lives in the CASE below: day 1 = 10 calls, day 2 = 25,
-- day 3 = 50, day 4 onward = 65. Tune those values to your operation.
CREATE OR REPLACE FUNCTION pick_did(p_tel text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  v_tel TEXT := right(regexp_replace(p_tel, '\D', '', 'g'), 10);
  v_did TEXT;
BEGIN
  -- 1) sticky: same number for the same lead (if that number is still active)
  SELECT s.did INTO v_did
    FROM did_sticky s
    JOIN numbers_pool n ON n.did = s.did AND n.status = 'active'
   WHERE s.telefono = v_tel;

  IF v_did IS NULL THEN
    -- 2) an active number with room left according to its age (the ramp)
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

    -- 3) everything hit the cap: use the least loaded one anyway
    IF v_did IS NULL THEN
      SELECT n.did INTO v_did FROM numbers_pool n
       WHERE n.status = 'active'
       ORDER BY (SELECT count(*) FROM did_usage u
                  WHERE u.did = n.did AND u.ts >= CURRENT_DATE) ASC,
                random()
       LIMIT 1;
    END IF;

    IF v_did IS NULL THEN RETURN ''; END IF;   -- empty pool

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

-- Load the number pool (example):
--   INSERT INTO numbers_pool (did) VALUES ('15551000001'), ('15551000002')
--     ON CONFLICT DO NOTHING;
