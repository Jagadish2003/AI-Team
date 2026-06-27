--
-- AgentIQ — consolidated provisioning script (schema + seed), head 0019.
--
-- Single self-contained replacement for the former 01_schema.sql / 02_seed.sql /
-- 03_lazy_runtime_tables.sql. Creates the agentiq role, all 27 tables (incl.
-- org_licenses, ingestion_checkpoints, opportunity_instances),
-- indexes/constraints/rules, seeds the core reference rows, and stamps
-- alembic_version to head 0019.
--
-- Run connected to the TARGET database (which must already exist), as a
-- superuser or the schema owner:
--   psql -h <DB_HOST> -p 5432 -U postgres -d <DB_NAME> -v ON_ERROR_STOP=1 -f provision.sql
--
-- Idempotent: CREATE ROLE is guarded; the seed and alembic stamp use ON CONFLICT
-- DO NOTHING. (Table creation is not re-runnable on its own — drop/recreate the
-- database for a clean rebuild.)
--
-- NOTE: the maintained provisioning path remains provision.sh / provision_schema.py
-- (alembic migrations + seed_loader). This pure-SQL file is the alternative for
-- environments without the Python toolchain; keep it in sync when migrations change.
--

--
-- PostgreSQL database dump
--


-- Dumped from database version 17.10
-- Dumped by pg_dump version 17.10

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET search_path TO "public";
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = "heap";

--
-- Application role (previously in 00_create_role_and_db.sql).
-- Run 01_schema.sql connected to the target database (e.g. agentiqdev) as a
-- superuser or the schema owner. The database itself must already exist.
-- CREATE ROLE has no IF NOT EXISTS, so the DO block makes it idempotent.
-- CHANGE THE PASSWORD below before running on any shared/production server.
--
DO
$$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentiq') THEN
        CREATE ROLE agentiq LOGIN PASSWORD 'agentiq';
    END IF;
END
$$;

-- Give the agentiq role ownership of (and full privilege on) the public schema
-- so it can create/own the objects below. Requires the current connection to be
-- a superuser or the existing schema owner.
ALTER SCHEMA public OWNER TO agentiq;
GRANT ALL ON SCHEMA public TO agentiq;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."alembic_version" (
    "version_num" character varying(32) NOT NULL
);


--
-- Name: audit_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."audit_events" (
    "id" "text" NOT NULL,
    "payload" "text" NOT NULL
);


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."audit_log" (
    "id" "text" NOT NULL,
    "org_id" "text" NOT NULL,
    "event_type" "text" NOT NULL,
    "user_id" "text",
    "run_id" "text",
    "connector_id" "text",
    "payload" "text",
    "timestamp" "text" NOT NULL
);


--
-- Name: causal_hypotheses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."causal_hypotheses" (
    "id" character varying(36) NOT NULL,
    "org_id" character varying(64) NOT NULL,
    "opportunity_id" character varying(64) NOT NULL,
    "run_id" character varying(64) NOT NULL,
    "cause_chain" "text" NOT NULL,
    "evidence_links" "text" NOT NULL,
    "temporal_support" "text",
    "confidence" double precision NOT NULL,
    "inferred" boolean NOT NULL,
    "falsifiability_condition" "text" NOT NULL,
    "preliminary" boolean NOT NULL,
    "preliminary_reason" "text",
    "gate_run_count" integer NOT NULL,
    "generated_by" character varying(32) NOT NULL,
    "created_at" timestamp without time zone NOT NULL
);


--
-- Name: connectors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."connectors" (
    "id" "text" NOT NULL,
    "payload" "text" NOT NULL
);


--
-- Name: credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."credentials" (
    "id" "text" NOT NULL,
    "org_id" "text" NOT NULL,
    "connector_id" "text" NOT NULL,
    "access_token" "text" NOT NULL,
    "refresh_token" "text",
    "expires_at" "text" NOT NULL,
    "scopes" "text" NOT NULL,
    "created_at" "text" NOT NULL,
    "updated_at" "text" NOT NULL,
    "refresh_failed" integer DEFAULT 0 NOT NULL,
    "is_deleted" boolean DEFAULT false NOT NULL
);


