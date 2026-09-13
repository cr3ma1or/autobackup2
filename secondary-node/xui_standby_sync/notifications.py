"""Best-effort Telegram notifications."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .models import NotificationConfig

logger = logging.getLogger(__name__)


def send_telegram(
    message: str, config: NotificationConfig, *, timeout: int = 10
) -> bool:
    if not config.enabled or not config.bot_token or not config.chat_id:
        return False
    url = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": config.chat_id, "text": message}
    ).encode("utf-8")
    request = urllib.request.Request(url, data=payload)
    opener = urllib.request.build_opener()
    if config.proxy_url:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {"http": config.proxy_url, "https": config.proxy_url}
            )
        )
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            logger.warning("Telegram API returned an unsuccessful response")
            return False
        return True
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as error:
        logger.warning("Telegram notification failed: %s", error)
        return False
