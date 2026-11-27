-- Second sample schema for generic mode (PG_MCP_BOOKSTORE_TOOLS=0).
-- Synthetic data: patient codes and birth years only, no names or real records.
-- Load into an empty database named clinic:
--   createdb clinic && psql -d clinic -v ON_ERROR_STOP=1 -f seed/sample_clinic.sql

BEGIN;

-- Tables

CREATE TABLE clinicians (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    specialty TEXT NOT NULL
);

CREATE TABLE patients (
    id SERIAL PRIMARY KEY,
    patient_code TEXT NOT NULL UNIQUE,
    birth_year INTEGER CHECK (birth_year BETWEEN 1900 AND 2026),
    city TEXT
);

CREATE TABLE appointments (
    id SERIAL PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    clinician_id INTEGER NOT NULL REFERENCES clinicians(id),
    scheduled_at TIMESTAMPTZ NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'completed', 'cancelled'))
);

-- Deliberately not granted below: shows the allowlist on a second schema.
CREATE TABLE billing_notes (
    id SERIAL PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    note TEXT NOT NULL
);

CREATE INDEX idx_appointments_patient ON appointments(patient_id);
CREATE INDEX idx_appointments_clinician ON appointments(clinician_id);

-- Seed clinicians (fictional)

INSERT INTO clinicians (name, specialty) VALUES
    ('Dr. Clinician One',   'Family Medicine'),
    ('Dr. Clinician Two',   'Cardiology'),
    ('Dr. Clinician Three', 'Dermatology'),
    ('Dr. Clinician Four',  'Pediatrics');

-- Seed patients (synthetic codes)

INSERT INTO patients (patient_code, birth_year, city) VALUES
    ('P-1001', 1958, 'Springfield'),
    ('P-1002', 1972, 'Riverton'),
    ('P-1003', 1985, 'Springfield'),
    ('P-1004', 1994, 'Lakeside'),
    ('P-1005', 2012, 'Riverton'),
    ('P-1006', 2018, 'Lakeside');

-- Seed appointments

INSERT INTO appointments (patient_id, clinician_id, scheduled_at, reason, status) VALUES
    (1, 2, '2026-10-01 09:00+00', 'Follow-up visit',  'completed'),
    (1, 1, '2026-11-03 10:30+00', 'Annual check-up',  'completed'),
    (2, 2, '2026-10-14 14:00+00', 'Consultation',     'completed'),
    (2, 3, '2026-12-02 11:00+00', 'Skin check',       'scheduled'),
    (3, 1, '2026-10-20 08:30+00', 'Annual check-up',  'cancelled'),
    (3, 1, '2026-11-18 08:30+00', 'Annual check-up',  'completed'),
    (4, 3, '2026-12-05 15:00+00', 'Consultation',     'scheduled'),
    (5, 4, '2026-11-10 09:15+00', 'Vaccination',      'completed'),
    (6, 4, '2026-11-24 13:45+00', 'Routine visit',    'completed'),
    (6, 4, '2026-12-15 13:45+00', 'Follow-up visit',  'scheduled');

INSERT INTO billing_notes (appointment_id, note) VALUES
    (1, 'synthetic billing note one'),
    (3, 'synthetic billing note two');

-- Read-only role for the MCP server (its own role, so both samples can share one cluster)

CREATE ROLE clinic_readonly WITH LOGIN PASSWORD 'clinic_readonly';
GRANT CONNECT ON DATABASE clinic TO clinic_readonly;
GRANT USAGE ON SCHEMA public TO clinic_readonly;

-- Table allowlist, enforced by the database: SELECT on these three tables only.
-- billing_notes is not granted, and there is deliberately no GRANT ... ON ALL TABLES
-- and no ALTER DEFAULT PRIVILEGES, so a table added later stays unreadable.
-- Sequences are not granted either, so currval/nextval/setval are denied.
GRANT SELECT ON TABLE clinicians, patients, appointments TO clinic_readonly;

-- PUBLIC can create temporary tables by default; the server never needs them.
REVOKE TEMPORARY ON DATABASE clinic FROM PUBLIC;

-- Function privileges live in each database's catalog, so the side-channel
-- revokes from seed/init.sql are repeated here.
REVOKE EXECUTE ON FUNCTION pg_catalog.pg_terminate_backend(integer, bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_catalog.pg_cancel_backend(integer) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_catalog.pg_notify(text, text) FROM PUBLIC;

DO $$
DECLARE f regprocedure;
BEGIN
    FOR f IN SELECT p.oid::regprocedure FROM pg_proc p
             WHERE p.proname = 'pg_logical_emit_message'
               AND p.pronamespace = 'pg_catalog'::regnamespace
    LOOP
        EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', f);
    END LOOP;
END
$$;

COMMIT;
