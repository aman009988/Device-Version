import os
import json


# ── PostgreSQL ────────────────────────────────────────────────────────────────
DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", 5))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", 20))

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DEVICE_CACHE_TTL = int(os.environ.get("DEVICE_CACHE_TTL", 300))        # 5 min
CLIENT_RELEASE_CACHE_TTL = int(os.environ.get("CLIENT_RELEASE_CACHE_TTL", 3600))  # 1 hr
XML_GEN_LOCK_TTL = int(os.environ.get("XML_GEN_LOCK_TTL", 60))         # 60 s dedup

# ── RabbitMQ ──────────────────────────────────────────────────────────────────
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

QUEUE_HEARTBEAT   = "device.heartbeat"
QUEUE_REBOOT      = "device.reboot"
QUEUE_XML_GENERATE = "device.xml_generate"
QUEUE_TIME_UPDATE = "device.time_update"

# ── Storage ───────────────────────────────────────────────────────────────────
# JSON map: {"minio": [1,2,3], "azure": [4,5], "s3": []}
# If a serverid is not in any list it defaults to s3.
SERVERS_USING_AZURE_STORAGE: dict = json.loads(
    os.environ.get("SERVERS_USING_AZURE_STORAGE", "{}")
)
# Comma-separated server IDs that use a separate bucket
SERVER_IDS_FOR_SEPARATE_BUCKET: list[int] = [
    int(x) for x in os.environ.get("SERVER_IDS_FOR_SEPARATE_BUCKET", "").split(",") if x.strip()
]

LIVEURL          = os.environ["LIVEURL"]                         # e.g. https://hub.example.com
AWS_BUCKET_NAME  = os.environ["AWS_BUCKET_NAME"]
AZURE_ACCOUNT_NAME = os.environ.get("AZURE_ACCOUNT_NAME", "")
MINIO_SERVER     = os.environ.get("MINIO_SERVER", "")

# ── RSA key ───────────────────────────────────────────────────────────────────
# Same .pem that PHP uses: vendor/RSA_KEY/public_third_key.pem
RSA_PUBLIC_KEY_PATH = os.environ.get(
    "RSA_PUBLIC_KEY_PATH", "/app/keys/public_third_key.pem"
)

# ── PHP XML queue / APIPATH ───────────────────────────────────────────────────
# The existing PHP queue server URL (QUEUE_SERVER constant from PHP config)
PHP_QUEUE_SERVER = os.environ.get("PHP_QUEUE_SERVER", "")
# The PHP API base path (APIPATH constant from PHP config)
PHP_APIPATH      = os.environ.get("PHP_APIPATH", "")

# ── Misc ──────────────────────────────────────────────────────────────────────
SUPPORT_USER_EMAIL = os.environ.get("SUPPORT_USER_EMAIL", "support@example.com")
SUPPORT_PHONE      = os.environ.get("SUPPORT_PHONE", "1-877-525-1995")

# Heartbeat batcher settings
HEARTBEAT_BATCH_WINDOW_MS = int(os.environ.get("HEARTBEAT_BATCH_WINDOW_MS", 500))
HEARTBEAT_BATCH_MAX_SIZE  = int(os.environ.get("HEARTBEAT_BATCH_MAX_SIZE", 500))

# Reboot worker concurrency
REBOOT_WORKER_PREFETCH = int(os.environ.get("REBOOT_WORKER_PREFETCH", 50))
