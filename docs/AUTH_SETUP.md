# AUTH_SETUP.md — Obtaining Salesforce Credentials for SF-3.1

SF-3.1 live mode requires two environment variables:

```bash
export SF_INSTANCE_URL=https://your-org.my.salesforce.com
export SF_ACCESS_TOKEN=your_bearer_token
```

The token is a standard Salesforce OAuth 2.0 Bearer token. Three ways to get one:

---

## Option 1 — Salesforce CLI (fastest)

```bash
# Install CLI: https://developer.salesforce.com/tools/salesforcecli
sf org login web --alias sf31-dev

# Get the access token
sf org display --target-org sf31-dev --json | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('SF_INSTANCE_URL=' + d['result']['instanceUrl'])
print('SF_ACCESS_TOKEN=' + d['result']['accessToken'])
"
```

Copy the two lines into your shell. Token is valid for your session.

---

## Option 2 — Workbench (browser-based, no install)

1. Go to https://workbench.developerforce.com
2. Choose your org type (Production / Sandbox / Developer)
3. Click **Login with Salesforce** and authenticate
4. Go to **Info > Session Information**
5. Copy the `Access Token` and `Instance URL` fields

```bash
export SF_INSTANCE_URL=<Instance URL from Workbench>
export SF_ACCESS_TOKEN=<Access Token from Workbench>
```

---

## Option 3 — Postman / cURL OAuth flow

```bash
# Username-Password OAuth (dev orgs only — not for production)
curl -X POST https://login.salesforce.com/services/oauth2/token \
  -d "grant_type=password" \
  -d "client_id=YOUR_CONSUMER_KEY" \
  -d "client_secret=YOUR_CONSUMER_SECRET" \
  -d "username=YOUR_USERNAME" \
  -d "password=YOUR_PASSWORD_PLUS_SECURITY_TOKEN"

# Response contains access_token and instance_url
```

For sandbox orgs replace `login.salesforce.com` with `test.salesforce.com`.

---

## Token Expiration

Salesforce Bearer tokens expire after the session timeout configured in your org
(default: 2 hours for developer orgs, 8 hours for some sandboxes).

If SF-3.1 returns HTTP 401 mid-run:

```bash
# Re-authenticate with CLI
sf org login web --alias sf31-dev
eval $(sf org display --target-org sf31-dev --json | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = d['result']
print('export SF_INSTANCE_URL=' + r['instanceUrl'])
print('export SF_ACCESS_TOKEN=' + r['accessToken'])
")

# Re-run the validator
python -m backend.discovery.ingest.live_validator --report-path runs/sf31_report.json
```

---

## Tooling API Permissions

`get_flow_inventory` uses the Salesforce Tooling API (`/services/data/v59.0/tooling/`).
This requires the user or Connected App to have:

- **Profile permission**: Modify Metadata (or API Enabled + read access to Flow)
- OR the token is from a System Administrator profile (has Tooling API by default)

If you see:
```
ERROR [get_flow_inventory]  SOQL query failed: Tooling API access required
```

Check: Setup > Users > [your user] > Profile > System Permissions > `API Enabled` and `Modify Metadata Through Metadata API Functions`.

---

## Quick Checklist Before Running

```bash
# 1. Verify both vars are set
echo $SF_INSTANCE_URL
echo $SF_ACCESS_TOKEN | head -c 20  # show first 20 chars only

# 2. Quick connectivity test
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $SF_ACCESS_TOKEN" \
  "$SF_INSTANCE_URL/services/data/v59.0/limits/"
# Expected: 200

# 3. Run validator
python -m backend.discovery.ingest.live_validator --check-only
```