--
-- Name: entities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."entities" (
    "id" character varying(36) NOT NULL,
    "org_id" character varying(64) NOT NULL,
    "entity_type" character varying(32) NOT NULL,
    "canonical_name" character varying(256) NOT NULL,
    "display_name" character varying(256) NOT NULL,
    "source_system" character varying(64) NOT NULL,
    "source_record_id" character varying(256),
    "resolution_confidence" double precision NOT NULL,
    "resolution_status" character varying(32) NOT NULL,
    "first_seen_run_id" character varying(64) NOT NULL,
    "last_seen_run_id" character varying(64) NOT NULL,
    "run_count" integer NOT NULL,
    "metadata" "text",
    "created_at" timestamp without time zone NOT NULL,
    "updated_at" timestamp without time zone NOT NULL,
    CONSTRAINT "entities_entity_type_check" CHECK ((("entity_type")::"text" = ANY ((ARRAY['object'::character varying, 'person'::character varying, 'process'::character varying, 'project'::character varying, 'system'::character varying, 'team'::character varying])::"text"[]))),
    CONSTRAINT "entities_resolution_status_check" CHECK ((("resolution_status")::"text" = ANY ((ARRAY['ambiguous'::character varying, 'resolved'::character varying, 'unresolved'::character varying])::"text"[])))
);


--
-- Name: entity_relationships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."entity_relationships" (
    "id" character varying(36) NOT NULL,
    "org_id" character varying(64) NOT NULL,
    "from_entity_id" character varying(36) NOT NULL,
    "to_entity_id" character varying(36) NOT NULL,
    "relationship_type" character varying(32) NOT NULL,
    "confidence" double precision NOT NULL,
    "inferred" boolean NOT NULL,
    "evidence" "text",
    "first_seen_run_id" character varying(64) NOT NULL,
    "last_seen_run_id" character varying(64) NOT NULL,
    "run_count" integer NOT NULL,
    "created_at" timestamp without time zone NOT NULL,
    CONSTRAINT "entity_relationships_relationship_type_check" CHECK ((("relationship_type")::"text" = ANY ((ARRAY['depends_on'::character varying, 'escalates_to'::character varying, 'member_of'::character varying, 'owns'::character varying, 'routes_to'::character varying])::"text"[])))
);


--
-- Name: evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."evidence" (
    "id" "text" NOT NULL,
    "payload" "text" NOT NULL
);


--
-- Name: executive_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."executive_reports" (
    "id" "text" NOT NULL,
    "payload" "text" NOT NULL
);


--
-- Name: kv; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."kv" (
    "key" "text" NOT NULL,
    "payload" "text" NOT NULL
);


--
-- Name: login_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."login_attempts" (
    "id" character varying(36) NOT NULL,
    "email" character varying(256) NOT NULL,
    "ip_address" character varying(64) NOT NULL,
    "attempted_at" timestamp without time zone NOT NULL,
    "succeeded" boolean NOT NULL,
    "is_deleted" boolean DEFAULT false NOT NULL
);


--
-- Name: mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."mappings" (
    "id" "text" NOT NULL,
    "payload" "text" NOT NULL
);


--
-- Name: nonces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."nonces" (
    "key" "text" NOT NULL,
    "data" "text" NOT NULL,
    "is_deleted" boolean DEFAULT false NOT NULL
);


--
-- Name: oauth_nonces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."oauth_nonces" (
    "nonce" "text" NOT NULL,
    "connector_id" "text" NOT NULL,
    "expires_at" "text" NOT NULL,
    "is_deleted" boolean DEFAULT false NOT NULL
);


--
-- Name: opportunities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."opportunities" (
    "id" "text" NOT NULL,
    "payload" "text" NOT NULL
);


--
-- Name: orgs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."orgs" (
    "id" character varying(36) NOT NULL,
    "name" character varying(256) NOT NULL,
    "created_at" timestamp without time zone NOT NULL,
    "approval_status" character varying(32) DEFAULT 'pending_approval'::character varying NOT NULL,
    "approval_token_hash" character varying(256),
    "approval_token_expires_at" timestamp with time zone,
    "approved_at" timestamp with time zone,
    "approved_by_action" character varying(16)
);


