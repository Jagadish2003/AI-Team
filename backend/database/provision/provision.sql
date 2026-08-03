--
-- AgentIQ — consolidated provisioning script (schema + seed), head 0033.
--
-- Single self-contained replacement for the former 01_schema.sql / 02_seed.sql /
-- 03_lazy_runtime_tables.sql. Creates the agentiq role, all tables (incl.
-- org_licenses, ingestion_checkpoints, opportunity_instances, the R18-B1/B2
-- pgvector-backed retrieval_chunks + retrieval_refresh_queue, the MSP-B8
-- ops_event_staging + ops_event_load_batches, the MSP-B5 runbook_matches /
-- runbook_match_decision_history / runbook_match_feedback, the 2.0-C1
-- pack_states + append-only pack_state_history, the 2.0-C2 append-only
-- pack_certification_reviews + pack_certification_policies, and the R-1.9.1-L3
-- vendor-side license_registry + append-only issuance_audit),
-- indexes/constraints/rules, seeds the connector catalog, grants the app login
-- role(s) privileges on the schema, REVOKES DELETE/TRUNCATE on the run-history
-- tables (2.0-C1 AC4), and stamps alembic_version to head 0035.
--
-- BEFORE RUNNING ON PRODUCTION — two values in this file are dev defaults and
-- MUST be set for the target environment. Both are marked "TODO(deploy)" below:
--   1. the agentiq role password (search: TODO(deploy) role password), and
--   2. the app login role(s) in the grants block (search: TODO(deploy) app roles) —
--      the role in the app's DATABASE_URL must appear there or the application
--      hits "permission denied for table ..." on its first write.
--
-- SEED POLICY (production): this file seeds ONLY the connector catalog, which the
-- Integration Hub reads to render its tiles — without it no system can be
-- connected. The dev seed's demo `mappings` and `permissions` rows are
-- deliberately NOT seeded here: nothing in the application ever writes those two
-- tables, so seeding them would display synthetic field mappings / permission
-- checks as if they were the customer's own. Consequence: those views start
-- empty. Per-connector `metrics` are likewise left empty — they are per-org,
-- run-derived values (app/connector_metrics.py writes them to the ORG overlay
-- after a real run), so a shared-catalog metric would show fabricated numbers
-- until the first run completes.
--
-- Requires the pgvector extension for the retrieval_chunks vector column
-- (CREATE EXTENSION below); the provisioning connection must be permitted to
-- create it — on managed PostgreSQL this is typically a one-time grant.
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
--
-- TODO(deploy) role password: the literal below is a DEV default and is committed
-- to source control — treat it as public. Replace it with the environment's real
-- secret before running anywhere shared or production. If the role already exists
-- in the target database this block is a no-op and the password is untouched.
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
    "is_deleted" boolean DEFAULT false NOT NULL,
    "kind" "text" DEFAULT 'oauth' NOT NULL,
    "enc_username" "text",
    "enc_secret" "text",
    "base_url" "text"
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
    CONSTRAINT "entity_relationships_relationship_type_check" CHECK ((("relationship_type")::"text" = ANY ((ARRAY['connects_to'::character varying, 'depends_on'::character varying, 'escalates_to'::character varying, 'member_of'::character varying, 'owns'::character varying, 'routes_to'::character varying, 'runs_on'::character varying, 'used_by'::character varying])::"text"[])))
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
    "approved_by_action" character varying(16),
    "domain" character varying(255),
    "name_normalised" character varying(256) DEFAULT ''::character varying NOT NULL
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
-- Name: org_join_requests; Type: TABLE; Schema: public; Owner: -
--
-- Synced from the live dev database (org join-request approval flow: a user's
-- request to join an org, decided by an owner). No migration or model in this
-- repo owns this table yet — keep this definition in sync with the live schema
-- until one takes ownership.
--

