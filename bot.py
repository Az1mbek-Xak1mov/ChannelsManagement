import asyncio
import logging
import os
import re
from collections import defaultdict

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
from openai_processor import process_text


load_dotenv()


# =========================
# Configuration
# =========================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("STRING_SESSION", "")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "")

SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "")
DESTINATION_CHANNEL = os.getenv("DESTINATION_CHANNEL", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

ALBUM_BUFFER_SECONDS = float(os.getenv("ALBUM_BUFFER_SECONDS", "2.5"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("telethon-forwarder")


def validate_config() -> None:
    """Fail fast when required env vars are missing."""
    required = {
        "API_ID": API_ID,
        "API_HASH": API_HASH,
        "STRING_SESSION": STRING_SESSION,
        "SOURCE_CHANNEL": SOURCE_CHANNEL,
        "DESTINATION_CHANNEL": DESTINATION_CHANNEL,
        "OPENAI_API_KEY": OPENAI_API_KEY,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def normalize_chat(value: str) -> str | int:
    """Accept @username, username, or numeric ID."""
    raw = value.strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw if raw.startswith("@") else f"@{raw}"


def extract_text_for_ai(message: Message) -> str:
    """Extract plain text/caption for OpenAI processing."""
    return message.raw_text or ""


def extract_source_links(message: Message) -> list[tuple[int, str]]:
    """Extract links from source message as (source_word_index, url)."""
    text = message.raw_text or ""
    entities = message.entities or []
    if not text or not entities:
        return []

    links: list[tuple[int, str]] = []

    for ent in entities:
        if isinstance(ent, MessageEntityTextUrl):
            # Hidden URL behind visible word/phrase.
            prefix = text[:ent.offset]
            source_word_index = len(re.findall(r"\S+", prefix))
            links.append((source_word_index, ent.url))
        elif isinstance(ent, MessageEntityUrl):
            # Visible URL in text.
            raw_url = text[ent.offset:ent.offset + ent.length].strip()
            if raw_url:
                prefix = text[:ent.offset]
                source_word_index = len(re.findall(r"\S+", prefix))
                links.append((source_word_index, raw_url))

    return links


def attach_links_to_translated_text(
    translated_text: str,
    source_links: list[tuple[int, str]],
    source_text: str,
) -> str:
    """Attach source URLs to nearest translated words, preserving clickable links."""
    if not translated_text.strip() or not source_links:
        return translated_text

    if re.search(r"\[[^\]]+\]\([^\)]+\)", translated_text):
        # OpenAI already preserved at least one markdown link.
        return translated_text

    src_words = re.findall(r"\S+", source_text)
    dst_words = list(re.finditer(r"\S+", translated_text))
    if not dst_words:
        return translated_text

    src_count = max(len(src_words), 1)
    dst_count = len(dst_words)

    used_dst_indexes: set[int] = set()
    chars = list(translated_text)

    # Process from end to start so index replacement does not shift earlier spans.
    resolved: list[tuple[int, int, str, str]] = []
    for src_idx, url in source_links:
        mapped = round((min(max(src_idx, 0), src_count - 1) / src_count) * (dst_count - 1))
        while mapped in used_dst_indexes and mapped + 1 < dst_count:
            mapped += 1
        used_dst_indexes.add(mapped)

        match = dst_words[mapped]
        resolved.append((match.start(), match.end(), match.group(0), url))

    for start, end, word, url in sorted(resolved, key=lambda x: x[0], reverse=True):
        safe_word = word.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        safe_url = url.replace(")", "%29")
        linked = f"[{safe_word}]({safe_url})"
        chars[start:end] = list(linked)

    return "".join(chars)


async def main() -> None:
    validate_config()

    source_ref = normalize_chat(SOURCE_CHANNEL)
    destination_ref = normalize_chat(DESTINATION_CHANNEL)

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        logger.warning("Provided StringSession is not authorized.")
        if not PHONE_NUMBER:
            raise RuntimeError(
                "StringSession is not authorized and PHONE_NUMBER is missing. "
                "Set PHONE_NUMBER and rerun to generate a new session."
            )

        logger.info("Requesting login code for %s", PHONE_NUMBER)
        await client.send_code_request(PHONE_NUMBER)
        code = input("Enter Telegram login code: ").strip()

        try:
            await client.sign_in(phone=PHONE_NUMBER, code=code)
        except SessionPasswordNeededError:
            password = input("Enter 2FA password: ").strip()
            await client.sign_in(password=password)

        new_session = client.session.save()
        logger.info("Authorization successful.")
        print("\nNew STRING_SESSION (save this to .env):")
        print(new_session)

    source_entity = await client.get_entity(source_ref)
    destination_entity = await client.get_entity(destination_ref)

    logger.info("User client started")
    logger.info("Source channel: %s (id=%s)", SOURCE_CHANNEL, source_entity.id)
    logger.info("Destination channel: %s", DESTINATION_CHANNEL)

    # grouped_id -> list[Message]
    album_buffers: dict[int, list[Message]] = defaultdict(list)
    # grouped_id -> asyncio.Task
    album_tasks: dict[int, asyncio.Task] = {}

    async def flush_album(grouped_id: int) -> None:
        """Wait briefly, then send buffered album as one media group."""
        await asyncio.sleep(ALBUM_BUFFER_SECONDS)

        messages = album_buffers.pop(grouped_id, [])
        album_tasks.pop(grouped_id, None)

        if not messages:
            return

        messages.sort(key=lambda m: m.id)
        logger.info("Processing album grouped_id=%s items=%d", grouped_id, len(messages))

        original_text = ""
        source_for_links = None
        for msg in messages:
            candidate = extract_text_for_ai(msg)
            if candidate.strip():
                original_text = candidate
                source_for_links = msg
                break

        new_text = ""
        if original_text.strip():
            try:
                processed = await process_text(original_text)
            except Exception as exc:
                logger.exception("OpenAI failed for album grouped_id=%s: %s", grouped_id, exc)
                return

            if processed is None:
                logger.info("Album grouped_id=%s skipped by OpenAI", grouped_id)
                return
            new_text = processed
            if source_for_links is not None:
                links = extract_source_links(source_for_links)
                new_text = attach_links_to_translated_text(new_text, links, original_text)

        media_objects = [m.media for m in messages if m.media is not None]
        if not media_objects:
            logger.warning("Album grouped_id=%s has no media, skipping", grouped_id)
            return

        # Zero-download media forwarding: pass media objects directly.
        await client.send_file(
            destination_entity,
            file=media_objects,
            caption=new_text if new_text else None,
            parse_mode="md",
        )
        logger.info("Forwarded album grouped_id=%s", grouped_id)

    @client.on(events.NewMessage(chats=source_entity))
    async def on_new_message(event: events.NewMessage.Event) -> None:
        """Handle new source-channel messages and forward to destination."""
        message = event.message

        logger.info(
            "Received message id=%s grouped_id=%s chat_id=%s out=%s post=%s",
            message.id,
            message.grouped_id,
            event.chat_id,
            message.out,
            message.post,
        )

        # Album/media-group: buffer by grouped_id and flush once.
        if message.grouped_id is not None:
            gid = int(message.grouped_id)
            album_buffers[gid].append(message)

            if gid not in album_tasks:
                album_tasks[gid] = asyncio.create_task(flush_album(gid))
            return

        original_text = extract_text_for_ai(message)
        new_text = ""

        if original_text.strip():
            try:
                processed = await process_text(original_text)
            except Exception as exc:
                logger.exception("OpenAI failed for message id=%s: %s", message.id, exc)
                return

            if processed is None:
                logger.info("Message id=%s skipped by OpenAI", message.id)
                return
            new_text = processed
            links = extract_source_links(message)
            new_text = attach_links_to_translated_text(new_text, links, original_text)

        # Zero-download media forwarding.
        if message.media is not None:
            await client.send_file(
                destination_entity,
                file=message.media,
                caption=new_text if new_text else None,
                parse_mode="md",
            )
            logger.info("Forwarded media message id=%s", message.id)
            return

        # Text-only forwarding.
        if new_text.strip():
            await client.send_message(destination_entity, new_text, parse_mode="md")
            logger.info("Forwarded text message id=%s", message.id)
        else:
            logger.info("Message id=%s has no text and no media, skipped", message.id)

    logger.info("Listening for new posts...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