--
-- Name: ingestion_checkpoints; Type: TABLE; Schema: public; Owner: -
--
-- SOURCE OF TRUTH: database/models/ingestion_checkpoints.py
-- (ALL_INGESTION_CHECKPOINTS_DDL), applied by migration 0017/0018 and the runtime
-- repository. This pure-SQL provisioning path mirrors that schema and MUST be kept
-- in sync with it — if you change the table there (columns, types, defaults, PK,
-- is_deleted), make the identical change here. Keep the two definitions equivalent.
--

CREATE TABLE "public"."ingestion_checkpoints" (
    "org_id" character varying(64) NOT NULL,
    "connector_id" character varying(64) NOT NULL,
    "value" "text" NOT NULL,
    "captured_at" timestamp with time zone NOT NULL,
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL,
    "is_deleted" boolean DEFAULT false NOT NULL,
    CONSTRAINT "ingestion_checkpoints_pkey" PRIMARY KEY ("org_id", "connector_id")
);


--
-- Name: org_licenses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."org_licenses" (
    "org_id" character varying(36) NOT NULL,
    "license_key" "text" NOT NULL,
    "last_seen_date" character varying(32),
    "last_status" character varying(32),
    "updated_at" timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT "org_licenses_pkey" PRIMARY KEY ("org_id")
);


--
-- Name: opportunity_instances; Type: TABLE; Schema: public; Owner: -
--
-- SOURCE OF TRUTH: database/models/opportunity_instances.py
-- (ALL_OPPORTUNITY_INSTANCES_DDL), applied by migration 0019 and the runtime
-- ensure_opportunity_instances_table() helper. This pure-SQL provisioning path
-- mirrors that schema and MUST be kept in sync with it — if you change the table
-- there (columns, types, defaults, PK, indexes), make the identical change here.
--

CREATE TABLE "public"."opportunity_instances" (
    "opportunity_identity" character varying(64) NOT NULL,
    "run_id" character varying(64) NOT NULL,
    "org_id" character varying(64) NOT NULL,
    "pack_id" character varying(64) NOT NULL,
    "pack_version" character varying(32) NOT NULL,
    "detector_id" character varying(128) NOT NULL,
    "signal_source" character varying(128),
    "opportunity_ref" character varying(64),
    "impact" integer,
    "effort" integer,
    "score" double precision,
    "confidence" character varying(16),
    "tier" character varying(32),
    "evidence_ids" "text",
    "evidence_count" integer DEFAULT 0 NOT NULL,
    "narrative" "text",
    "metadata" "text",
    "created_at" timestamp without time zone NOT NULL,
    "is_deleted" boolean DEFAULT false NOT NULL,
    CONSTRAINT "opportunity_instances_pkey" PRIMARY KEY ("opportunity_identity", "run_id")
);


--
-- Name: permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."permissions" (
    "id" "text" NOT NULL,
    "payload" "text" NOT NULL
);


--
-- Name: run_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."run_events" (
    "run_id" "text" NOT NULL,
    "seq" integer NOT NULL,
    "payload" "text" NOT NULL,
    "is_deleted" boolean DEFAULT false NOT NULL
);


--
-- Name: runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."runs" (
    "id" "text" NOT NULL,
    "payload" "text" NOT NULL,
    "current_step" character varying,
    "seq" bigint NOT NULL
);


--
-- Name: runs_seq_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE "public"."runs_seq_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: runs_seq_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE "public"."runs_seq_seq" OWNED BY "public"."runs"."seq";


--
-- Name: signal_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."signal_snapshots" (
    "id" character varying(36) NOT NULL,
    "org_id" character varying(64) NOT NULL,
    "run_id" character varying(64) NOT NULL,
    "pack_id" character varying(64) NOT NULL,
    "detector_id" character varying(128) NOT NULL,
    "signal_key" character varying(256) NOT NULL,
    "metric_name" character varying(128) NOT NULL,
    "metric_value" double precision NOT NULL,
    "threshold" double precision,
    "fired" boolean NOT NULL,
    "signal_source" character varying(64) NOT NULL,
    "captured_at" timestamp without time zone NOT NULL,
    "baseline_mean" double precision,
    "baseline_stddev" double precision,
    "baseline_window_days" integer,
    "baseline_calculated_at" timestamp without time zone
);