CREATE TABLE "public"."org_join_requests" (
    "id" character varying(36) NOT NULL,
    "org_id" character varying(36) NOT NULL,
    "user_id" character varying(36) NOT NULL,
    "email" character varying(256) NOT NULL,
    "status" character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    "role" character varying(16),
    "requested_at" timestamp without time zone NOT NULL,
    "decided_at" timestamp without time zone,
    "decided_by" character varying(36),
    CONSTRAINT "org_join_requests_pkey" PRIMARY KEY ("id")
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
-- Name: license_registry; Type: TABLE; Schema: public; Owner: -
-- R-1.9.1-L3 (AT-715 / T1): CloudFulcrum vendor-side license issuance registry.
--

CREATE TABLE "public"."license_registry" (
    "license_id" "text" NOT NULL,
    "customer" "text" NOT NULL,
    "org_id" "text" NOT NULL,
    "contract_ref" "text" NOT NULL,
    "deployment_type" "text" NOT NULL,
    "max_systems" integer,
    "expires_at" "date" NOT NULL,
    "grace_days" integer DEFAULT 14 NOT NULL,
    "kid" "text" NOT NULL,
    "issued_by" "text" NOT NULL,
    "issued_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "supersedes" "text",
    "notes" "text",
    "deployment_fee_collected" boolean DEFAULT false NOT NULL,
    "deployment_fee_collected_at" timestamp with time zone,
    "payload_version" integer DEFAULT 2 NOT NULL,
    "license_key" "text" NOT NULL,
    CONSTRAINT "license_registry_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'superseded'::"text", 'revoked_at_next_rotation'::"text"])))
);


--
-- Name: issuance_audit; Type: TABLE; Schema: public; Owner: -
-- R-1.9.1-L3 (AT-717 / T3): append-only issuance ledger (see the rewrite rules
-- issuance_audit_no_update / issuance_audit_no_delete below).
--

CREATE TABLE "public"."issuance_audit" (
    "audit_id" "text" NOT NULL,
    "license_id" "text" NOT NULL,
    "action" "text" NOT NULL,
    "actor" "text" NOT NULL,
    "occurred_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "customer" "text",
    "org_id" "text",
    "contract_ref" "text",
    "kid" "text",
    "deployment_type" "text",
    "terms" "text",
    "supersedes" "text",
    "notes" "text",
    CONSTRAINT "issuance_audit_action_check" CHECK (("action" = ANY (ARRAY['issue'::"text", 'renew'::"text", 'regenerate'::"text"])))
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
    "reset_token_expires_at" timestamp without time zone,
    "org_id" character varying(36)
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
-- Name: users fk_users_org_id; Type: FK CONSTRAINT; Schema: public; Owner: -
-- Denormalized org pointer (migration 0021). workspace_members stays authoritative.
--

ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "fk_users_org_id" FOREIGN KEY ("org_id") REFERENCES "public"."orgs" ("id");


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
-- Name: license_registry license_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."license_registry"
    ADD CONSTRAINT "license_registry_pkey" PRIMARY KEY ("license_id");


--
-- Name: issuance_audit issuance_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."issuance_audit"
    ADD CONSTRAINT "issuance_audit_pkey" PRIMARY KEY ("audit_id");


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
-- Name: idx_join_requests_org_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_join_requests_org_status" ON "public"."org_join_requests" USING "btree" ("org_id", "status");


--
-- Name: idx_join_requests_pending_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "idx_join_requests_pending_unique" ON "public"."org_join_requests" USING "btree" ("org_id", "user_id") WHERE (("status")::"text" = 'pending'::"text");


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
-- Name: idx_orgs_domain_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "idx_orgs_domain_unique" ON "public"."orgs" USING "btree" ("domain") WHERE ("domain" IS NOT NULL);


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
-- Name: idx_license_registry_customer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_license_registry_customer" ON "public"."license_registry" USING "btree" ("customer");


--
-- Name: idx_license_registry_org; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_license_registry_org" ON "public"."license_registry" USING "btree" ("org_id");


--
-- Name: idx_license_registry_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_license_registry_expiry" ON "public"."license_registry" USING "btree" ("status", "expires_at");


--
-- Name: idx_license_registry_supersedes; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_license_registry_supersedes" ON "public"."license_registry" USING "btree" ("supersedes");


--
-- Name: idx_issuance_audit_license; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_issuance_audit_license" ON "public"."issuance_audit" USING "btree" ("license_id");


--
-- Name: idx_users_email_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "idx_users_email_unique" ON "public"."users" USING "btree" ("email");


--
-- Name: idx_orgs_name_normalised_unique; Type: INDEX; Schema: public; Owner: -
-- Org-name deduplication (migration 0020): one org_id per normalised name.
--

CREATE UNIQUE INDEX "idx_orgs_name_normalised_unique" ON "public"."orgs" USING "btree" ("name_normalised");


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
-- Name: issuance_audit issuance_audit_no_delete; Type: RULE; Schema: public; Owner: -
-- R-1.9.1-L3 (AC2): the issuance audit ledger is append-only.
--

CREATE RULE "issuance_audit_no_delete" AS
    ON DELETE TO "public"."issuance_audit" DO INSTEAD NOTHING;


--
-- Name: issuance_audit issuance_audit_no_update; Type: RULE; Schema: public; Owner: -
--

CREATE RULE "issuance_audit_no_update" AS
    ON UPDATE TO "public"."issuance_audit" DO INSTEAD NOTHING;


--
-- R18-B1 / R18-B2 — retrieval substrate + freshness (migrations 0024, 0025).
--
-- SOURCE OF TRUTH: database/models/retrieval.py (ALL_RETRIEVAL_DDL) and
-- database/models/retrieval_freshness.py (ALL_FRESHNESS_DDL), applied by
-- migrations 0024/0025 and the runtime ensure_retrieval_store() /
-- ensure_freshness_schema() helpers. This pure-SQL provisioning path mirrors those
-- and MUST be kept in sync with them — if you change the schema there (columns,
-- types, defaults, indexes, CHECK values), make the identical change here. The
-- retrieval_chunks table below is the FINAL state after BOTH migrations: it
-- includes the R18-B2 freshness columns (is_stale / stale_at) that 0025 adds.
--
-- The embedding column uses the pgvector "vector" type; the extension must exist
-- before the table is created. IF NOT EXISTS makes the extension create idempotent.
--

CREATE EXTENSION IF NOT EXISTS "vector" WITH SCHEMA "public";


--
-- Name: retrieval_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."retrieval_chunks" (
    "chunk_id" character varying(36) NOT NULL,
    "org_id" character varying(64) NOT NULL,
    "content" "text" NOT NULL,
    "content_hash" character varying(64) NOT NULL,
    "content_type" character varying(32) NOT NULL,
    "source_system" character varying(64) NOT NULL,
    "source_artifact" character varying(1024) NOT NULL,
    "source_timestamp" timestamp without time zone,
    "chunk_position" integer DEFAULT 0 NOT NULL,
    "embedding" "public"."vector",
    "embedding_model" character varying(128),
    "embedding_model_version" character varying(64),
    "embedded_at" timestamp without time zone,
    "provenance" "text",
    "created_at" timestamp without time zone NOT NULL,
    "updated_at" timestamp without time zone NOT NULL,
    "is_stale" boolean DEFAULT false NOT NULL,
    "stale_at" timestamp without time zone,
    CONSTRAINT "retrieval_chunks_content_type_check" CHECK ((("content_type")::"text" = ANY ((ARRAY['code'::character varying, 'conversation'::character varying, 'prose'::character varying])::"text"[])))
);


--
-- Name: retrieval_refresh_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."retrieval_refresh_queue" (
    "id" character varying(36) NOT NULL,
    "org_id" character varying(64) NOT NULL,
    "source_system" character varying(64) NOT NULL,
    "source_artifact" character varying(512) NOT NULL,
    "change_kind" character varying(16) NOT NULL,
    "status" character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    "attempts" integer DEFAULT 0 NOT NULL,
    "enqueued_at" timestamp without time zone NOT NULL,
    "updated_at" timestamp without time zone NOT NULL,
    "last_error" "text",
    CONSTRAINT "retrieval_refresh_queue_status_check" CHECK ((("status")::"text" = ANY ((ARRAY['done'::character varying, 'failed'::character varying, 'in_progress'::character varying, 'pending'::character varying])::"text"[])))
);


--
-- Name: retrieval_chunks retrieval_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."retrieval_chunks"
    ADD CONSTRAINT "retrieval_chunks_pkey" PRIMARY KEY ("chunk_id");


--
-- Name: retrieval_refresh_queue retrieval_refresh_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY "public"."retrieval_refresh_queue"
    ADD CONSTRAINT "retrieval_refresh_queue_pkey" PRIMARY KEY ("id");


--
-- Name: idx_retrieval_chunks_org_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_retrieval_chunks_org_source" ON "public"."retrieval_chunks" USING "btree" ("org_id", "source_system");


--
-- Name: idx_retrieval_chunks_org_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_retrieval_chunks_org_hash" ON "public"."retrieval_chunks" USING "btree" ("org_id", "content_hash");


--
-- Name: idx_retrieval_chunks_org_artifact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_retrieval_chunks_org_artifact" ON "public"."retrieval_chunks" USING "btree" ("org_id", "source_artifact");


--
-- Name: idx_retrieval_chunks_org_stale; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_retrieval_chunks_org_stale" ON "public"."retrieval_chunks" USING "btree" ("org_id", "is_stale");


--
-- Name: uq_refresh_queue_org_artifact; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX "uq_refresh_queue_org_artifact" ON "public"."retrieval_refresh_queue" USING "btree" ("org_id", "source_system", "source_artifact");


--
-- Name: idx_refresh_queue_org_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX "idx_refresh_queue_org_status" ON "public"."retrieval_refresh_queue" USING "btree" ("org_id", "status");


--
-- MSP-B8 — Event-History Bridge staging schema (migrations 0027, 0028).
--
-- SOURCE OF TRUTH: database/models/ops_event_staging.py
-- (ALL_OPS_EVENT_STAGING_DDL, staging schema contract v1.1.0), applied by
-- migrations 0027 (create) and 0028 (add event_time). The statements below are
-- copied VERBATIM from that module — the same text alembic executes — so the two
-- paths cannot diverge; keep them identical when the model changes.
--
-- Applies when AgentIQ's own PostgreSQL hosts the staging store. A
-- partner-provisioned store instead applies database/staging/*.sql; the shape is
-- identical either way. event_time is folded in here (0028) rather than added by
-- a separate ALTER, which yields the same final column order.
--

CREATE TABLE IF NOT EXISTS ops_event_staging (
    row_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id            VARCHAR(64)             NOT NULL,
    provider          VARCHAR(32)             NOT NULL,
    source_format     VARCHAR(64)             NOT NULL,
    batch_id          VARCHAR(128)            NOT NULL,
    provider_event_id VARCHAR(256)            NOT NULL,
    raw               JSONB                   NOT NULL,
    loaded_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    event_time        TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_ops_event_staging_provider_event
        UNIQUE (org_id, provider, provider_event_id)
);

CREATE INDEX IF NOT EXISTS idx_ops_event_staging_org_row
    ON ops_event_staging (org_id, row_id);

CREATE INDEX IF NOT EXISTS idx_ops_event_staging_org_batch
    ON ops_event_staging (org_id, batch_id);

CREATE INDEX IF NOT EXISTS idx_ops_event_staging_org_format
    ON ops_event_staging (org_id, provider, source_format);

CREATE TABLE IF NOT EXISTS ops_event_load_batches (
    org_id           VARCHAR(64)              NOT NULL,
    batch_id         VARCHAR(128)             NOT NULL,
    provider         VARCHAR(32)              NOT NULL,
    source_format    VARCHAR(64)              NOT NULL,
    source_reference TEXT,
    record_count     INTEGER                  NOT NULL DEFAULT 0,
    skipped_count    INTEGER                  NOT NULL DEFAULT 0,
    loaded_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, batch_id)
);


--
-- MSP-B5 — runbook-match lifecycle, decision history, and labelled feedback
-- (migration 0029).
--
-- SOURCE OF TRUTH: database/models/runbook_match_decisions.py
-- (ALL_RUNBOOK_MATCH_DDL), applied by migration 0029. Copied VERBATIM below —
-- keep identical when the model changes.
--
-- These three tables have NO runtime ensure_* helper: app/runbook_match_decisions.py
-- only reads and writes them. Provisioning is therefore the only thing that can
-- create them, and without them the runbook-match decision API fails with
-- 'relation "runbook_matches" does not exist'.
--

CREATE TABLE IF NOT EXISTS runbook_matches (
    org_id          VARCHAR(64)  NOT NULL,
    recurrence_id   VARCHAR(128) NOT NULL,
    base_state      VARCHAR(16)  NOT NULL,
    current_state   VARCHAR(16)  NOT NULL,
    current_action  VARCHAR(16),
    match_payload   TEXT         NOT NULL,
    revision        INTEGER      NOT NULL DEFAULT 0,
    updated_by      VARCHAR(128),
    created_at      TIMESTAMPTZ  NOT NULL,
    updated_at      TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (org_id, recurrence_id)
);

CREATE TABLE IF NOT EXISTS runbook_match_decision_history (
    id                  VARCHAR(64)  PRIMARY KEY,
    org_id              VARCHAR(64)  NOT NULL,
    recurrence_id       VARCHAR(128) NOT NULL,
    revision            INTEGER      NOT NULL,
    action              VARCHAR(16)  NOT NULL,
    previous_action     VARCHAR(16),
    previous_state      VARCHAR(16)  NOT NULL,
    resulting_state     VARCHAR(16)  NOT NULL,
    actor_id            VARCHAR(128) NOT NULL,
    decided_at          TIMESTAMPTZ  NOT NULL,
    UNIQUE (org_id, recurrence_id, revision)
);

CREATE TABLE IF NOT EXISTS runbook_match_feedback (
    id                  VARCHAR(64)  PRIMARY KEY,
    decision_history_id VARCHAR(64)  NOT NULL UNIQUE,
    org_id              VARCHAR(64)  NOT NULL,
    recurrence_id       VARCHAR(128) NOT NULL,
    feedback_label      VARCHAR(32)  NOT NULL,
    features_payload    TEXT         NOT NULL,
    actor_id            VARCHAR(128) NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runbook_match_history_org_recurrence
    ON runbook_match_decision_history (org_id, recurrence_id, revision DESC);

CREATE INDEX IF NOT EXISTS idx_runbook_match_feedback_org_created
    ON runbook_match_feedback (org_id, created_at DESC);


--
-- Name: pack_states, pack_state_history — 2.0-C1 T2 (AT-827) alembic 0031
--
-- Per-org pack lifecycle state (active/disabled) and its append-only transition
-- history. Like the runbook tables above these have NO runtime ensure_* helper:
-- app/pack_state.py only reads and writes them, so provisioning is the only thing
-- that can create them.
--
-- ABSENCE OF A ROW MEANS 'active'. There is no seed step: provisioning these
-- tables changes no behaviour until a customer disables a pack. The read path in
-- app/pack_state.py is fail-soft, so a deployment that has not yet run this
-- migration serves every pack as active rather than failing.
--

-- pinned_version / previous_version / resulting_version are the 2.0-C1 T3 (AT-828)
-- version-rollback columns (alembic 0032). NULL means "not pinned" — the pack runs
-- its current registry version, exactly as before rollback existed.
CREATE TABLE IF NOT EXISTS pack_states (
    org_id         VARCHAR(64)  NOT NULL,
    pack_id        VARCHAR(64)  NOT NULL,
    state          VARCHAR(16)  NOT NULL,
    revision       INTEGER      NOT NULL DEFAULT 0,
    reason         TEXT,
    updated_by     VARCHAR(128),
    created_at     TIMESTAMPTZ  NOT NULL,
    updated_at     TIMESTAMPTZ  NOT NULL,
    pinned_version VARCHAR(32),
    PRIMARY KEY (org_id, pack_id)
);

CREATE TABLE IF NOT EXISTS pack_state_history (
    id                VARCHAR(64)  PRIMARY KEY,
    org_id            VARCHAR(64)  NOT NULL,
    pack_id           VARCHAR(64)  NOT NULL,
    revision          INTEGER      NOT NULL,
    transition        VARCHAR(16)  NOT NULL,
    previous_state    VARCHAR(16)  NOT NULL,
    resulting_state   VARCHAR(16)  NOT NULL,
    reason            TEXT,
    actor_id          VARCHAR(128) NOT NULL,
    changed_at        TIMESTAMPTZ  NOT NULL,
    previous_version  VARCHAR(32),
    resulting_version VARCHAR(32),
    UNIQUE (org_id, pack_id, revision)
);

-- Idempotent guards for a database provisioned before 0032 (the CREATE above only
-- applies to a fresh install).
ALTER TABLE pack_states
    ADD COLUMN IF NOT EXISTS pinned_version VARCHAR(32);
ALTER TABLE pack_state_history
    ADD COLUMN IF NOT EXISTS previous_version VARCHAR(32);
ALTER TABLE pack_state_history
    ADD COLUMN IF NOT EXISTS resulting_version VARCHAR(32);

CREATE INDEX IF NOT EXISTS idx_pack_state_history_org_pack
    ON pack_state_history (org_id, pack_id, revision DESC);

CREATE INDEX IF NOT EXISTS idx_pack_states_org_state
    ON pack_states (org_id, state);


--
-- Name: pack_certification_reviews — 2.0-C2 T2 (AT-832) alembic 0034
--
-- Append-only certification review trail: who reviewed which pack version,
-- against which criteria, with what decision and on what date (2.0-C2 AC5).
-- Like the tables above it has NO runtime ensure_* helper — provisioning is the
-- only thing that creates it, and app/pack_certification_review.py only reads and
-- appends.
--
-- A review RECORDS a decision; it never grants a badge. Only a valid CloudFulcrum
-- signature over a pack's certification metadata does that (2.0-C2 T1 / AT-831),
-- so nothing in the runtime verification path reads this table. The read path is
-- fail-soft: a deployment that has not yet run 0034 reports no reviews rather than
-- failing a page.
--
-- DELETE/TRUNCATE on this table is REVOKED further down (it is in the protected
-- set) — a certification decision that can be deleted is not auditable.
--

CREATE TABLE IF NOT EXISTS pack_certification_reviews (
    id               VARCHAR(64)  PRIMARY KEY,
    org_id           VARCHAR(64)  NOT NULL,
    pack_id          VARCHAR(64)  NOT NULL,
    pack_version     VARCHAR(32)  NOT NULL,
    revision         INTEGER      NOT NULL,
    reviewer_id      VARCHAR(128) NOT NULL,
    reviewer_name    VARCHAR(256),
    reviewed_at      TIMESTAMPTZ  NOT NULL,
    platform_version VARCHAR(32)  NOT NULL,
    proposed_level   VARCHAR(16)  NOT NULL,
    decision         VARCHAR(16)  NOT NULL,
    criteria         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    scope_summary    TEXT         NOT NULL DEFAULT '',
    notes            TEXT,
    UNIQUE (org_id, pack_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_pack_certification_reviews_org_pack
    ON pack_certification_reviews (org_id, pack_id, revision DESC);


--
-- Name: pack_certification_policies — 2.0-C2 T4 (AT-834) alembic 0035
--
-- Per-org activation floor: the MINIMUM certification level a pack must hold to be
-- activated (e.g. a federal deployment setting 'certified'). No runtime ensure_*
-- helper — provisioning is the only thing that creates it.
--
-- ABSENCE OF A ROW MEANS NO RESTRICTION. Provisioning changes no behaviour until an
-- owner sets a floor; there is no seed step.
--
-- Unlike every other pack read, the policy read FAILS CLOSED: if this table cannot
-- be read, activation is refused rather than proceeding as though no policy were
-- set — a security control that fails open would lift the restriction exactly when
-- it matters most. Lifting a restriction WRITES 'community'; there is no delete
-- path, so "who lowered the floor, and when" stays answerable in audit_log.
--

CREATE TABLE IF NOT EXISTS pack_certification_policies (
    org_id        VARCHAR(64)  PRIMARY KEY,
    minimum_level VARCHAR(16)  NOT NULL,
    revision      INTEGER      NOT NULL DEFAULT 0,
    reason        TEXT,
    updated_by    VARCHAR(128),
    created_at    TIMESTAMPTZ  NOT NULL,
    updated_at    TIMESTAMPTZ  NOT NULL
);


--
-- PostgreSQL database dump complete
--


--
-- Seed: the connector catalog — the ONE table the application cannot start
-- usefully without (the Integration Hub renders its tiles from these rows, and
-- org_connectors_list overlays each org's connection state on top).
--
-- Idempotent via ON CONFLICT DO NOTHING. Mirrors database/seed/connectors.json
-- (28 rows) with one deliberate production difference: every `metrics` array is
-- empty. Metrics are per-org, run-derived values written to the ORG-namespaced
-- overlay by app/connector_metrics.py after a real discovery run; the dev seed's
-- illustrative numbers (e.g. Slack "Messages (7d): 12840") would otherwise render
-- as this customer's real figures until their first run completes.
--
-- The dev seed's `mappings` (17) and `permissions` (5) rows are NOT seeded — see
-- the SEED POLICY note in the file header.
--

-- connectors (28 rows)
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('aws_events', '{"id": "aws_events", "name": "AWS Events", "category": "Cloud Operations - Multi-account", "tier": "standard", "status": "not_configured", "configured": false, "multiScope": true, "scopeNoun": "account", "metrics": [], "lastSynced": "\u2014", "reads": ["CloudWatch Alarms", "EventBridge Rules", "CloudTrail Management Events"], "signalStrength": 55}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('azure_events', '{"id": "azure_events", "name": "Azure Events", "category": "Cloud Operations - Multi-subscription", "tier": "standard", "status": "not_configured", "configured": false, "multiScope": true, "scopeNoun": "subscription", "metrics": [], "lastSynced": "\u2014", "reads": ["Monitor Alerts", "Activity Log (Administrative)", "Service Health"], "signalStrength": 55}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('azure_devops', '{"id": "azure_devops", "name": "Azure DevOps", "category": "ALM / CI/CD", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Work Items", "Pipelines", "Repos"], "signalStrength": 62}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('azure_repos', '{"id": "azure_repos", "name": "Azure Repos", "category": "Source control", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Commits", "Pull Requests", "Branches"], "signalStrength": 55}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('bitbucket', '{"id": "bitbucket", "name": "Bitbucket", "category": "Source control", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Pull Requests", "Repos", "Pipelines"], "signalStrength": 55}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('confluence', '{"id": "confluence", "name": "Confluence", "category": "Docs / knowledge", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Pages", "Spaces", "Templates"], "signalStrength": 58}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('databricks', '{"id": "databricks", "name": "Databricks", "category": "Data \u00b7 Platform", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Job Runs", "Pipelines", "Notebooks"], "signalStrength": 52}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('dbt', '{"id": "dbt", "name": "dbt", "category": "Transforms", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Models", "Tests", "Lineage"], "signalStrength": 42}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('dynamics365', '{"id": "dynamics365", "name": "Dynamics 365", "category": "ERP / CRM", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Accounts", "Opportunities", "Work Orders"], "signalStrength": 65}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('github', '{"id": "github", "name": "GitHub", "category": "Engineering \u00b7 Delivery", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Pull Requests", "Review Comments", "Commits"], "signalStrength": 81}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('gitlab', '{"id": "gitlab", "name": "GitLab", "category": "DevOps", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Merge Requests", "Pipelines", "Issues"], "signalStrength": 60}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('jira', '{"id": "jira", "name": "Jira", "category": "Issues / backlog", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Issues", "Sprints", "Epics"], "signalStrength": 78}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('linear', '{"id": "linear", "name": "Linear", "category": "Product / issues", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Issues", "Projects", "Cycles"], "signalStrength": 55}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('m365', '{"id": "m365", "name": "Microsoft 365", "category": "Comms / docs", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Emails", "Calendar", "Documents"], "signalStrength": 50}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('notion', '{"id": "notion", "name": "Notion", "category": "Docs / wiki", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Pages", "Databases", "Blocks"], "signalStrength": 45}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('oracle_db', '{"id": "oracle_db", "name": "Oracle DB", "category": "Database", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Tables", "Procedures", "AWR Reports"], "signalStrength": 48}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('oracle_ebs', '{"id": "oracle_ebs", "name": "Oracle EBS", "category": "Finance / HR", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["GL Journals", "AP Invoices", "Cost Centers"], "signalStrength": 72}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('postgresql', '{"id": "postgresql", "name": "PostgreSQL", "category": "Database", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Tables", "Views", "Query Logs"], "signalStrength": 48}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('salesforce', '{"id": "salesforce", "name": "Salesforce", "category": "CRM \u00b7 Salesforce / nCino", "tier": "recommended", "recommendedRank": 1, "status": "disconnected", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Accounts", "Opportunities", "Cases"], "signalStrength": 94, "products": []}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('sap', '{"id": "sap", "name": "SAP", "category": "ERP \u00b7 Process", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Change Documents", "Approvals", "Transports (if available)"], "signalStrength": 76}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('servicenow', '{"id": "servicenow", "name": "ServiceNow", "category": "Operations \u00b7 Incidents", "tier": "recommended", "recommendedRank": 2, "status": "disconnected", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Incident Tickets", "Change Requests", "SLA Definitions"], "signalStrength": 88}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('sharepoint', '{"id": "sharepoint", "name": "SharePoint", "category": "Docs / intranet", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Documents", "Lists", "Sites"], "signalStrength": 52}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('slack', '{"id": "slack", "name": "Slack", "category": "Comms \u00b7 Ops", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Channels", "Threads", "Mentions"], "signalStrength": 79}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('snowflake', '{"id": "snowflake", "name": "Snowflake", "category": "Data warehouse", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Schemas", "Query History", "Warehouses"], "signalStrength": 50}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('sql_server', '{"id": "sql_server", "name": "SQL Server", "category": "Database", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Tables", "Stored Procedures", "Query Plans"], "signalStrength": 48}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('teams', '{"id": "teams", "name": "Microsoft Teams", "category": "Comms / docs", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Channels", "Messages", "Meetings"], "signalStrength": 50}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('workday', '{"id": "workday", "name": "Workday", "category": "HR / finance", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Workers", "Business Processes", "Compensation"], "signalStrength": 68}') ON CONFLICT ("id") DO NOTHING;
INSERT INTO "public"."connectors" ("id", "payload") VALUES ('zendesk', '{"id": "zendesk", "name": "Zendesk", "category": "Support", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\u2014", "reads": ["Tickets", "Agents", "SLA Policies"], "signalStrength": 60}') ON CONFLICT ("id") DO NOTHING;

-- mappings / permissions: intentionally NOT seeded in production (see the SEED
-- POLICY note in the file header). Nothing in the application writes these two
-- tables, so the dev seed rows would surface synthetic field mappings and
-- permission checks as if they were this customer's own. The dev/demo rows live
-- in database/seed/mappings.json and database/seed/permissions.json if a
-- non-production environment wants them (via seed_loader.py).

--
-- Alembic head this file provisions. Must equal the newest revision in
-- backend/migrations/versions/ — a DB stamped lower will have `alembic upgrade
-- head` re-run intervening migrations against tables that already exist.
--
-- 2.0-C1/C2: this file now carries the 0031 (pack_states / pack_state_history),
-- 0032 (version-pin columns), 0033 (REVOKE DELETE/TRUNCATE on the history tables)
-- 0034 (pack_certification_reviews) and 0035 (pack_certification_policies)
-- objects, so it must stamp 0035. A stale stamp would leave a
-- freshly provisioned database claiming a head it is ahead of, and `alembic upgrade
-- head` would then re-run 0031-0033. Those are all idempotent, so it would not
-- break, but the recorded head would be wrong. Bump this whenever you add DDL here.
INSERT INTO "public"."alembic_version" ("version_num") VALUES ('0035') ON CONFLICT DO NOTHING;


--
-- Application-role grants.
--
-- provision.sql builds the schema as the role that RUNS it (a superuser such as
-- 'postgres', or the schema owner 'agentiq' set above), so THAT role owns the
-- tables and implicitly has every privilege. But an application may log in as a
-- SEPARATE role from the tables' owner — this deployment's app logs in as
-- 'aiqdevusr' (see DEV_DATABASE_URL / PROD_DATABASE_URL in .env), which owns
-- nothing here and would otherwise hit "permission denied for table ..." on its
-- first write (e.g. seed_owner -> workspace_members).
--
-- Grant every app login role this deployment uses USAGE/CREATE on the schema and
-- full privileges on all CURRENT and FUTURE objects. Guarded per role, so it is a
-- safe no-op where a given role does not exist. To support another app role, add
-- it to the array below. Pure SQL (no psql \set), so this runs identically via
-- `psql -f` and via a psycopg2 loader.
--
-- TODO(deploy) app roles: the array below lists DEV roles. 'aiqdevusr' is the dev
-- app login; a production database is reached with a different role (see
-- PROD_DATABASE_URL in .env.template). ADD THE PRODUCTION APP ROLE — the username
-- from the DATABASE_URL the application will actually use — before running this
-- file. Because each role is guarded by an existence check, a role that is absent
-- is silently skipped: leaving the prod role out fails SILENTLY here and then
-- surfaces at runtime as "permission denied for table ..." on the app's first
-- write (e.g. seed_owner -> workspace_members at startup).
--
DO
$$
DECLARE
    r text;
    app_roles text[] := ARRAY['agentiq', 'aiqdevusr'];  -- TODO(deploy): add the prod app role
BEGIN
    FOREACH r IN ARRAY app_roles LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('GRANT USAGE, CREATE ON SCHEMA public TO %I', r);
            EXECUTE format('GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO %I', r);
            EXECUTE format('GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO %I', r);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO %I', r);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO %I', r);
        END IF;
    END LOOP;
END
$$;


--
-- Run-history retention — 2.0-C1 T4 (AT-829) alembic 0033
--
-- The GRANT block above hands each app role ALL PRIVILEGES, which includes DELETE
-- and TRUNCATE. Claw those two back on the tables holding run history, so no
-- application path — intended or buggy — can remove a finding, its evidence, a run
-- record, or the pack lifecycle audit trail (2.0-C1 AC4). The DATABASE refuses it.
--
-- MUST run AFTER the GRANT block, or the grant hands DELETE straight back.
--
-- The protected set is declared once in backend/app/history_retention.py
-- (PROTECTED_TABLE_REASONS) and mirrored here; a contract test asserts the two
-- agree, so adding a table there without updating this block fails CI.
--
-- Deliberately NOT protected (deletion is correct for them):
--   retrieval_chunks / retrieval_refresh_queue — derived vector index + work queue;
--       R18-B2 freshness purges chunks when a source artifact changes. Re-embeddable,
--       loses no history.
--   entity_relationships — cross-run graph state that relationship_mapper prunes when
--       a relationship no longer holds; a current view, not a historical record.
--
-- NOTE run_events: db.delete_run_events is a SOFT delete (UPDATE is_deleted = TRUE),
-- which is why revoking DELETE here does not break rewriting a run's event list.
--
-- Idempotent: REVOKE on an already-revoked privilege is a no-op, and both the role
-- and the table are existence-guarded.
--
DO
$$
DECLARE
    r text;
    t text;
    app_roles text[] := ARRAY['agentiq', 'aiqdevusr'];  -- TODO(deploy): add the prod app role
    protected_tables text[] := ARRAY[
        'kv',                   -- run-scoped artifacts: findings, evidence, roadmap, report
        'opportunity_instances',-- per-instance pack id + pack version stamps (R16-B1 §4)
        'pack_certification_reviews', -- append-only certification review trail (2.0-C2 T2)
        'pack_state_history',   -- append-only pack lifecycle trail (2.0-C1 T2/T3)
        'run_events',           -- run event log (soft-deleted, never removed)
        'runs'                  -- run records
    ];
BEGIN
    FOREACH r IN ARRAY app_roles LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            FOREACH t IN ARRAY protected_tables LOOP
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = t
                ) THEN
                    EXECUTE format(
                        'REVOKE DELETE, TRUNCATE ON TABLE public.%I FROM %I', t, r
                    );
                END IF;
            END LOOP;
        END IF;
    END LOOP;
END
$$;
