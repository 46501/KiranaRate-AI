# KiranaIQ — Single-Store Production Deployment Guide

This guide describes how to deploy, configure, secure, and operate **KiranaIQ** (AI Bill Scanner) as a production-grade, single-instance retail intelligence appliance for an Indian grocery / kirana merchant.

---

## Architecture Overview

```
                        ┌──────────────────────────────────────────────┐
                        │              Internet Clients                │
                        │   (Merchant WhatsApp, Meta Webhooks, Browser)│
                        └───────────────────────┬──────────────────────┘
                                                │ HTTPS (Port 443)
                                                ▼
                        ┌──────────────────────────────────────────────┐
                        │             Nginx Reverse Proxy              │
                        │  - TLS Termination (Let's Encrypt / Certbot) │
                        │  - Appends X-Forwarded-For: <Client IP>      │
                        │  - Enforces client_max_body_size 10M         │
                        └───────────────────────┬──────────────────────┘
                                                │ HTTP (127.0.0.1:8000)
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ KiranaIQ Application Host (Linux/systemd)                                   │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ Uvicorn ASGI Server (Single Process, 1 Worker)                        │  │
│  │  - FastAPI Web Framework                                              │  │
│  │  - Right-to-Left Trusted Proxy IP Parsing (TRUSTED_PROXIES=127.0.0.1) │  │
│  │  - Security: Rate Limiting, Brute Force Lockout, CSRF, Session Cookie │  │
│  │  - HMAC-SHA256 Webhook Verification (X-Hub-Signature-256)             │  │
│  │  - Google GenAI SDK (Gemini 3.8 Flash Vision Extraction)              │  │
│  │  - Meta WhatsApp Cloud API (Media Download & Text/Template Alerts)    │  │
│  └───────────────────────────────────┬───────────────────────────────────┘  │
│                                      │                                      │
│                                      ▼                                      │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ SQLite Database (WAL Mode, busy_timeout=30000ms)                      │  │
│  │  - Ledger, Review Queue, Price History, Audit Trail                   │  │
│  │  - Safe multi-reader single-writer within single instance              │  │
│  │  - Automated Zero-Downtime Hot Backups via scripts/backup_db.py        │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Prerequisites

- **Server:** Ubuntu 22.04 LTS or 24.04 LTS (1 vCPU, 2 GB RAM minimum, 20 GB SSD).
- **Domain:** A registered domain or subdomain pointing to the server's public IP (e.g., `kirana.example.com`).
- **Python:** Python 3.10, 3.11, or 3.12 with `python3-venv` and `python3-pip`.
- **Meta Developer Account:** For WhatsApp Cloud API integration.
- **Google AI Studio Account:** For Gemini API Key (`gemini-3.8-flash`).

---

## 2. Directory Layout & User Setup

Run on the server as `root` or `sudo`:

```bash
# Create dedicated unprivileged service user
useradd -r -s /bin/false -d /opt/kiranaiq kiranaiq

# Create application directory
mkdir -p /opt/kiranaiq
mkdir -p /opt/kiranaiq/media
mkdir -p /var/backups/kiranaiq
mkdir -p /var/log/kiranaiq

# Clone repository
git clone https://github.com/your-username/AI-Bill-Scanner.git /opt/kiranaiq
cd /opt/kiranaiq

# Create virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Set ownership
chown -R kiranaiq:kiranaiq /opt/kiranaiq
chown -R kiranaiq:kiranaiq /var/backups/kiranaiq
chown -R kiranaiq:kiranaiq /var/log/kiranaiq
chmod 700 /opt/kiranaiq/media
chmod 700 /var/backups/kiranaiq
```

---

## 3. Production Environment Configuration (`.env`)

Create `/opt/kiranaiq/.env` (owned by `kiranaiq`, permissions `0600`):

```bash
sudo -u kiranaiq nano /opt/kiranaiq/.env
```

```ini
# ============================================================
# KiranaIQ Production Environment Variables
# ============================================================

# Environment & Security
ENVIRONMENT=production
DEBUG=false
TRUSTED_PROXIES=127.0.0.1,::1
SESSION_COOKIE_SECURE=true

# Secrets (Generate using: python3 -c "import secrets; print(secrets.token_hex(32))")
SESSION_SECRET_KEY=REPLACE_WITH_RANDOM_64_CHAR_HEX_STRING
API_KEY=REPLACE_WITH_RANDOM_64_CHAR_HEX_STRING

# Dashboard Authentication
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=REPLACE_WITH_STRONG_STORE_PASSWORD

# Rate Limiting
RATE_LIMIT_ENABLED=true
RATE_LIMIT_LOGIN_PER_MINUTE=10
RATE_LIMIT_UPLOAD_PER_MINUTE=20
RATE_LIMIT_WEBHOOK_PER_MINUTE=120
LOGIN_MAX_FAILED_ATTEMPTS=5
LOGIN_LOCKOUT_SECONDS=300

