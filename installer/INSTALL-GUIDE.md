# AgentIQ — Installation & HTTPS Setup Guide

**Applies to:** AgentIQ-Setup-1.7.0.msi · Windows Server 2025 (also 2016+) · x64

This guide walks an administrator through installing AgentIQ, enabling HTTPS
with your organization's SSL certificate, and browsing the application by
domain name.

---

## 1. Before you start

| Requirement | Detail |
|---|---|
| Operating system | Windows Server 2025 (2016 or later supported), 64-bit |
| Account | Local Administrator |
| Disk space | ~5 GB free on `C:` |
| Network ports | 80 (HTTP) and 443 (HTTPS) free — the installer stops IIS "Default Web Site" if it holds port 80 |
| Internet | Not required — all components are bundled. (Used only as a fallback if a bundled component is missing.) |

**What the installer sets up for you:** PostgreSQL 16 (service
`AgentIQ-PostgreSQL`, port 5433), Python 3.11 runtime, IIS with reverse proxy,
the AgentIQ backend (service `AgentIQ-Backend`, port 8000), the web
application on port 80, and Windows Firewall rules. Everything lives under
`C:\AgentIQ`.

---

## 2. Install AgentIQ

### Interactive (recommended)

1. Copy `AgentIQ-Setup-1.7.0.msi` anywhere on the server (Desktop, `C:\Temp` — the location does not matter).
2. Right-click the MSI → **Install** (or double-click). Accept the elevation prompt.
3. Follow the wizard: license → install folder (keep the default `C:\AgentIQ`) → Install.
4. The wizard finishes in under a minute — **this is expected**. It has copied
   the setup files; the actual installation (PostgreSQL, Python, IIS, services)
   now runs in the background and takes **10–25 minutes**.
5. On the Finish page, keep **"Show installation progress and open AgentIQ
   when ready"** ticked and click Finish. A progress window opens and shows
   each installation step live:

   ```
   [STEP] PostgreSQL
     OK   PostgreSQL data directory initialised
     OK   PostgreSQL configured
   [STEP] Python backend environment
     OK   venv created
     ...
   ============================================================
    AgentIQ installation COMPLETE.
    Application verified healthy - opening in your browser.
   ============================================================
   ```

6. When the window reports **COMPLETE**, your browser opens
   `http://localhost/` at the AgentIQ sign-in page. Done.

> Closing the progress window does **not** stop the installation. It is
> finished when the file `C:\AgentIQ\logs\install-complete.txt` exists.

### Silent (for scripted/mass deployment)

```powershell
msiexec /i AgentIQ-Setup-1.7.0.msi /qn /norestart /l*v C:\Windows\Temp\agentiq-msi.log
```

The command returns quickly; installation continues in the background.
Wait for `C:\AgentIQ\logs\install-complete.txt` to appear (10–25 min), e.g.:

```powershell
while (-not (Test-Path C:\AgentIQ\logs\install-complete.txt)) { Start-Sleep 30 }
```

### Upgrading from an earlier version

Run the new MSI the same way — no uninstall needed. It upgrades in place and
**preserves your database and configuration** (`C:\AgentIQ\backend\.env`).

---

## 3. Verify the installation

```powershell
Get-Service AgentIQ-Backend, AgentIQ-PostgreSQL        # both "Running"
Invoke-WebRequest http://localhost/api/health -UseBasicParsing
# -> {"ok":true, "status":"healthy", "checks":{"database":{"status":"ok"}}}
```

Then browse **http://localhost/** — you should see the AgentIQ sign-in page.

---

## 4. Register your SSL certificate (HTTPS)

AgentIQ ships with `C:\AgentIQ\scripts\Enable-Https.ps1`, which performs the
entire HTTPS setup in one step.

### 4.1 Get your certificate as a PFX file

