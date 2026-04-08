import logging
import time
from typing import Optional
import asyncio
from pathlib import Path

import requests
from config import (
    ADMIN_USER_IDS,
    BOT_TOKEN,
    DESTINATION_CHANNEL,
    LOG_FILE,
    OPENAI_API_KEY,
    SOURCE_CHANNEL,
)
from openai_processor import process_text


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

POLL_TIMEOUT = 30  # long-polling timeout for getUpdates
MEDIA_GROUP_WAIT = 2  # seconds to wait for more media group messages


class TelegramBot:
    """Telegram Bot API wrapper for channel forwarding."""

    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.offset: Optional[int] = None

    def _call(self, method: str, data: Optional[dict] = None, timeout: int = 60) -> Optional[dict]:
        """Call Telegram Bot API method."""
        url = f"{self.base_url}/{method}"
        try:
            resp = requests.post(url, json=data or {}, timeout=timeout)
            result = resp.json()
            if not result.get("ok"):
                logger.error("API %s failed: %s", method, result.get("description"))
                return None
            return result.get("result")
        except requests.RequestException as exc:
            logger.error("API %s request failed: %s", method, exc)
            return None

    def get_updates(self) -> list[dict]:
        """Long-poll for new updates."""
        data = {
            "timeout": POLL_TIMEOUT,
            "allowed_updates": ["channel_post", "message"],
        }
        if self.offset is not None:
            data["offset"] = self.offset
        result = self._call("getUpdates", data, timeout=POLL_TIMEOUT + 10)
        return result if result else []

    def send_message(self, chat_id: str | int, text: str,
                     parse_mode: Optional[str] = "Markdown") -> bool:
        """Send a text message."""
        payload = {
            "chat_id": chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        result = self._call("sendMessage", payload)
        return result is not None

    def copy_message(self, from_chat: str, to_chat: str, message_id: int,
                     caption: Optional[str] = None) -> bool:
        """Copy a message from one chat to another, optionally with new caption."""
        data = {
            "chat_id": to_chat,
            "from_chat_id": from_chat,
            "message_id": message_id,
        }
        if caption is not None:
            data["caption"] = caption
            data["parse_mode"] = "Markdown"
        result = self._call("copyMessage", data)
        return result is not None

    def send_media_group(self, chat_id: str, media: list[dict]) -> bool:
        """Send a media group (multiple photos/videos)."""
        result = self._call("sendMediaGroup", {
            "chat_id": chat_id,
            "media": media,
        })
        return result is not None


def _get_file_id(message: dict) -> Optional[tuple[str, str]]:
    """Extract (media_type, file_id) from a message.

    Returns:
        ("photo", file_id) or ("video", file_id) or ("document", file_id)
        or ("animation", file_id) or None
    """
    if "photo" in message:
        # photo is an array of sizes, take the largest
        return "photo", message["photo"][-1]["file_id"]
    if "video" in message:
        return "video", message["video"]["file_id"]
    if "animation" in message:
        return "animation", message["animation"]["file_id"]
    if "document" in message:
        return "document", message["document"]["file_id"]
    if "audio" in message:
        return "audio", message["audio"]["file_id"]
    if "voice" in message:
        return "voice", message["voice"]["file_id"]
    if "video_note" in message:
        return "video_note", message["video_note"]["file_id"]
    return None


def _get_text(message: dict) -> str:
    """Get text or caption from a message."""
    return message.get("text") or message.get("caption") or ""


def _get_source_chat_id(message: dict) -> Optional[int]:
    """Get the chat ID from a channel post."""
    chat = message.get("chat", {})
    return chat.get("id")


def _normalize_channel(channel: str) -> str:
    """Normalize channel identifier — ensure @ prefix for usernames."""
    channel = channel.strip()
    if channel.startswith("-100") or channel.startswith("-"):
        return channel  # numeric ID
    if not channel.startswith("@"):
        return f"@{channel}"
    return channel


def _matches_source(message: dict, source_channel: str) -> bool:
    """Check if a channel_post is from our source channel."""
    chat = message.get("chat", {})
    username = chat.get("username", "")
    chat_id = str(chat.get("id", ""))

    source = source_channel.strip().lstrip("@")
    return username == source or chat_id == source or chat_id == f"-100{source}"


def _tail_lines(file_path: str, line_count: int = 10) -> str:
    """Return last N lines from a log file."""
    path = Path(file_path)
    if not path.exists():
        return f"Log file not found: {file_path}"

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = "".join(lines[-line_count:]).strip()
        return tail or "Log file is empty."
    except Exception as exc:
        logger.exception("Failed to read log file: %s", exc)
        return "Failed to read log file."


def handle_admin_check_message(bot: TelegramBot, message: dict) -> bool:
    """Send last 10 log lines to authorized users."""
    user = message.get("from", {})
    user_id = user.get("id")
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip().lower()

    if user_id not in ADMIN_USER_IDS:
        return False

    if text not in {"/logs", "/check", "/status", "logs", "check", "status"}:
        return False

    logs = _tail_lines(LOG_FILE, 10)
    response = f"Last 10 log lines:\n{logs}"
    sent = bot.send_message(chat_id, response, parse_mode=None)
    if sent:
        logger.info("Sent last 10 logs to admin user %s", user_id)
    return sent


def forward_single_message(bot: TelegramBot, message: dict,
                           source_channel: str, dest_channel: str) -> bool:
    """Process and forward a single message (no media group)."""
    original_text = _get_text(message)
    has_media = _get_file_id(message) is not None
    message_id = message.get("message_id")

    # Process text through OpenAI
    processed_text = ""
    if original_text.strip():
        processed = asyncio.run(process_text(original_text))
        if processed is None:
            logger.info("Post %s skipped by OpenAI filter", message_id)
            return False
        processed_text = processed

    if has_media:
        # Copy message with new caption (preserves media via file_id, no download)
        ok = bot.copy_message(
            from_chat=source_channel,
            to_chat=dest_channel,
            message_id=message_id,
            caption=processed_text if processed_text else None,
        )
        if ok:
            logger.info("Forwarded post %s with media to destination", message_id)
            return True
        logger.warning("copyMessage failed for post %s", message_id)
        return False
    else:
        # Text-only message
        if not processed_text.strip():
            logger.info("Post %s has no text content after processing, skipping", message_id)
            return False
        ok = bot.send_message(dest_channel, processed_text)
        if ok:
            logger.info("Forwarded text post %s to destination", message_id)
        return ok


def forward_media_group(bot: TelegramBot, messages: list[dict],
                        source_channel: str, dest_channel: str) -> bool:
    """Process and forward a media group (multiple photos/videos in one post)."""
    # Combine all captions (usually only the first message has a caption)
    combined_text = ""
    for msg in messages:
        t = _get_text(msg)
        if t.strip():
            combined_text = t
            break

    # Process text through OpenAI
    processed_text = ""
    if combined_text.strip():
        processed = asyncio.run(process_text(combined_text))
        if processed is None:
            logger.info("Media group skipped by OpenAI filter")
            return False
        processed_text = processed

    # Build media group using file_ids (no download!)
    media = []
    for idx, msg in enumerate(messages):
        file_info = _get_file_id(msg)
        if not file_info:
            continue
        media_type, file_id = file_info
        entry = {
            "type": media_type,
            "media": file_id,
        }
        # Caption goes only on the first item
        if idx == 0 and processed_text:
            entry["caption"] = processed_text
            entry["parse_mode"] = "Markdown"
        media.append(entry)

    if not media:
        logger.warning("Media group has no extractable media")
        return False

    ok = bot.send_media_group(dest_channel, media)
    if ok:
        logger.info("Forwarded media group (%d items) to destination", len(media))
    else:
        logger.warning("sendMediaGroup failed, trying individual copies")
        # Fallback: copy messages individually
        any_sent = False
        for idx, msg in enumerate(messages):
            caption = processed_text if idx == 0 else None
            if bot.copy_message(source_channel, dest_channel, msg["message_id"], caption):
                any_sent = True
        return any_sent
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

    bot = TelegramBot(BOT_TOKEN)
    source = _normalize_channel(SOURCE_CHANNEL)
    dest = _normalize_channel(DESTINATION_CHANNEL)

    logger.info("Starting bot (Bot API polling mode)")
    logger.info("Source: %s", source)
    logger.info("Destination: %s", dest)
    logger.info("Admin check enabled for user IDs: %s", sorted(ADMIN_USER_IDS))

    # Buffer for media groups: {media_group_id: [messages]}
    media_group_buffer: dict[str, list[dict]] = {}
    media_group_times: dict[str, float] = {}

    while True:
        try:
            updates = bot.get_updates()

            for update in updates:
                # Advance offset so we don't re-process
                update_id = update["update_id"]
                bot.offset = update_id + 1

                message = update.get("message")
                if message:
                    handle_admin_check_message(bot, message)
                    continue

                channel_post = update.get("channel_post")
                if not channel_post:
                    continue

                if not _matches_source(channel_post, SOURCE_CHANNEL):
                    logger.debug("Ignoring post from non-source channel")
                    continue

                msg_id = channel_post.get("message_id")
                media_group_id = channel_post.get("media_group_id")

                if media_group_id:
                    # Part of a media group — buffer it
                    if media_group_id not in media_group_buffer:
                        media_group_buffer[media_group_id] = []
                        media_group_times[media_group_id] = time.time()
                    media_group_buffer[media_group_id].append(channel_post)
                    logger.info("Buffered media group item %s (group: %s, items: %d)",
                                msg_id, media_group_id, len(media_group_buffer[media_group_id]))
                else:
                    # Single message — forward immediately
                    logger.info("New single post detected: %s", msg_id)
                    forward_single_message(bot, channel_post, source, dest)

            # Check if any media groups are complete (waited long enough)
            now = time.time()
            completed_groups = [
                gid for gid, t in media_group_times.items()
                if now - t >= MEDIA_GROUP_WAIT
            ]
            for gid in completed_groups:
                messages = media_group_buffer.pop(gid)
                media_group_times.pop(gid)
                logger.info("Processing media group %s with %d items", gid, len(messages))
                forward_media_group(bot, messages, source, dest)

        except Exception as exc:
            logger.exception("Unexpected error in polling loop: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
