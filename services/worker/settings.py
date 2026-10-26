import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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
