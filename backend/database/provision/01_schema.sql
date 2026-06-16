--
-- PostgreSQL database dump
--

\restrict gcZ7QPeJ2RxfpJ6Jlf2NZjBZpBxLzKtzNQotfEgHdrEbDf22Rmwa2TtIknje8vD

-- Dumped from database version 17.10
-- Dumped by pg_dump version 17.10

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = "heap";

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
    "refresh_failed" integer DEFAULT 0 NOT NULL
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
    "succeeded" boolean NOT NULL
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
    "data" "text" NOT NULL
);


--
-- Name: oauth_nonces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."oauth_nonces" (
    "nonce" "text" NOT NULL,
    "connector_id" "text" NOT NULL,
    "expires_at" "text" NOT NULL
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
    "created_at" timestamp without time zone NOT NULL
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
    "payload" "text" NOT NULL
);


--
-- Name: runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."runs" (
    "id" "text" NOT NULL,
    "payload" "text" NOT NULL,
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
    "last_login_at" timestamp without time zone
);


--
-- Name: workspace_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE "public"."workspace_members" (
    "org_id" "text" NOT NULL,
    "user_id" "text" NOT NULL,
    "role" "text" NOT NULL,
    "created_at" "text" NOT NULL,
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

\unrestrict gcZ7QPeJ2RxfpJ6Jlf2NZjBZpBxLzKtzNQotfEgHdrEbDf22Rmwa2TtIknje8vD

-- Alembic version stamp (snapshot taken at head 0010)
INSERT INTO "public"."alembic_version" ("version_num") VALUES ('0010') ON CONFLICT DO NOTHING;
