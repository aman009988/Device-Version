# device-version-service

Python microservice that replaces the PHP `GET /feed/deviceVersion/:ip(/:protocol)` API.  
Handles 10 000+ simultaneous device reboots without DB connection exhaustion by using  
Redis for caching and RabbitMQ for async DB writes.

---

## Table of Contents

1. [Why this service exists](#1-why-this-service-exists)
2. [Architecture overview](#2-architecture-overview)
3. [Request flow — step by step](#3-request-flow--step-by-step)
4. [Project structure](#4-project-structure)
5. [File responsibilities](#5-file-responsibilities)
6. [Environment variables](#6-environment-variables)
7. [Prerequisites](#7-prerequisites)
8. [Setup guide](#8-setup-guide)
9. [Running the service](#9-running-the-service)
10. [Verifying it works](#10-verifying-it-works)
11. [Tuning parameters](#11-tuning-parameters)
12. [How each worker operates](#12-how-each-worker-operates)
13. [API reference](#13-api-reference)
14. [Nginx integration](#14-nginx-integration)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Why this service exists

Every device calls `GET /feed/deviceVersion/<MAC>` every **30 seconds**.  
On device reboot it calls `GET /feed/deviceVersion/<MAC>/true?start=true`.

### Problem with the original PHP API

When 10 000 devices reboot simultaneously the PHP handler does — synchronously, per request:

| Operation | Type | Impact at 10k |
|---|---|---|
| `SELECT` from `device` table by MAC | DB read | 10 000 simultaneous connections |
| `UPDATE device SET connectionstatus` | DB write | 10 000 individual writes/sec |
| `UPDATE device SET lastxmlgeneratedon` | DB write | 10 000 individual writes/sec |
| `INSERT INTO devicelog` ("App Started") | DB write | 10 000 inserts/sec |
| Trigger XML generation | HTTP call | 10 000 HTTP calls |

Result: PostgreSQL connection pool exhausted → timeouts → cascading failures → devices show offline.

### Solution

| Old (PHP synchronous) | New (Python async) |
|---|---|
| 10 000 SELECT queries | Redis cache → **0 DB reads** on warm cache |
| 10 000 UPDATE connectionstatus | Heartbeat batcher → **1 bulk UPDATE per 500 ms** |
| 10 000 INSERT devicelog | RabbitMQ queue, max 50 concurrent DB writes |
| 10 000 UPDATE lastxmlgeneratedon | RabbitMQ queue, controlled rate |
| Response blocked until all DB writes finish | Response returned in **< 1 ms**, writes happen after |

---

## 2. Architecture overview

```
Device (every 30s)
        │
        ▼
   Nginx Layer
   ┌─────────────────────────────────────────────────┐
   │  GET /deviceVersion/<mac>                       │
   │  → .txt file exists? → return file content      │
   │  → file missing     → forward to Python service │
   │                                                 │
   │  GET /deviceVersion/<mac>/true?start=true       │
   │  → always forward to Python service             │
   └────────────────────┬────────────────────────────┘
                        │
                        ▼
          ┌─────────────────────────┐
          │  FastAPI Producer       │  port 8000
          │  (4 uvicorn workers)    │
          │                         │
          │  1. Normalize MAC       │
          │  2. Redis cache lookup  │──── HIT ──► return {"desc": version}
          │  3. DB lookup on miss   │
          │  4. Publish to RabbitMQ │  (fire-and-forget)
          │  5. Return response     │
          └────────┬────────────────┘
                   │ publishes to 4 queues
        ┌──────────┴──────────────────────────────┐
        │                                         │
        ▼                                         ▼
  device.heartbeat                         device.reboot
  device.xml_generate                    device.time_update
        │
        ▼
  ┌─────────────────────────────────────────────────┐
  │  Consumer Service  (single asyncio process)     │
  │                                                 │
  │  ┌──────────────────────────────────────────┐   │
  │  │ heartbeat_batcher                        │   │
  │  │  500ms window → 1 bulk UPDATE per batch  │   │
  │  └──────────────────────────────────────────┘   │
  │  ┌──────────────────────────────────────────┐   │
  │  │ reboot_worker  (prefetch=50)             │   │
  │  │  INSERT devicelog + UPDATE device        │   │
  │  │  → publishes to device.xml_generate      │   │
  │  └──────────────────────────────────────────┘   │
  │  ┌──────────────────────────────────────────┐   │
  │  │ xml_gen_worker (prefetch=10)             │   │
  │  │  Redis NX lock → POST to PHP getfeeds3   │   │
  │  └──────────────────────────────────────────┘   │
  │  ┌──────────────────────────────────────────┐   │
  │  │ time_update_worker                       │   │
  │  │  UPDATE device.raw_time_on_device        │   │
  │  └──────────────────────────────────────────┘   │
  └────────────────────────────────────────────┬────┘
                                               │
                                    ┌──────────▼──────────┐
                                    │    PostgreSQL DB    │
                                    │  (same DB as PHP)   │
                                    └─────────────────────┘
```

---

## 3. Request flow — step by step

### Normal heartbeat  `GET /feed/deviceVersion/<mac>`

```
1. Normalize MAC  →  lower-case, strip colons/spaces
2. Redis GET device:mac:<mac>
   └─ HIT  → skip DB entirely
   └─ MISS → SELECT from device table → store in Redis (TTL 300s)
3. Check device found?
   └─ NO  → fetch latest Windows client release (cached 1hr) → return error JSON
4. Publish {device_id} to device.heartbeat queue  (async, non-blocking)
5. If ?time= passed → publish to device.time_update queue
6. Return {"desc": "<updateversion>"}  immediately
```

### Reboot call  `GET /feed/deviceVersion/<mac>/true?start=true`

```
1–3. Same as above
4.   Determine generate_xml:
     └─ lastxmlgeneratedon < today midnight → True  (also update Redis cache eagerly)
     └─ protocol == "true"                 → True
5.   Publish to device.heartbeat  (always)
6.   Publish to device.reboot  (because ?start= present)
     payload: {device_id, mac, server_id, firstconnected, raw_time, generate_xml}
7.   If protocol == "true":
     └─ resolve storage host + bucket for this server_id
     └─ RSA-encrypt host and bucket (same key as PHP)
     └─ Return {"desc": version, "h": encrypted_host, "b": encrypted_bucket}
     else:
     └─ Return {"desc": version}
```

### Consumer — reboot_worker processes the queue message

```
1. INSERT INTO devicelog (deviceid, NOW(), 'App Started')
2. UPDATE device SET
     lastxmlgeneratedon  = today 00:00:00
     mc_last_notified_at = NULL
     connectionstatus    = NOW()
     [firstconnected = NOW()  if was NULL]
     [raw_time_on_device, time_on_device  if ?time= was passed]
3. If generate_xml == True:
   → publish {device_id, mac} to device.xml_generate queue
```

### Consumer — xml_gen_worker processes the queue message

```
1. Redis SET NX  xmlgen:lock:<device_id>  TTL=60s
   └─ lock exists → skip (another worker already handling this device)
   └─ acquired    → POST to PHP_QUEUE_SERVER/single
                    or GET PHP_APIPATH/getfeeds3/<mac>
```

---

## 4. Project structure

```
device-version-service/
│
├── main.py                      # FastAPI app — the HTTP producer
├── config.py                    # All config read from .env
├── db.py                        # SQLAlchemy ORM models + async queries
├── cache.py                     # Redis helpers (device cache + dedup lock)
├── queue.py                     # RabbitMQ publisher (fire-and-forget)
├── crypto.py                    # RSA encrypt (port of PHP openSSlPasswordEncrypterForFeedRouter)
├── storage.py                   # Resolve host/bucket by serverid (port of PHP fx_set_storage_type)
│
├── workers/
│   ├── __init__.py
│   ├── consumer_main.py         # Entrypoint — starts all workers, auto-restarts on crash
│   ├── heartbeat_batcher.py     # Consumes device.heartbeat, bulk UPDATE connectionstatus
│   ├── reboot_worker.py         # Consumes device.reboot, INSERT/UPDATE, triggers XML gen
│   ├── xml_gen_worker.py        # Consumes device.xml_generate, calls PHP getfeeds3
│   └── time_update_worker.py    # Consumes device.time_update, UPDATE time columns
│
├── Dockerfile.producer          # Image for the FastAPI service
├── Dockerfile.consumer          # Image for the worker service
├── docker-compose.yml           # Defines: producer, consumer, redis, rabbitmq
├── .env.example                 # Template — copy to .env and fill in values
├── requirements.txt             # Python dependencies
└── keys/                        # NOT committed — you create this directory
    └── public_third_key.pem     # Copied from PHP: api/v1/vendor/RSA_KEY/public_third_key.pem
```

---

## 5. File responsibilities

| File | Responsibility |
|---|---|
| `main.py` | HTTP route handler. Reads from Redis, publishes to RabbitMQ, returns response immediately. Never writes to DB directly. |
| `db.py` | SQLAlchemy async ORM. Models: `Device`, `DeviceLog`, `ClientRelease`. All parameterised queries — no raw SQL. |
| `cache.py` | Redis read/write for device cache (`device:mac:<mac>`, TTL 300s), client release cache (TTL 1hr), xml-gen dedup lock (`xmlgen:lock:<id>`, TTL 60s). |
| `queue.py` | RabbitMQ publisher. All 4 queues declared durable. Errors logged but never propagated to HTTP response. |
| `crypto.py` | `rsa_encrypt(value)` — loads `public_third_key.pem`, encrypts with PKCS1v15, returns base64. Identical output to PHP `openssl_public_encrypt`. |
| `storage.py` | `resolve_host_and_bucket(server_id)` — returns storage host + bucket path for minio/azure/s3 based on server ID. Port of PHP `fx_set_storage_type_using_serverid`. |
| `workers/heartbeat_batcher.py` | Time-windowed batch consumer. Collects device IDs for 500ms, fires one `UPDATE device SET connectionstatus = NOW() WHERE id = ANY(...)`. |
| `workers/reboot_worker.py` | Per-message consumer (prefetch=50). Runs `INSERT devicelog` + `UPDATE device` + optionally publishes to xml_generate queue. |
| `workers/xml_gen_worker.py` | Calls PHP feed generation. Redis `SET NX` dedup ensures one generation per device per 60s window. |
| `workers/time_update_worker.py` | Updates `raw_time_on_device` / `time_on_device` columns for non-reboot `?time=` calls. |
| `workers/consumer_main.py` | Runs all 4 workers concurrently in one asyncio event loop. Auto-restarts any crashed worker with exponential back-off. |

---

## 6. Environment variables

All variables live in `.env`. Every container reads from it via `env_file: .env` in `docker-compose.yml`.

| Variable | Example | Description |
|---|---|---|
| `DB_HOST` | `127.0.0.1` | PostgreSQL host (same DB as PHP backend) |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | `hubpro` | Database name |
| `DB_USER` | `postgres` | Database user |
| `DB_PASSWORD` | `secret` | Database password |
| `DB_POOL_MIN` | `5` | Min DB connections per service |
| `DB_POOL_MAX` | `20` | Max DB connections (producer); `10` for consumer |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection URL |
| `DEVICE_CACHE_TTL` | `300` | Seconds to cache device row in Redis |
| `CLIENT_RELEASE_CACHE_TTL` | `3600` | Seconds to cache Windows client release |
| `XML_GEN_LOCK_TTL` | `60` | Seconds for xml-gen dedup lock per device |
| `RABBITMQ_URL` | `amqp://guest:guest@rabbitmq:5672/` | RabbitMQ connection URL (used by app) |
| `RABBITMQ_DEFAULT_USER` | `guest` | RabbitMQ admin user (used by rabbitmq container) |
| `RABBITMQ_DEFAULT_PASS` | `guest` | RabbitMQ admin password |
| `LIVEURL` | `https://hub.example.com` | Same as PHP `LIVEURL` constant |
| `AWS_BUCKET_NAME` | `my-bucket` | Same as PHP `awsBucketName` |
| `AZURE_ACCOUNT_NAME` | `myaccount` | Azure storage account name (leave blank if not used) |
| `MINIO_SERVER` | `http://minio:9000` | MinIO server URL (leave blank if not used) |
| `SERVERS_USING_AZURE_STORAGE` | `{"minio":[1,2],"azure":[3]}` | JSON map of storage type → list of serverids |
| `SERVER_IDS_FOR_SEPARATE_BUCKET` | `4,5` | Comma-separated serverids that use a separate bucket |
| `RSA_PUBLIC_KEY_PATH` | `/app/keys/public_third_key.pem` | Path inside container (do not change) |
| `PHP_QUEUE_SERVER` | `http://php-host/queue` | PHP queue service URL (`QUEUE_SERVER` in PHP config). Leave blank to use `PHP_APIPATH` fallback. |
| `PHP_APIPATH` | `https://hub.example.com/api/v1/` | PHP API base path (`APIPATH` in PHP config) |
| `SUPPORT_USER_EMAIL` | `support@example.com` | Email returned in device-not-found response |
| `SUPPORT_PHONE` | `1-877-525-1995` | Phone returned in device-not-found response |
| `HEARTBEAT_BATCH_WINDOW_MS` | `500` | Milliseconds to accumulate heartbeat messages before flushing |
| `HEARTBEAT_BATCH_MAX_SIZE` | `500` | Max messages per heartbeat batch regardless of window |
| `REBOOT_WORKER_PREFETCH` | `50` | Max concurrent reboot messages being processed at once |

---

## 7. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Docker | 24+ | `docker --version` |
| Docker Compose | v2 (plugin) | `docker compose version` |
| PostgreSQL | existing | The same DB the PHP backend uses — no migration needed |
| PHP RSA key | — | `api/v1/vendor/RSA_KEY/public_third_key.pem` from the PHP project |

No Python installation needed on the host — everything runs inside Docker.

---

## 8. Setup guide

### Step 1 — Clone / navigate to the service

```bash
cd /path/to/python-services/device-version-service
```

### Step 2 — Copy the RSA public key

This is the same key the PHP backend uses for encrypting storage credentials.  
It lives at `api/v1/vendor/RSA_KEY/public_third_key.pem` in the PHP project.

```bash
mkdir -p keys
cp /path/to/hub-backend-php/api/v1/vendor/RSA_KEY/public_third_key.pem keys/
```

On **this machine** the exact command is:
```bash
mkdir -p keys
cp /home/in-aman-r/Aman/python-services/hub-backend-php/api/v1/vendor/RSA_KEY/public_third_key.pem keys/
```

### Step 3 — Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in every value. At minimum you **must** set:

```env
DB_HOST=<your PostgreSQL host>
DB_NAME=<your DB name>
DB_USER=<your DB user>
DB_PASSWORD=<your DB password>
LIVEURL=<your hub domain, e.g. https://hub.example.com>
AWS_BUCKET_NAME=<your bucket name>
PHP_APIPATH=<your PHP API base URL, e.g. https://hub.example.com/api/v1/>
RABBITMQ_DEFAULT_USER=<choose a username>
RABBITMQ_DEFAULT_PASS=<choose a strong password>
RABBITMQ_URL=amqp://<user>:<pass>@rabbitmq:5672/
SUPPORT_USER_EMAIL=<your support email>
```

> **Important:** `RABBITMQ_URL` must use the same user/pass as `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS`.

### Step 4 — Configure storage type mapping

Set `SERVERS_USING_AZURE_STORAGE` to match your PHP `SERVERS_USING_AZURE_STORAGE` constant.

```env
# All servers use MinIO:
SERVERS_USING_AZURE_STORAGE={"minio":[1,2,3,4,5]}

# Mixed:
SERVERS_USING_AZURE_STORAGE={"minio":[1,2],"azure":[3,4]}

# All use S3 (default):
SERVERS_USING_AZURE_STORAGE={}
```

### Step 5 — Configure PHP XML generation

The `xml_gen_worker` needs to know how to trigger feed generation on the PHP side.

**If you use QUEUE_SERVER (recommended):**
```env
PHP_QUEUE_SERVER=http://your-queue-server-host
```

**If no queue server (direct call):**
```env
PHP_QUEUE_SERVER=
PHP_APIPATH=https://hub.example.com/api/v1/
```

---

## 9. Running the service

### Start everything

```bash
docker compose up -d
```

This starts 4 containers:

| Container | Role | Port |
|---|---|---|
| `producer` | FastAPI HTTP server | `8000` |
| `consumer` | All 4 workers (heartbeat, reboot, xml_gen, time_update) | — |
| `redis` | Cache layer | `6379` |
| `rabbitmq` | Message queue | `5672` (AMQP), `15672` (management UI) |

### Build images first (or after code changes)

```bash
docker compose build
docker compose up -d
```

### View logs

```bash
# All containers
docker compose logs -f

# Only producer
docker compose logs -f producer

# Only consumer (all workers)
docker compose logs -f consumer
```

### Stop the service

```bash
docker compose down
```

### Stop and remove all data (Redis cache, RabbitMQ queues)

```bash
docker compose down -v
```

---

## 10. Verifying it works

### Health check

```bash
curl http://localhost:8000/health
# Expected: {"status":"ok"}
```

### Simulate a device heartbeat

```bash
curl "http://localhost:8000/feed/deviceVersion/AA:BB:CC:DD:EE:FF"
# Expected: {"desc":"<updateversion>"}
# or if device not found: {"desc":"device not found","status":"error","code":19008,...}
```

### Simulate a device reboot

```bash
curl "http://localhost:8000/feed/deviceVersion/AA:BB:CC:DD:EE:FF/true?start=true"
# Expected: {"desc":"<updateversion>","h":"<encrypted_host>","b":"<encrypted_bucket>"}
```

### Check RabbitMQ queues

Open the RabbitMQ management UI at `http://localhost:15672`  
Login with your `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS`.

You should see 4 queues:
- `device.heartbeat`
- `device.reboot`
- `device.xml_generate`
- `device.time_update`

### Check Redis cache

```bash
docker compose exec redis redis-cli
> KEYS device:mac:*
> GET device:mac:aabbccddeeff
```

---

## 11. Tuning parameters

| Parameter | Default | When to change |
|---|---|---|
| `HEARTBEAT_BATCH_WINDOW_MS` | `500` | Lower for more frequent DB updates; raise if DB is still under pressure |
| `HEARTBEAT_BATCH_MAX_SIZE` | `500` | Raise if you have > 10k simultaneous heartbeats per 500ms |
| `REBOOT_WORKER_PREFETCH` | `50` | Lower if DB CPU spikes during mass reboot; raise if queue backs up too slowly |
| `DEVICE_CACHE_TTL` | `300` | Controls how stale `updateversion` can be. Lower = fresher data, more DB reads |
| `XML_GEN_LOCK_TTL` | `60` | Dedup window for XML generation. Should be ≥ time PHP getfeeds3 takes |
| `DB_POOL_MAX` (producer) | `20` | Increase if Redis goes down and all requests fall back to DB simultaneously |

---

## 12. How each worker operates

### heartbeat_batcher

- Maintains a 500ms sliding window
- Collects all `device_id` values from `device.heartbeat` messages
- Deduplicates within the window (one entry per device ID)
- Fires a single SQL: `UPDATE device SET connectionstatus = NOW() WHERE id = ANY([...])`
- ACKs all messages after successful DB write; NACKs + requeues on failure

### reboot_worker

- Prefetch = 50 → maximum 50 simultaneous DB writes at any moment
- Per message: 
  1. `INSERT INTO devicelog` (App Started)
  2. `UPDATE device` (lastxmlgeneratedon, mc_last_notified_at, connectionstatus, optionally firstconnected + time fields)
  3. If `generate_xml == True` → publish to `device.xml_generate`
- Message is ACKed only after all DB writes succeed

### xml_gen_worker

- Prefetch = 10 (intentionally low — PHP getfeeds3 is heavy)
- Acquires Redis `SET NX` lock per device_id (TTL = 60s)
- If lock already held → ACK and skip (de-duplicate)
- If acquired → POST to `PHP_QUEUE_SERVER/single` or GET `PHP_APIPATH/getfeeds3/<mac>`
- On HTTP failure → raises exception → message NACKed and requeued

### time_update_worker

- Prefetch = 100
- Parses `raw_time` string into `time_on_device` datetime
- `UPDATE device SET raw_time_on_device, time_on_device WHERE id = <device_id>`

---

## 13. API reference

### `GET /feed/deviceVersion/{ip}`  
### `GET /feed/deviceVersion/{ip}/{protocol}`

| Parameter | Location | Required | Description |
|---|---|---|---|
| `ip` | URL | Yes | Device MAC address (any format: `AA:BB:CC:DD:EE:FF`, `aabbccddeeff`, etc.) |
| `protocol` | URL | No | Pass `true` to request encrypted storage credentials (sent on first call after reboot) |
| `did` | Query string | No | Device DB integer ID. If present, MAC is looked up from DB instead of URL |
| `time` | Query string | No | Current time on device (for clock-drift tracking, e.g. `2026-04-02 10:30:00`) |
| `start` | Query string | No | Any non-empty value triggers "App Started" log insert |

**Response — device found, normal heartbeat:**
```json
{"desc": "1743000000"}
```

**Response — device found, protocol=true (reboot):**
```json
{
  "desc": "1743000000",
  "h": "<RSA-encrypted-base64-host>",
  "b": "<RSA-encrypted-base64-bucket>"
}
```

**Response — device not found:**
```json
{
  "desc": "device not found",
  "status": "error",
  "code": 19008,
  "path": "latest-client-setup.exe",
  "client": "4.2.1",
  "login": false,
  "signup": false,
  "phone": "1-877-525-1995",
  "email": "support@example.com",
  "extn": "1"
}
```

**Response headers (always):**
```
Cache-Control: no-store, no-cache, must-revalidate, max-age=0
Pragma: no-cache
```

### `GET /health`

Returns `{"status": "ok"}`. Used by load balancers and Docker health checks.

---

## 14. Nginx integration

No changes needed to Nginx. The existing rules continue to work:

- **Normal heartbeat** — Nginx checks for `.txt` file, serves it if exists, else proxies to this Python service (was PHP, now Python on port 8000).
- **Reboot call** (`/true`) — Nginx always proxies to this Python service.

Update your Nginx `proxy_pass` to point to port `8000`:

```nginx
location ~ ^/api/v1/feed/deviceVersion/ {
    # .txt file shortcut (existing rule)
    set $txt_path /path/to/txt/$1.txt;
    if (-f $txt_path) {
        return 200;  # serve file
    }
    # Forward to Python service
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

---

## 15. Troubleshooting

### Producer container exits immediately

```bash
docker compose logs producer
```

Common causes:
- Missing `.env` file → `cp .env.example .env` and fill values
- `DB_HOST` unreachable from inside Docker → use host IP, not `127.0.0.1` (use `host.docker.internal` on Mac/Windows or the actual server IP on Linux)
- `keys/public_third_key.pem` missing → see [Setup Step 2](#step-2--copy-the-rsa-public-key)

### "device not found" for a device that exists in the DB

- Check the MAC is normalised the same way: `echo "AA:BB:CC:DD:EE:FF" | tr '[:upper:]' '[:lower:]' | tr -d ':'`
- Check the device is `isactive = 1` in the `device` table
- Check `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` in `.env`

### RabbitMQ queues growing but not draining

- Check consumer is running: `docker compose ps consumer`
- Check consumer logs: `docker compose logs -f consumer`
- Check DB connectivity from consumer: `docker compose exec consumer python3 -c "import asyncio, db, config; asyncio.run(db.init_pool()); print('DB OK')"`

### Redis connection refused

- Check `REDIS_URL=redis://redis:6379/0` (uses the Docker service name `redis`, not `127.0.0.1`)
- Check Redis is healthy: `docker compose ps redis`

### RSA encryption fails

```
RuntimeError: RSA public key load failed
```
- The `keys/` directory was not created or the `.pem` file was not copied
- Re-run: `cp /home/in-aman-r/Aman/python-services/hub-backend-php/api/v1/vendor/RSA_KEY/public_third_key.pem keys/`

### DB host is on the same Linux machine (not in Docker)

`127.0.0.1` inside a container refers to the container itself, not the host.  
Use `host.docker.internal` (Docker Desktop) or add `network_mode: host` to the service,  
or use the machine's LAN IP address:

```env
DB_HOST=192.168.1.100
```

mkdir -p keys
cp /path/to/php/vendor/RSA_KEY/public_third_key.pem keys/

docker compose up -d