# Meta WhatsApp Cloud API
WHATSAPP_VERIFY_TOKEN=REPLACE_WITH_RANDOM_VERIFY_TOKEN
WHATSAPP_APP_SECRET=REPLACE_WITH_META_APP_SECRET
WHATSAPP_ACCESS_TOKEN=REPLACE_WITH_PERMANENT_SYSTEM_USER_ACCESS_TOKEN
WHATSAPP_PHONE_NUMBER_ID=REPLACE_WITH_META_PHONE_NUMBER_ID
WHATSAPP_API_VERSION=v19.0
WHATSAPP_OWNER_NUMBER=919876543210
WHATSAPP_ALLOWLIST=919876543210,919123456789
WHATSAPP_TEMPLATE_NAME=purchase_rate_review
WHATSAPP_TEMPLATE_LANG=en

# Google Gemini Vision
MOCK_GEMINI=false
GEMINI_API_KEY=REPLACE_WITH_GOOGLE_AI_STUDIO_API_KEY
GEMINI_MODEL=gemini-3.8-flash
GEMINI_REQUEST_TIMEOUT=30

# Storage & Database
STORAGE_BACKEND=sqlite
SQLITE_DB_PATH=/opt/kiranaiq/bills.db
MEDIA_STORAGE_DIR=/opt/kiranaiq/media
BILL_SIZE_LIMIT_MB=5

# Autonomous Workers
ENABLE_BACKGROUND_WORKERS=true
WORKER_SCAN_INTERVAL_SECONDS=15
WORKER_POST_INTERVAL_SECONDS=15
WORKER_ALERT_INTERVAL_SECONDS=30

# Business Rules
TZ_NAME=Asia/Kolkata
ALERT_EXPIRY_HOURS=24
ALERT_LOOKBACK_DAYS=30
ALERT_NEW_BILL_DAYS=7
ALERT_MIN_GAP_PCT=2.00
ALERT_MIN_GAP_PAISE=1
```

Set strict permissions:
```bash
chmod 600 /opt/kiranaiq/.env
chown kiranaiq:kiranaiq /opt/kiranaiq/.env
```

---

## 4. Systemd Service Setup

Create `/etc/systemd/system/kiranaiq.service`:

```ini
[Unit]
Description=KiranaIQ Production Bill Scanner & Retail Intelligence
After=network.target

[Service]
Type=simple
User=kiranaiq
Group=kiranaiq
WorkingDirectory=/opt/kiranaiq
EnvironmentFile=/opt/kiranaiq/.env
ExecStart=/opt/kiranaiq/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
Restart=always
RestartSec=5
StandardOutput=append:/var/log/kiranaiq/app.log
StandardError=append:/var/log/kiranaiq/error.log
LimitNOFILE=65535

# Sandboxing
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
systemctl daemon-reload
systemctl enable kiranaiq
systemctl start kiranaiq
systemctl status kiranaiq
```

---

## 5. Nginx Reverse Proxy & TLS Setup

Install Nginx and Certbot:
```bash
apt update
apt install -y nginx certbot python3-certbot-nginx
```

Create `/etc/nginx/sites-available/kiranaiq`:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name kirana.example.com;

    # Redirect all HTTP requests to HTTPS
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name kirana.example.com;

    # SSL Certificates managed by Certbot
    # ssl_certificate /etc/letsencrypt/live/kirana.example.com/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/kirana.example.com/privkey.pem;
    # include /etc/letsencrypt/options-ssl-nginx.conf;
    # ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 10M;

    # Security Headers
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Timeouts for Gemini vision scanning and file uploads
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
```

Enable the configuration and obtain TLS certificate:
```bash
ln -s /etc/nginx/sites-available/kiranaiq /etc/nginx/sites-enabled/
nginx -t
certbot --nginx -d kirana.example.com
systemctl reload nginx
```

---

## 6. Automated Hot Database Backups

KiranaIQ provides an online, crash-consistent backup utility (`scripts/backup_db.py`) leveraging SQLite's native `sqlite3.Connection.backup()` API. It runs with zero downtime, does not lock concurrent readers or writers, validates SQLite integrity on the destination copy, and prunes older archives.

Configure a daily cron job:
```bash
sudo crontab -u kiranaiq -e
```

Add the following line (runs daily at 2:00 AM IST, keeping 14 days of backups):
```cron
0 2 * * * /opt/kiranaiq/venv/bin/python /opt/kiranaiq/scripts/backup_db.py --dest-dir /var/backups/kiranaiq --keep 14 >> /var/log/kiranaiq/backup.log 2>&1
```