IIS needs a **PFX** (PKCS#12) containing the certificate, private key, and
chain.

- **Already have a `.pfx`?** Skip to 4.2.
- **Have PEM files** (`cert.pem`, `privkey.pem`, `fullchain.pem` — e.g. from
  Let's Encrypt)? Convert them (OpenSSL ships with Git for Windows):

  ```powershell
  & "C:\Program Files\Git\usr\bin\openssl.exe" pkcs12 -export `
      -out C:\Temp\ssl\agentiq.pfx `
      -inkey C:\Temp\ssl\privkey.pem `
      -in   C:\Temp\ssl\fullchain.pem `
      -passout pass:YOUR_PFX_PASSWORD
  ```

### 4.2 Run the HTTPS setup script

As Administrator (replace the FQDN with your domain name):

```powershell
powershell -ExecutionPolicy Bypass -File C:\AgentIQ\scripts\Enable-Https.ps1 `
    -PfxPath C:\Temp\ssl\agentiq.pfx -Fqdn aiq.yourcompany.com
```

You are prompted for the PFX password. The script then:

1. Imports the certificate into the machine store
2. Adds the HTTPS (443) binding to the AgentIQ site
3. Binds the certificate (via `netsh` — reliable on all Windows versions)
4. Adds an automatic HTTP → HTTPS redirect
5. Updates the backend configuration and restarts services
6. Verifies and prints **"HTTPS is working"**

> **Note on verification:** always test HTTPS with a browser or `curl.exe`.
> PowerShell's `Invoke-WebRequest` on Windows PowerShell 5.1 falsely reports
> TLS failures against modern (ECDSA) certificates even when HTTPS is working
> perfectly.

```powershell
curl.exe -sk https://localhost/api/health       # -> {"ok":true, ...}
```

### 4.3 Certificate renewal

When your certificate is renewed (Let's Encrypt certs expire every ~90 days),
repeat 4.1–4.2 with the new files. The script is safe to re-run.

---

## 5. Browse the application by domain name

The server answers **any** hostname that reaches it — no IIS changes are
needed. You only need name resolution:

1. **DNS (proper way):** on your internal DNS server, create an **A record**
   pointing your chosen name at this server's IP:

   ```
   aiq.yourcompany.com  →  <server IP>
   ```

2. **Hosts file (quick test, per client machine):** as Administrator, add a
   line to `C:\Windows\System32\drivers\etc\hosts` on the client:

   ```
   <server IP>   aiq.yourcompany.com
   ```

   then run `ipconfig /flushdns`.

**Test from a client machine:**

```powershell
ping aiq.yourcompany.com                     # must reply from the server IP
```

Then browse **https://aiq.yourcompany.com/** (after HTTPS setup) or
`http://…` before it. With HTTPS enabled, any `http://` request is redirected
to `https://` automatically.

> Modern browsers silently upgrade addresses to `https://`. If a page "won't
> load" over plain HTTP but the server responds to `curl`, complete the HTTPS
> setup (section 4) — that resolves it.

---

## 6. Everyday operations

| Task | How |
|---|---|
| Edit configuration | `notepad C:\AgentIQ\backend\.env`, then restart the backend |
| Restart backend | `Restart-Service AgentIQ-Backend -Force` |
| Restart everything | `Restart-Service AgentIQ-PostgreSQL, AgentIQ-Backend -Force; iisreset /restart` |
| Configuration wizard | Start Menu → AgentIQ → **Configure AgentIQ** |
| Open the app | Start Menu → AgentIQ → **Open AgentIQ** |

**Going production / public hostname:** in `.env` set
`ENVIRONMENT=production`, `AGENTIQ_BACKEND_URL=https://<your FQDN>`, and
`AGENTIQ_ADMIN_EMAIL=<admin email>`, then restart the backend. (In production
mode the backend refuses to start until these are set correctly.)

---

## 7. Logs & troubleshooting

| Log | Location |
|---|---|
| Installation | `C:\AgentIQ\logs\install-*.log` (+ `install-complete.txt` marker) |
| Backend errors | `C:\AgentIQ\logs\backend-error.log` — check first if the API is down |
| Backend output | `C:\AgentIQ\logs\backend.log` |
| Web requests (IIS) | `C:\inetpub\logs\LogFiles\W3SVC<n>\u_ex*.log` |
| PostgreSQL | `C:\AgentIQ\postgres\data\log\postgresql-*.log` |

| Symptom | Fix |
|---|---|
| Wizard finished but app not up yet | Normal — installation continues in the background; wait for `install-complete.txt` (10–25 min) |
| Page is blank after an upgrade | Hard-refresh the browser: **Ctrl+F5** (clears the cached old version) |
| `Invoke-WebRequest` fails on https | False alarm on PS 5.1 + ECDSA certs — verify with a browser or `curl.exe -sk` |
| API down (`/api/health` fails) | `Get-Content C:\AgentIQ\logs\backend-error.log -Tail 40`, fix `.env` issue shown, restart backend |
| FQDN doesn't resolve | Add the DNS A record (or client hosts entry) from section 5 |

---

## 8. Uninstall

Settings → Apps → **AgentIQ** → Uninstall (or Start Menu → AgentIQ →
Uninstall AgentIQ). Services, the IIS site, firewall rules and program files
are removed. The database directory is preserved in case of reinstall; delete
`C:\AgentIQ` manually if you want a complete wipe.
