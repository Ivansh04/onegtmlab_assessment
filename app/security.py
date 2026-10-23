import hashlib
import hmac
import time
from urllib.parse import unquote
from fastapi import HTTPException, Request
from app.config import Settings

def verify_hubspot_signature(request: Request, raw_body: bytes, settings: Settings) -> None:
    """Validate HubSpot v3 request signatures when configured for a public app."""
    if not settings.enable_webhook_signature_validation:
        return
    secret = settings.hubspot_client_secret
    # During local development the secret may be absent. Production must configure it.
    if not secret:
        if settings.environment.lower() == "production":
            raise HTTPException(status_code=503, detail="Webhook signature validation is not configured")
        return

    signature = request.headers.get("X-HubSpot-Signature-v3")
    timestamp = request.headers.get("X-HubSpot-Request-Timestamp")
    if not signature or not timestamp:
        raise HTTPException(status_code=401, detail="Missing HubSpot signature headers")
    try:
        timestamp_ms = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid signature timestamp") from exc
    now_ms = int(time.time() * 1000)
    if abs(now_ms - timestamp_ms) > settings.webhook_max_age_seconds * 1000:
        raise HTTPException(status_code=401, detail="Expired webhook signature")

    uri = str(request.url)
    source = request.method.upper() + unquote(uri) + raw_body.decode("utf-8") + timestamp
    digest = hmac.new(secret.encode(), source.encode(), hashlib.sha256).digest()
    import base64
    expected = base64.b64encode(digest).decode()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid HubSpot signature")