To run a manual on-demand backup:
```bash
sudo -u kiranaiq /opt/kiranaiq/venv/bin/python /opt/kiranaiq/scripts/backup_db.py --dest-dir /var/backups/kiranaiq --keep 14
```

---

## 7. Meta WhatsApp Cloud API Setup Guide

Follow these steps to connect your WhatsApp Business account with KiranaIQ:

### Step 7.1: Create Meta Developer App
1. Go to [Meta for Developers](https://developers.facebook.com/) and register / sign in.
2. Click **My Apps** > **Create App**.
3. Select **Other** > **Business** app type.
4. Name the app (e.g., `KiranaIQ-Store`) and associate it with your Meta Business Account.

### Step 7.2: Configure WhatsApp Product
1. In the App Dashboard left sidebar, find **Add Product** and click **WhatsApp** > **Set up**.
2. Under **WhatsApp** > **API Setup**:
   - You will see a test phone number and a temporary access token.
   - Note the **Phone Number ID** (e.g., `105938472910482`). Paste this into `.env` as `WHATSAPP_PHONE_NUMBER_ID`.

### Step 7.3: Create Permanent System User Token
*Temporary tokens expire in 24 hours. Production requires a permanent System User token:*
1. Open [Meta Business Suite](https://business.facebook.com/) > **Business Settings**.
2. Navigate to **Users** > **System Users**.
3. Click **Add**, name it `kiranaiq-system-user`, and assign the role **Admin**.
4. Click **Generate New Token**:
   - Select your KiranaIQ app.
   - Check the `whatsapp_business_messaging` and `whatsapp_business_management` permissions.
   - Set token expiration to **Never**.
5. Copy the generated token immediately. Paste into `.env` as `WHATSAPP_ACCESS_TOKEN`.

### Step 7.4: Register & Verify Store Phone Number
1. In the WhatsApp API Setup page, scroll to **Step 5: Add a phone number**.
2. Enter your store's WhatsApp number.
3. Complete SMS / voice call verification OTP.
4. This becomes your live sender number. Add this number and authorized merchant numbers to `WHATSAPP_ALLOWLIST` in `.env`.

### Step 7.5: Configure Webhook
1. In Meta App Dashboard, navigate to **WhatsApp** > **Configuration**.
2. Click **Edit** next to **Webhook**:
   - **Callback URL:** `https://kirana.example.com/webhook/whatsapp`
   - **Verify Token:** Enter the value you set for `WHATSAPP_VERIFY_TOKEN` in `.env`.
3. Click **Verify and Save**. KiranaIQ will respond to the GET verification request.
4. Under **Webhook fields**, click **Manage** and subscribe to **`messages`**.

### Step 7.6: Submit WhatsApp Message Template
To send proactive price gap notifications, register a template in Meta Business Suite:
1. Open WhatsApp Manager > **Account Tools** > **Message Templates**.
2. Click **Create Template**:
   - Category: **Utility**
   - Name: `purchase_rate_review`
   - Language: `en` (English)
3. Body text format:
   ```text
   Price alert for {{1}}: Rate is Rs {{2}}/{{3}}, which is {{4}}% higher than past purchase of Rs {{5}}/{{6}} from {{7}}.
   ```
4. Submit for review (approval usually takes under 1 hour).
5. Once approved, outgoing alerts will dispatch automatically when rate gaps exceed `ALERT_MIN_GAP_PCT` (2.0%).

---

## 8. Verification & Operational Runbook

### Health Check Probe
Verify live application and SQLite database connectivity:
```bash
curl -s https://kirana.example.com/health | jq .
```
Expected response:
```json
{
  "status": "ok",
  "database": "ok",
  "environment": "production",
  "gemini_model": "gemini-3.8-flash",
  "mock_gemini": false,
  "mock_whatsapp": false
}
```

### Checking Application Logs
```bash
tail -f /var/log/kiranaiq/app.log
journalctl -u kiranaiq -f
```

### Retrying Failed Scans
If an image scan fails due to a temporary network blip or unreadable receipt:
1. **Via Web Dashboard:** Navigate to `https://kirana.example.com/bills/{bill_id}` and click **"Retry AI Scan"**.
2. **Via REST API:**
   ```bash
   curl -X POST https://kirana.example.com/bills/{bill_id}/retry \
        -H "X-API-Key: YOUR_API_KEY"
   ```
   This resets the bill status to `QUEUED`, purges incomplete lines, and triggers the Gemini scanner worker immediately.

### Database File Maintenance
SQLite WAL mode checkpoints automatically. To manually inspect size and tables:
```bash
sqlite3 /opt/kiranaiq/bills.db "PRAGMA journal_mode; PRAGMA page_count; PRAGMA freelist_count;"
```
