import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE_URL = "postgresql+psycopg://transcode:transcode@127.0.0.1:5433/transcode"


def load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def database_url() -> str:
    load_env()
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def storage_dir() -> Path:
    return Path(os.environ.get("STORAGE_DIR", ROOT / "storage"))


def session_secret() -> bytes:
    raw = os.environ.get("SESSION_SECRET", "dev-only-change-me")
    return raw.encode()


def cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "0") == "1"


MAX_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_TTL_SECONDS = 15 * 60
ALLOWED_TYPES = {"video/mp4", "video/quicktime", "video/webm"}