--
-- Name: telemetry_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."telemetry_events" (
    "id" "text" NOT NULL,
    "org_id" "text" NOT NULL,
    "event_type" "text" NOT NULL,
    "source" "text" NOT NULL,
    "run_id" "text",
    "connector_id" "text",
    "pack_id" "text",
    "duration_ms" integer,
    "success" integer,
    "count" integer,
    "error_code" "text",
    "payload" "text" DEFAULT '{}'::"text" NOT NULL,
    "timestamp" "text" NOT NULL
);


--
-- Name: uploads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."uploads" (
    "id" "text" NOT NULL,
    "payload" "text" NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."users" (
    "id" character varying(36) NOT NULL,
    "email" character varying(256) NOT NULL,
    "password_hash" character varying(256) NOT NULL,
    "is_active" boolean NOT NULL,
    "invite_token_hash" character varying(256),
    "invite_token_expires_at" timestamp without time zone,
    "created_at" timestamp without time zone NOT NULL,
    "last_login_at" timestamp without time zone,
    "reset_token_hash" character varying(256),
    "reset_token_expires_at" timestamp without time zone
);


--
-- Name: workspace_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."workspace_members" (
    "org_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "role" "text" NOT NULL,
    "created_at" "text" NOT NULL,
    "is_deleted" boolean DEFAULT false NOT NULL,
    CONSTRAINT "workspace_members_role_check" CHECK (("role" = ANY (ARRAY['owner'::"text", 'analyst'::"text", 'viewer'::"text"])))
);


--
-- Name: runs seq; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."runs" ALTER COLUMN "seq" SET DEFAULT "nextval"('"public"."runs_seq_seq"'::"regclass");


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."alembic_version"
    ADD CONSTRAINT "alembic_version_pkc" PRIMARY KEY ("version_num");


--
-- Name: audit_events audit_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."audit_events"
    ADD CONSTRAINT "audit_events_pkey" PRIMARY KEY ("id");


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."audit_log"
    ADD CONSTRAINT "audit_log_pkey" PRIMARY KEY ("id");


--
-- Name: causal_hypotheses causal_hypotheses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."causal_hypotheses"
    ADD CONSTRAINT "causal_hypotheses_pkey" PRIMARY KEY ("id");


--
-- Name: connectors connectors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."connectors"
    ADD CONSTRAINT "connectors_pkey" PRIMARY KEY ("id");


--
-- Name: credentials credentials_org_id_connector_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."credentials"
    ADD CONSTRAINT "credentials_org_id_connector_id_key" UNIQUE ("org_id", "connector_id");


--
-- Name: credentials credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."credentials"
    ADD CONSTRAINT "credentials_pkey" PRIMARY KEY ("id");


--
-- Name: entities entities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."entities"
    ADD CONSTRAINT "entities_pkey" PRIMARY KEY ("id");


--
-- Name: entity_relationships entity_relationships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."entity_relationships"
    ADD CONSTRAINT "entity_relationships_pkey" PRIMARY KEY ("id");


--
-- Name: evidence evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."evidence"
    ADD CONSTRAINT "evidence_pkey" PRIMARY KEY ("id");


--
-- Name: executive_reports executive_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."executive_reports"
    ADD CONSTRAINT "executive_reports_pkey" PRIMARY KEY ("id");


--
-- Name: kv kv_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."kv"
    ADD CONSTRAINT "kv_pkey" PRIMARY KEY ("key");


--
-- Name: login_attempts login_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."login_attempts"
    ADD CONSTRAINT "login_attempts_pkey" PRIMARY KEY ("id");


--
-- Name: mappings mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."mappings"
    ADD CONSTRAINT "mappings_pkey" PRIMARY KEY ("id");


--
-- Name: nonces nonces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."nonces"
    ADD CONSTRAINT "nonces_pkey" PRIMARY KEY ("key");


--
-- Name: oauth_nonces oauth_nonces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."oauth_nonces"
    ADD CONSTRAINT "oauth_nonces_pkey" PRIMARY KEY ("nonce");


--
-- Name: opportunities opportunities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."opportunities"
    ADD CONSTRAINT "opportunities_pkey" PRIMARY KEY ("id");


--
-- Name: orgs orgs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."orgs"
    ADD CONSTRAINT "orgs_pkey" PRIMARY KEY ("id");


--
-- Name: permissions permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."permissions"
    ADD CONSTRAINT "permissions_pkey" PRIMARY KEY ("id");


--
-- Name: run_events run_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."run_events"
    ADD CONSTRAINT "run_events_pkey" PRIMARY KEY ("run_id", "seq");


--
-- Name: runs runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."runs"
    ADD CONSTRAINT "runs_pkey" PRIMARY KEY ("id");


--
-- Name: signal_snapshots signal_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."signal_snapshots"
    ADD CONSTRAINT "signal_snapshots_pkey" PRIMARY KEY ("id");


--
-- Name: telemetry_events telemetry_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."telemetry_events"
    ADD CONSTRAINT "telemetry_events_pkey" PRIMARY KEY ("id");


--
-- Name: uploads uploads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."uploads"
    ADD CONSTRAINT "uploads_pkey" PRIMARY KEY ("id");


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_pkey" PRIMARY KEY ("id");


--
-- Name: workspace_members workspace_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."workspace_members"
    ADD CONSTRAINT "workspace_members_pkey" PRIMARY KEY ("org_id", "user_id");


--
-- Name: idx_audit_org_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_audit_org_event" ON "public"."audit_log" USING "btree" ("org_id", "event_type");


--
-- Name: idx_audit_org_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_audit_org_ts" ON "public"."audit_log" USING "btree" ("org_id", "timestamp");


--
-- Name: idx_ch_org_opp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ch_org_opp" ON "public"."causal_hypotheses" USING "btree" ("org_id", "opportunity_id");


--
-- Name: idx_ch_org_preliminary; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ch_org_preliminary" ON "public"."causal_hypotheses" USING "btree" ("org_id", "preliminary") WHERE "preliminary";


--
-- Name: idx_credentials_connector_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_credentials_connector_id" ON "public"."credentials" USING "btree" ("connector_id");


--
-- Name: idx_credentials_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_credentials_org_id" ON "public"."credentials" USING "btree" ("org_id");


--
-- Name: idx_entities_org_canonical; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_entities_org_canonical" ON "public"."entities" USING "btree" ("org_id", "entity_type", "canonical_name");


--
-- Name: idx_entities_org_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_entities_org_run" ON "public"."entities" USING "btree" ("org_id", "last_seen_run_id");


--
-- Name: idx_entities_org_run_count; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_entities_org_run_count" ON "public"."entities" USING "btree" ("org_id", "run_count");


--
-- Name: idx_er_org_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_er_org_from" ON "public"."entity_relationships" USING "btree" ("org_id", "from_entity_id");


--
-- Name: idx_er_org_natural_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "idx_er_org_natural_key" ON "public"."entity_relationships" USING "btree" ("org_id", "from_entity_id", "to_entity_id", "relationship_type");


--
-- Name: idx_er_org_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_er_org_to" ON "public"."entity_relationships" USING "btree" ("org_id", "to_entity_id");


--
-- Name: idx_er_org_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_er_org_type" ON "public"."entity_relationships" USING "btree" ("org_id", "relationship_type", "inferred");


--
-- Name: idx_login_attempts_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_login_attempts_email" ON "public"."login_attempts" USING "btree" ("email", "attempted_at");


--
-- Name: idx_login_attempts_ip; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_login_attempts_ip" ON "public"."login_attempts" USING "btree" ("ip_address", "attempted_at");


--
-- Name: idx_opp_instances_identity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_opp_instances_identity" ON "public"."opportunity_instances" USING "btree" ("opportunity_identity", "is_deleted");


--
-- Name: idx_opp_instances_org_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_opp_instances_org_run" ON "public"."opportunity_instances" USING "btree" ("org_id", "run_id");


--
-- Name: idx_ss_baseline_stale; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ss_baseline_stale" ON "public"."signal_snapshots" USING "btree" ("baseline_calculated_at");


--
-- Name: idx_ss_org_detector; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ss_org_detector" ON "public"."signal_snapshots" USING "btree" ("org_id", "detector_id", "captured_at" DESC);


--
-- Name: idx_ss_org_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ss_org_run" ON "public"."signal_snapshots" USING "btree" ("org_id", "run_id");


--
-- Name: idx_ss_org_signal_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_ss_org_signal_time" ON "public"."signal_snapshots" USING "btree" ("org_id", "signal_key", "captured_at" DESC);


--
-- Name: idx_telemetry_org_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_telemetry_org_event" ON "public"."telemetry_events" USING "btree" ("org_id", "event_type");


--
-- Name: idx_telemetry_org_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_telemetry_org_run" ON "public"."telemetry_events" USING "btree" ("org_id", "run_id");


--
-- Name: idx_telemetry_org_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_telemetry_org_ts" ON "public"."telemetry_events" USING "btree" ("org_id", "timestamp");


--
-- Name: idx_users_email_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "idx_users_email_unique" ON "public"."users" USING "btree" ("email");


--
-- Name: telemetry_events trg_telemetry_no_delete; Type: RULE; Schema: public; Owner: -
--

CREATE RULE "trg_telemetry_no_delete" AS
    ON DELETE TO "public"."telemetry_events" DO INSTEAD NOTHING;


--
-- Name: telemetry_events trg_telemetry_no_update; Type: RULE; Schema: public; Owner: -
--

CREATE RULE "trg_telemetry_no_update" AS
    ON UPDATE TO "public"."telemetry_events" DO INSTEAD NOTHING;


--
-- PostgreSQL database dump complete
--


--
-- Seed: core reference rows (connectors, mappings, permissions, uploads).
-- Idempotent via ON CONFLICT DO NOTHING. Sourced from the provisioned schema.
--

-- connectors (26 rows)
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('azure_devops', '{"id": "azure_devops", "name": "Azure DevOps", "category": "ALM / CI/CD", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Work Items", "Pipelines", "Repos"], "signalStrength": 62}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('azure_repos', '{"id": "azure_repos", "name": "Azure Repos", "category": "Source control", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Commits", "Pull Requests", "Branches"], "signalStrength": 55}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('bitbucket', '{"id": "bitbucket", "name": "Bitbucket", "category": "Source control", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Pull Requests", "Repos", "Pipelines"], "signalStrength": 55}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('confluence', '{"id": "confluence", "name": "Confluence", "category": "Docs / knowledge", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Pages", "Spaces", "Templates"], "signalStrength": 58}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('databricks', '{"id": "databricks", "name": "Databricks", "category": "Data \u00b7 Platform", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [{"label": "Job Runs", "value": "184"}, {"label": "Pipelines", "value": "27"}], "lastSynced": "\u2014", "reads": ["Job Runs", "Pipelines", "Notebooks"], "signalStrength": 52}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('dbt', '{"id": "dbt", "name": "dbt", "category": "Transforms", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Models", "Tests", "Lineage"], "signalStrength": 42}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('dynamics365', '{"id": "dynamics365", "name": "Dynamics 365", "category": "ERP / CRM", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Accounts", "Opportunities", "Work Orders"], "signalStrength": 65}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('github', '{"id": "github", "name": "GitHub", "category": "Engineering \u00b7 Delivery", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [{"label": "Pull Requests", "value": "248"}, {"label": "Review Comments", "value": "913"}], "lastSynced": "\u2014", "reads": ["Pull Requests", "Review Comments", "Commits"], "signalStrength": 81}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('gitlab', '{"id": "gitlab", "name": "GitLab", "category": "DevOps", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Merge Requests", "Pipelines", "Issues"], "signalStrength": 60}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('jira', '{"id": "jira", "name": "Jira", "category": "Issues / backlog", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Issues", "Sprints", "Epics"], "signalStrength": 78}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('linear', '{"id": "linear", "name": "Linear", "category": "Product / issues", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Issues", "Projects", "Cycles"], "signalStrength": 55}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('m365', '{"id": "m365", "name": "Microsoft 365", "category": "Comms / docs", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Emails", "Calendar", "Documents"], "signalStrength": 50}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('notion', '{"id": "notion", "name": "Notion", "category": "Docs / wiki", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Pages", "Databases", "Blocks"], "signalStrength": 45}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('oracle_db', '{"id": "oracle_db", "name": "Oracle DB", "category": "Database", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Tables", "Procedures", "AWR Reports"], "signalStrength": 48}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('oracle_ebs', '{"id": "oracle_ebs", "name": "Oracle EBS", "category": "Finance / HR", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["GL Journals", "AP Invoices", "Cost Centers"], "signalStrength": 72}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('postgresql', '{"id": "postgresql", "name": "PostgreSQL", "category": "Database", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Tables", "Views", "Query Logs"], "signalStrength": 48}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('salesforce', '{"id": "salesforce", "name": "Salesforce", "category": "CRM \u00b7 PSS Benefits Administration", "tier": "recommended", "recommendedRank": 1, "status": "disconnected", "configured": false, "metrics": [{"label": "Applications", "value": "3"}, {"label": "Benefit Assignments", "value": "2"}], "lastSynced": "\u2014", "reads": ["IndividualApplication", "BenefitAssignment", "Case (Disability)"], "signalStrength": 94, "products": []}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('sap', '{"id": "sap", "name": "SAP", "category": "ERP \u00b7 Process", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [{"label": "Change Documents", "value": "1420"}, {"label": "Approvals", "value": "312"}], "lastSynced": "\u2014", "reads": ["Change Documents", "Approvals", "Transports (if available)"], "signalStrength": 76}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('servicenow', '{"id": "servicenow", "name": "ServiceNow", "category": "Operations \u00b7 Incidents", "tier": "recommended", "recommendedRank": 2, "status": "disconnected", "configured": false, "metrics": [{"label": "Incidents (90d)", "value": "5"}, {"label": "Benefit signals", "value": "5"}], "lastSynced": "\u2014", "reads": ["Incident Tickets", "Benefit Operations", "SLA Definitions"], "signalStrength": 88}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('sharepoint', '{"id": "sharepoint", "name": "SharePoint", "category": "Docs / intranet", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Documents", "Lists", "Sites"], "signalStrength": 52}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('slack', '{"id": "slack", "name": "Slack", "category": "Comms \u00b7 Ops", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [{"label": "Channels", "value": "34"}, {"label": "Messages (7d)", "value": "12840"}], "lastSynced": "\u2014", "reads": ["Channels", "Threads", "Mentions"], "signalStrength": 79}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('snowflake', '{"id": "snowflake", "name": "Snowflake", "category": "Data warehouse", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Schemas", "Query History", "Warehouses"], "signalStrength": 50}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('sql_server', '{"id": "sql_server", "name": "SQL Server", "category": "Database", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Tables", "Stored Procedures", "Query Plans"], "signalStrength": 48}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('teams', '{"id": "teams", "name": "Microsoft Teams", "category": "Comms / docs", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Channels", "Messages", "Meetings"], "signalStrength": 50}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('workday', '{"id": "workday", "name": "Workday", "category": "HR / finance", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Workers", "Business Processes", "Compensation"], "signalStrength": 68}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('zendesk', '{"id": "zendesk", "name": "Zendesk", "category": "Support", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Tickets", "Agents", "SLA Policies"], "signalStrength": 60}') ON CONFLICT ("id") DO NOTHING;

-- mappings (17 rows)
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map1', '{"id": "map1", "sourceSystem": "ServiceNow", "sourceField": "incident.number", "sourceType": "string", "commonField": "ticket.id", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map10', '{"id": "map10", "sourceSystem": "Jira", "sourceField": "field_10", "sourceType": "string", "commonField": "common_10", "status": "AMBIGUOUS", "confidence": "MEDIUM", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map11', '{"id": "map11", "sourceSystem": "Jira", "sourceField": "field_11", "sourceType": "string", "commonField": "common_11", "status": "UNMAPPED", "confidence": "LOW", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map12', '{"id": "map12", "sourceSystem": "ServiceNow", "sourceField": "field_12", "sourceType": "string", "commonField": "common_12", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map13', '{"id": "map13", "sourceSystem": "Jira", "sourceField": "field_13", "sourceType": "string", "commonField": "common_13", "status": "AMBIGUOUS", "confidence": "MEDIUM", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map14', '{"id": "map14", "sourceSystem": "Jira", "sourceField": "field_14", "sourceType": "string", "commonField": "common_14", "status": "UNMAPPED", "confidence": "LOW", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map15', '{"id": "map15", "sourceSystem": "ServiceNow", "sourceField": "field_15", "sourceType": "string", "commonField": "common_15", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map16', '{"id": "map16", "sourceSystem": "Jira", "sourceField": "field_16", "sourceType": "string", "commonField": "common_16", "status": "AMBIGUOUS", "confidence": "MEDIUM", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map17', '{"id": "map17", "sourceSystem": "Jira", "sourceField": "field_17", "sourceType": "string", "commonField": "common_17", "status": "UNMAPPED", "confidence": "LOW", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map2', '{"id": "map2", "sourceSystem": "ServiceNow", "sourceField": "incident.assigned_to", "sourceType": "reference", "commonField": "ticket.owner", "status": "MAPPED", "confidence": "MEDIUM", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map3', '{"id": "map3", "sourceSystem": "Jira", "sourceField": "issue.status", "sourceType": "string", "commonField": "ticket.status", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map4', '{"id": "map4", "sourceSystem": "Jira", "sourceField": "issue.approvals", "sourceType": "array", "commonField": "ticket.approvals", "status": "AMBIGUOUS", "confidence": "MEDIUM", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map5', '{"id": "map5", "sourceSystem": "Databricks", "sourceField": "job_run.state", "sourceType": "string", "commonField": "pipeline.runStatus", "status": "UNMAPPED", "confidence": "LOW", "commonEntity": "Pipeline"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map6', '{"id": "map6", "sourceSystem": "ServiceNow", "sourceField": "field_6", "sourceType": "string", "commonField": "common_6", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map7', '{"id": "map7", "sourceSystem": "Jira", "sourceField": "field_7", "sourceType": "string", "commonField": "common_7", "status": "AMBIGUOUS", "confidence": "MEDIUM", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map8', '{"id": "map8", "sourceSystem": "Jira", "sourceField": "field_8", "sourceType": "string", "commonField": "common_8", "status": "UNMAPPED", "confidence": "LOW", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."mappings" ("id", "payload") VALUES ('map9', '{"id": "map9", "sourceSystem": "ServiceNow", "sourceField": "field_9", "sourceType": "string", "commonField": "common_9", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}') ON CONFLICT ("id") DO NOTHING;

-- permissions (5 rows)
INSERT INTO "public"."permissions" ("id", "payload") VALUES ('p_conf_pages', '{"id": "p_conf_pages", "label": "Confluence: read pages", "sourceSystem": "Jira", "satisfied": false, "required": false}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."permissions" ("id", "payload") VALUES ('p_jira_iss', '{"id": "p_jira_iss", "label": "Jira: read issues", "sourceSystem": "Jira", "satisfied": true, "required": true}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."permissions" ("id", "payload") VALUES ('p_m365_teams', '{"id": "p_m365_teams", "label": "Microsoft 365: read Teams channel metadata", "sourceSystem": "Microsoft 365", "satisfied": false, "required": true}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."permissions" ("id", "payload") VALUES ('p_sn_cmdb', '{"id": "p_sn_cmdb", "label": "ServiceNow: read CMDB", "sourceSystem": "ServiceNow", "satisfied": true, "required": true}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."permissions" ("id", "payload") VALUES ('p_sn_inc', '{"id": "p_sn_inc", "label": "ServiceNow: read incidents", "sourceSystem": "ServiceNow", "satisfied": true, "required": true}') ON CONFLICT ("id") DO NOTHING;

-- uploads (0 rows)

INSERT INTO "public"."alembic_version" ("version_num") VALUES ('0019') ON CONFLICT DO NOTHING;
