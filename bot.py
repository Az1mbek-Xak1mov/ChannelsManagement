import logging
import time
from typing import Optional, Tuple
import asyncio
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from config import BOT_TOKEN, SOURCE_CHANNEL, DESTINATION_CHANNEL, OPENAI_API_KEY
from openai_processor import process_text


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 15


def build_source_web_url(source_channel: str) -> str:
    channel_username = source_channel.strip().lstrip("@")
    return f"https://t.me/s/{channel_username}"


def _extract_photo_url(photo_style: str) -> Optional[str]:
    # Example style: background-image:url('https://...jpg')
    match = re.search(r"url\((['\"]?)(.*?)\1\)", photo_style)
    if not match:
        return None
    return match.group(2)


def scrape_latest_post(source_channel: str) -> Optional[Tuple[int, str, Optional[str], Optional[str]]]:
    """Scrape latest post ID, text, and media info from the public Telegram web view."""
    url = build_source_web_url(source_channel)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to fetch source channel page: %s", exc)
        return None

    try:
        soup = BeautifulSoup(response.text, "html.parser")
        message_blocks = soup.select("div.tgme_widget_message_wrap")
        if not message_blocks:
            logger.warning("No message blocks found on source page")
            return None

        latest_block = message_blocks[-1]
        message_el = latest_block.select_one("div.tgme_widget_message")
        if not message_el:
            logger.warning("Latest message element not found")
            return None

        data_post = message_el.get("data-post", "")
        if "/" not in data_post:
            logger.warning("Missing or invalid data-post attribute")
            return None

        message_id_str = data_post.rsplit("/", 1)[-1]
        message_id = int(message_id_str)

        text_el = latest_block.select_one("div.tgme_widget_message_text")
        message_text = text_el.get_text("\n", strip=True) if text_el else ""

        media_type: Optional[str] = None
        media_url: Optional[str] = None

        video_source_el = latest_block.select_one("video source")
        video_el = latest_block.select_one("video")
        if video_source_el and video_source_el.get("src"):
            media_type = "video"
            media_url = urljoin("https://t.me", video_source_el.get("src"))
        elif video_el and video_el.get("src"):
            media_type = "video"
            media_url = urljoin("https://t.me", video_el.get("src"))
        else:
            photo_wrap_el = latest_block.select_one("a.tgme_widget_message_photo_wrap")
            if photo_wrap_el:
                style_value = photo_wrap_el.get("style", "")
                extracted = _extract_photo_url(style_value)
                if extracted:
                    media_type = "photo"
                    media_url = urljoin("https://t.me", extracted)

        return message_id, message_text, media_type, media_url
    except (ValueError, AttributeError) as exc:
        logger.warning("Failed to parse latest message: %s", exc)
        return None


def send_post_to_destination(
    bot_token: str,
    destination_channel: str,
    text: str,
    media_type: Optional[str] = None,
    media_url: Optional[str] = None,
) -> bool:
    """Send post (text/media) to destination channel via Telegram Bot API."""
    def _send(send_url: str, payload: dict) -> tuple[bool, str]:
        try:
            response = requests.post(send_url, data=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                return False, str(data)
            return True, "ok"
        except (requests.RequestException, ValueError) as exc:
            return False, str(exc)

    if media_type == "photo" and media_url:
        ok, err = _send(
            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
            {
                "chat_id": destination_channel,
                "photo": media_url,
                **({"caption": text} if text.strip() else {}),
            },
        )
        if ok:
            return True
        logger.warning("sendPhoto failed, fallback to text. Error: %s", err)

    elif media_type == "video" and media_url:
        ok, err = _send(
            f"https://api.telegram.org/bot{bot_token}/sendVideo",
            {
                "chat_id": destination_channel,
                "video": media_url,
                **({"caption": text} if text.strip() else {}),
            },
        )
        if ok:
            return True
        logger.warning("sendVideo failed, fallback to text. Error: %s", err)

    if not text.strip():
        logger.info("Post has no text and media upload failed/unsupported, skipping send")
        return False

    ok, err = _send(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        {"chat_id": destination_channel, "text": text},
    )
    if not ok:
        logger.error("Failed to send message to destination: %s", err)
    return ok


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing in .env")
    if not SOURCE_CHANNEL:
        raise RuntimeError("SOURCE_CHANNEL is missing in .env")
    if not DESTINATION_CHANNEL:
        raise RuntimeError("DESTINATION_CHANNEL is missing in .env")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")

    logger.info("Starting monitor")
    logger.info("Source: %s", SOURCE_CHANNEL)
    logger.info("Destination: %s", DESTINATION_CHANNEL)

    last_seen_message_id: Optional[int] = None

    while True:
        try:
            latest = scrape_latest_post(SOURCE_CHANNEL)
            if latest is None:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            message_id, message_text, media_type, media_url = latest

            if last_seen_message_id is None:
                last_seen_message_id = message_id
                logger.info("Initialized last seen message ID: %s", message_id)
            elif message_id > last_seen_message_id:
                logger.info("New post detected: %s", message_id)
                processed_text = ""
                if message_text.strip():
                    processed = asyncio.run(process_text(message_text))
                    if processed is None:
                        logger.info("Post %s skipped by OpenAI filter", message_id)
                        last_seen_message_id = message_id
                        time.sleep(CHECK_INTERVAL_SECONDS)
                        continue
                    processed_text = processed

                sent = send_post_to_destination(
                    BOT_TOKEN,
                    DESTINATION_CHANNEL,
                    processed_text,
                    media_type=media_type,
                    media_url=media_url,
                )
                if sent:
                    logger.info("Forwarded new post %s to destination", message_id)
                last_seen_message_id = message_id
            else:
                logger.debug("No new posts")

        except Exception as exc:
            logger.exception("Unexpected error in polling loop: %s", exc)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
