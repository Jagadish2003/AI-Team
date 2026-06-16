--
-- PostgreSQL database dump
--

\restrict 7jfyZTmFiVkBiTYbdOfUheofUoTTjmFzERZn4ryGfDBVg2ROGOZQhnBYpQjXec8

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

--
-- Data for Name: connectors; Type: TABLE DATA; Schema: public; Owner: -
--

COPY "public"."connectors" ("id", "payload") FROM stdin;
servicenow	{"id": "servicenow", "name": "ServiceNow", "category": "Operations \\u00b7 Incidents", "tier": "recommended", "recommendedRank": 2, "status": "disconnected", "configured": false, "metrics": [{"label": "Incidents (90d)", "value": "5"}, {"label": "Benefit signals", "value": "5"}], "lastSynced": "\\u2014", "reads": ["Incident Tickets", "Benefit Operations", "SLA Definitions"], "signalStrength": 88}
sap	{"id": "sap", "name": "SAP", "category": "ERP \\u00b7 Process", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [{"label": "Change Documents", "value": "1420"}, {"label": "Approvals", "value": "312"}], "lastSynced": "\\u2014", "reads": ["Change Documents", "Approvals", "Transports (if available)"], "signalStrength": 76}
github	{"id": "github", "name": "GitHub", "category": "Engineering \\u00b7 Delivery", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [{"label": "Pull Requests", "value": "248"}, {"label": "Review Comments", "value": "913"}], "lastSynced": "\\u2014", "reads": ["Pull Requests", "Review Comments", "Commits"], "signalStrength": 81}
slack	{"id": "slack", "name": "Slack", "category": "Comms \\u00b7 Ops", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [{"label": "Channels", "value": "34"}, {"label": "Messages (7d)", "value": "12840"}], "lastSynced": "\\u2014", "reads": ["Channels", "Threads", "Mentions"], "signalStrength": 79}
databricks	{"id": "databricks", "name": "Databricks", "category": "Data \\u00b7 Platform", "tier": "standard", "status": "disconnected", "configured": false, "metrics": [{"label": "Job Runs", "value": "184"}, {"label": "Pipelines", "value": "27"}], "lastSynced": "\\u2014", "reads": ["Job Runs", "Pipelines", "Notebooks"], "signalStrength": 52}
oracle_ebs	{"id": "oracle_ebs", "name": "Oracle EBS", "category": "Finance / HR", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["GL Journals", "AP Invoices", "Cost Centers"], "signalStrength": 72}
workday	{"id": "workday", "name": "Workday", "category": "HR / finance", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Workers", "Business Processes", "Compensation"], "signalStrength": 68}
dynamics365	{"id": "dynamics365", "name": "Dynamics 365", "category": "ERP / CRM", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Accounts", "Opportunities", "Work Orders"], "signalStrength": 65}
jira	{"id": "jira", "name": "Jira", "category": "Issues / backlog", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Issues", "Sprints", "Epics"], "signalStrength": 78}
azure_devops	{"id": "azure_devops", "name": "Azure DevOps", "category": "ALM / CI/CD", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Work Items", "Pipelines", "Repos"], "signalStrength": 62}
linear	{"id": "linear", "name": "Linear", "category": "Product / issues", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Issues", "Projects", "Cycles"], "signalStrength": 55}
zendesk	{"id": "zendesk", "name": "Zendesk", "category": "Support", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Tickets", "Agents", "SLA Policies"], "signalStrength": 60}
teams	{"id": "teams", "name": "Microsoft Teams", "category": "Comms / docs", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Channels", "Messages", "Meetings"], "signalStrength": 50}
m365	{"id": "m365", "name": "Microsoft 365", "category": "Comms / docs", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Emails", "Calendar", "Documents"], "signalStrength": 50}
confluence	{"id": "confluence", "name": "Confluence", "category": "Docs / knowledge", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Pages", "Spaces", "Templates"], "signalStrength": 58}
sharepoint	{"id": "sharepoint", "name": "SharePoint", "category": "Docs / intranet", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Documents", "Lists", "Sites"], "signalStrength": 52}
notion	{"id": "notion", "name": "Notion", "category": "Docs / wiki", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Pages", "Databases", "Blocks"], "signalStrength": 45}
gitlab	{"id": "gitlab", "name": "GitLab", "category": "DevOps", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Merge Requests", "Pipelines", "Issues"], "signalStrength": 60}
bitbucket	{"id": "bitbucket", "name": "Bitbucket", "category": "Source control", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Pull Requests", "Repos", "Pipelines"], "signalStrength": 55}
azure_repos	{"id": "azure_repos", "name": "Azure Repos", "category": "Source control", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Commits", "Pull Requests", "Branches"], "signalStrength": 55}
postgresql	{"id": "postgresql", "name": "PostgreSQL", "category": "Database", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Tables", "Views", "Query Logs"], "signalStrength": 48}
sql_server	{"id": "sql_server", "name": "SQL Server", "category": "Database", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Tables", "Stored Procedures", "Query Plans"], "signalStrength": 48}
oracle_db	{"id": "oracle_db", "name": "Oracle DB", "category": "Database", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Tables", "Procedures", "AWR Reports"], "signalStrength": 48}
snowflake	{"id": "snowflake", "name": "Snowflake", "category": "Data warehouse", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Schemas", "Query History", "Warehouses"], "signalStrength": 50}
salesforce	{"id": "salesforce", "name": "Salesforce", "category": "CRM \\u00b7 PSS Benefits Administration", "tier": "recommended", "recommendedRank": 1, "status": "disconnected", "configured": false, "metrics": [{"label": "Applications", "value": "3"}, {"label": "Benefit Assignments", "value": "2"}], "lastSynced": "\\u2014", "reads": ["IndividualApplication", "BenefitAssignment", "Case (Disability)"], "signalStrength": 94, "products": []}
dbt	{"id": "dbt", "name": "dbt", "category": "Transforms", "tier": "standard", "status": "not_configured", "configured": false, "metrics": [], "lastSynced": "\\u2014", "reads": ["Models", "Tests", "Lineage"], "signalStrength": 42}
\.


--
-- Data for Name: mappings; Type: TABLE DATA; Schema: public; Owner: -
--

COPY "public"."mappings" ("id", "payload") FROM stdin;
map1	{"id": "map1", "sourceSystem": "ServiceNow", "sourceField": "incident.number", "sourceType": "string", "commonField": "ticket.id", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}
map2	{"id": "map2", "sourceSystem": "ServiceNow", "sourceField": "incident.assigned_to", "sourceType": "reference", "commonField": "ticket.owner", "status": "MAPPED", "confidence": "MEDIUM", "commonEntity": "Ticket"}
map3	{"id": "map3", "sourceSystem": "Jira", "sourceField": "issue.status", "sourceType": "string", "commonField": "ticket.status", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}
map4	{"id": "map4", "sourceSystem": "Jira", "sourceField": "issue.approvals", "sourceType": "array", "commonField": "ticket.approvals", "status": "AMBIGUOUS", "confidence": "MEDIUM", "commonEntity": "Ticket"}
map5	{"id": "map5", "sourceSystem": "Databricks", "sourceField": "job_run.state", "sourceType": "string", "commonField": "pipeline.runStatus", "status": "UNMAPPED", "confidence": "LOW", "commonEntity": "Pipeline"}
map6	{"id": "map6", "sourceSystem": "ServiceNow", "sourceField": "field_6", "sourceType": "string", "commonField": "common_6", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}
map7	{"id": "map7", "sourceSystem": "Jira", "sourceField": "field_7", "sourceType": "string", "commonField": "common_7", "status": "AMBIGUOUS", "confidence": "MEDIUM", "commonEntity": "Ticket"}
map8	{"id": "map8", "sourceSystem": "Jira", "sourceField": "field_8", "sourceType": "string", "commonField": "common_8", "status": "UNMAPPED", "confidence": "LOW", "commonEntity": "Ticket"}
map9	{"id": "map9", "sourceSystem": "ServiceNow", "sourceField": "field_9", "sourceType": "string", "commonField": "common_9", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}
map10	{"id": "map10", "sourceSystem": "Jira", "sourceField": "field_10", "sourceType": "string", "commonField": "common_10", "status": "AMBIGUOUS", "confidence": "MEDIUM", "commonEntity": "Ticket"}
map11	{"id": "map11", "sourceSystem": "Jira", "sourceField": "field_11", "sourceType": "string", "commonField": "common_11", "status": "UNMAPPED", "confidence": "LOW", "commonEntity": "Ticket"}
map12	{"id": "map12", "sourceSystem": "ServiceNow", "sourceField": "field_12", "sourceType": "string", "commonField": "common_12", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}
map13	{"id": "map13", "sourceSystem": "Jira", "sourceField": "field_13", "sourceType": "string", "commonField": "common_13", "status": "AMBIGUOUS", "confidence": "MEDIUM", "commonEntity": "Ticket"}
map14	{"id": "map14", "sourceSystem": "Jira", "sourceField": "field_14", "sourceType": "string", "commonField": "common_14", "status": "UNMAPPED", "confidence": "LOW", "commonEntity": "Ticket"}
map15	{"id": "map15", "sourceSystem": "ServiceNow", "sourceField": "field_15", "sourceType": "string", "commonField": "common_15", "status": "MAPPED", "confidence": "HIGH", "commonEntity": "Ticket"}
map16	{"id": "map16", "sourceSystem": "Jira", "sourceField": "field_16", "sourceType": "string", "commonField": "common_16", "status": "AMBIGUOUS", "confidence": "MEDIUM", "commonEntity": "Ticket"}
map17	{"id": "map17", "sourceSystem": "Jira", "sourceField": "field_17", "sourceType": "string", "commonField": "common_17", "status": "UNMAPPED", "confidence": "LOW", "commonEntity": "Ticket"}
\.


--
-- Data for Name: permissions; Type: TABLE DATA; Schema: public; Owner: -
--

COPY "public"."permissions" ("id", "payload") FROM stdin;
p_sn_inc	{"id": "p_sn_inc", "label": "ServiceNow: read incidents", "sourceSystem": "ServiceNow", "satisfied": true, "required": true}
p_sn_cmdb	{"id": "p_sn_cmdb", "label": "ServiceNow: read CMDB", "sourceSystem": "ServiceNow", "satisfied": true, "required": true}
p_jira_iss	{"id": "p_jira_iss", "label": "Jira: read issues", "sourceSystem": "Jira", "satisfied": true, "required": true}
p_conf_pages	{"id": "p_conf_pages", "label": "Confluence: read pages", "sourceSystem": "Jira", "satisfied": false, "required": false}
p_m365_teams	{"id": "p_m365_teams", "label": "Microsoft 365: read Teams channel metadata", "sourceSystem": "Microsoft 365", "satisfied": false, "required": true}
\.


--
-- Data for Name: uploads; Type: TABLE DATA; Schema: public; Owner: -
--

COPY "public"."uploads" ("id", "payload") FROM stdin;
\.


--
-- PostgreSQL database dump complete
--

\unrestrict 7jfyZTmFiVkBiTYbdOfUheofUoTTjmFzERZn4ryGfDBVg2ROGOZQhnBYpQjXec8

