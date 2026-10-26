import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from app.settings import UPLOAD_TTL_SECONDS, session_secret


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_hex, digest_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def new_id() -> str:
    return secrets.token_urlsafe(32)


def sign_upload(job_id: int, exp: int, size: int, content_type: str) -> str:
    message = f"{job_id}.{exp}.{size}.{content_type}".encode()
    return hmac.new(session_secret(), message, hashlib.sha256).hexdigest()


def upload_url(job_id: int, size: int, content_type: str) -> str:
    exp = int((datetime.now(timezone.utc) + timedelta(seconds=UPLOAD_TTL_SECONDS)).timestamp())
    sig = sign_upload(job_id, exp, size, content_type)
    query = urlencode({"exp": exp, "size": size, "type": content_type, "sig": sig})
    return f"/api/storage/{job_id}?{query}"


def valid_upload(job_id: int, exp: int, size: int, content_type: str, sig: str) -> bool:
    if int(datetime.now(timezone.utc).timestamp()) > exp:
        return False
    expected = sign_upload(job_id, exp, size, content_type)
    return hmac.compare_digest(expected, sig)
