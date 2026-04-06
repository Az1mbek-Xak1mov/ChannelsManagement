import logging
import time
import json
from typing import Optional, Tuple, List
import asyncio
import re
from urllib.parse import urljoin
from io import BytesIO

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
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


# Regex matching strings that are ONLY emoji (incl. keycaps, ZWJ sequences, flags, variation selectors)
_EMOJI_ONLY_RE = re.compile(
    r'^[\s'
    r'\U0001F600-\U0001F64F'   # emoticons
    r'\U0001F300-\U0001F5FF'   # symbols & pictographs
    r'\U0001F680-\U0001F6FF'   # transport & map
    r'\U0001F1E0-\U0001F1FF'   # flags
    r'\U0001F900-\U0001F9FF'   # supplemental symbols
    r'\U0001FA00-\U0001FA6F'   # chess symbols
    r'\U0001FA70-\U0001FAFF'   # symbols extended-A
    r'\U00002702-\U000027B0'   # dingbats
    r'\U0000FE00-\U0000FE0F'   # variation selectors
    r'\U0000200D'              # ZWJ
    r'\U000020E3'              # combining enclosing keycap
    r'\U00002600-\U000026FF'   # misc symbols
    r'\U00002B50-\U00002B55'   # stars
    r'\U0000231A-\U0000231B'   # watch/hourglass
    r'\U00002934-\U00002935'   # arrows
    r'\U000025AA-\U000025FE'   # geometric shapes
    r'\U00000030-\U00000039'   # digits 0-9 (for keycap sequences like 2️⃣)
    r'\U0000002A\U00000023'    # * and # for keycap
    r']+$'
)


def _is_emoji_only(text: str) -> bool:
    """Return True if text consists only of emoji characters and whitespace."""
    return bool(text.strip()) and bool(_EMOJI_ONLY_RE.match(text))


def _escape_markdown(text: str) -> str:
    """Escape Markdown special characters, but leave already-formed links intact."""
    # We only escape characters that could break Markdown parsing
    # but NOT inside [...](url) constructs which we build ourselves
    for ch in ('\\', '`', '*', '_', '{', '}', '[', ']', '(', ')', '#', '+', '-', '.', '!'):
        text = text.replace(ch, f'\\{ch}')
    return text


def extract_text_with_links(element: Tag) -> str:
    """Walk HTML tree and convert to Markdown-style text.

    Preserves:
    - <a> tags as [text](url)
    - <b>/<strong> as **text**
    - <i>/<em> as _text_
    - <br> as newline
    - Inline elements (spans, tg-emoji, etc.) stay on the same line
    """
    parts: list[str] = []

    for child in element.children:
        if isinstance(child, NavigableString):
            text = str(child)
            # Escape markdown chars in plain text
            text = _escape_markdown(text)
            parts.append(text)
        elif isinstance(child, Tag):
            tag_name = child.name.lower()

            if tag_name == 'br':
                parts.append('\n')
            elif tag_name == 'a':
                href = child.get('href', '')
                link_text = child.get_text()
                if href and link_text.strip():
                    # Keep the link in Markdown format
                    safe_text = _escape_markdown(link_text)
                    parts.append(f'[{safe_text}]({href})')
                elif link_text.strip():
                    parts.append(_escape_markdown(link_text))
            elif tag_name in ('b', 'strong'):
                inner = extract_text_with_links(child)
                # Don't wrap emojis in bold — they render badly as **😱**
                if _is_emoji_only(inner):
                    parts.append(inner)
                else:
                    parts.append(f'**{inner}**')
            elif tag_name in ('i', 'em'):
                inner = extract_text_with_links(child)
                parts.append(f'_{inner}_')
            elif tag_name in ('code',):
                inner = child.get_text()
                parts.append(f'`{inner}`')
            elif tag_name in ('pre',):
                inner = child.get_text()
                parts.append(f'```\n{inner}\n```')
            elif tag_name in ('div', 'p'):
                inner = extract_text_with_links(child)
                parts.append(f'\n{inner}\n')
            else:
                # Inline elements: span, tg-emoji, etc. - keep on same line
                inner = extract_text_with_links(child)
                parts.append(inner)

    result = ''.join(parts)
    # Clean up excessive newlines
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def scrape_latest_post(source_channel: str) -> Optional[Tuple[int, str, List[Tuple[str, str]]]]:
    """Scrape latest post ID, text, and ALL media from the public Telegram web view.

    Returns:
        (message_id, message_text_markdown, [(media_type, media_url), ...])
    """
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

        # ---- Extract text with links preserved as Markdown ----
        text_el = latest_block.select_one("div.tgme_widget_message_text")
        if text_el:
            message_text = extract_text_with_links(text_el)
        else:
            message_text = ""

        # ---- Collect ALL media items ----
        media_items: List[Tuple[str, str]] = []

        # Collect all videos
        for vid_source in latest_block.select("video source[src]"):
            src = vid_source.get("src")
            if src:
                media_items.append(("video", urljoin("https://t.me", src)))

        # Also check <video src="..."> directly (without <source>)
        for vid in latest_block.select("video[src]"):
            src = vid.get("src")
            if src:
                full_url = urljoin("https://t.me", src)
                # Avoid duplicates from <video><source> already captured
                if not any(u == full_url for _, u in media_items):
                    media_items.append(("video", full_url))

        # Collect all photos
        for photo_el in latest_block.select("a.tgme_widget_message_photo_wrap"):
            style_value = photo_el.get("style", "")
            extracted = _extract_photo_url(style_value)
            if extracted:
                media_items.append(("photo", urljoin("https://t.me", extracted)))

        logger.debug(
            "Scraped post %s: text_len=%d, media_count=%d",
            message_id, len(message_text), len(media_items)
        )
        return message_id, message_text, media_items
    except (ValueError, AttributeError) as exc:
        logger.warning("Failed to parse latest message: %s", exc)
        return None


def send_post_to_destination(
    bot_token: str,
    destination_channel: str,
    text: str,
    media_items: Optional[List[Tuple[str, str]]] = None,
) -> bool:
    """Send post (text/media) to destination channel via Telegram Bot API.

    Supports single media, media groups, and text-only posts.
    Text is sent with parse_mode=Markdown so links render.
    """
    base_url = f"https://api.telegram.org/bot{bot_token}"

    def _send(send_url: str, payload: dict, files: Optional[dict] = None) -> tuple[bool, str]:
        try:
            response = requests.post(
                send_url,
                data=payload,
                files=files,
                timeout=60,  # larger timeout for media uploads
            )
            data = response.json()
            if response.status_code >= 400:
                return False, data.get("description", response.text)
            if not data.get("ok"):
                return False, data.get("description", str(data))
            return True, "ok"
        except (requests.RequestException, ValueError) as exc:
            return False, str(exc)

    def _download_media(url: str) -> tuple[Optional[BytesIO], Optional[str], Optional[str]]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": build_source_web_url(SOURCE_CHANNEL),
        }
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            if "image" in ctype:
                filename = "media.jpg"
            elif "video" in ctype:
                filename = "media.mp4"
            else:
                filename = "media.bin"
            return BytesIO(r.content), filename, ctype
        except requests.RequestException as exc:
            return None, None, str(exc)

    if not media_items:
        media_items = []

    # ---- CASE 1: Media group (2+ items) -> sendMediaGroup ----
    if len(media_items) >= 2:
        media_json = []
        files_dict = {}
        for idx, (mtype, murl) in enumerate(media_items):
            media_bytes, filename, dl_err = _download_media(murl)
            attach_key = f"media_{idx}"
            entry = {
                "type": mtype,  # "photo" or "video"
            }
            if idx == 0 and text.strip():
                entry["caption"] = text
                entry["parse_mode"] = "Markdown"
            if media_bytes:
                entry["media"] = f"attach://{attach_key}"
                files_dict[attach_key] = (filename or f"media_{idx}", media_bytes.getvalue())
            else:
                # Fallback to URL if download failed
                logger.warning("Media %d download failed (%s), trying URL", idx, dl_err)
                entry["media"] = murl
            media_json.append(entry)

        ok, err = _send(
            f"{base_url}/sendMediaGroup",
            {
                "chat_id": destination_channel,
                "media": json.dumps(media_json),
            },
            files=files_dict if files_dict else None,
        )
        if ok:
            return True
        logger.warning("sendMediaGroup failed: %s, trying individual sends", err)
        # Fallback: send items individually
        any_sent = False
        for idx, (mtype, murl) in enumerate(media_items):
            caption = text if idx == 0 and text.strip() else ""
            single_ok = _send_single_media(bot_token, destination_channel, caption, mtype, murl, _send, _download_media, base_url)
            if single_ok:
                any_sent = True
        return any_sent

    # ---- CASE 2: Single media item ----
    if len(media_items) == 1:
        mtype, murl = media_items[0]
        return _send_single_media(bot_token, destination_channel, text, mtype, murl, _send, _download_media, base_url)

    # ---- CASE 3: Text only ----
    if not text.strip():
        logger.info("Post has no text and no media, skipping send")
        return False

    ok, err = _send(
        f"{base_url}/sendMessage",
        {"chat_id": destination_channel, "text": text, "parse_mode": "Markdown"},
    )
    if not ok:
        logger.error("Failed to send message to destination: %s", err)
    return ok


def _send_single_media(bot_token, destination_channel, text, media_type, media_url, _send, _download_media, base_url) -> bool:
    """Send a single photo or video with optional caption."""
    if media_type == "photo":
        api_method = "sendPhoto"
        file_key = "photo"
        fallback_name = "photo.jpg"
    else:
        api_method = "sendVideo"
        file_key = "video"
        fallback_name = "video.mp4"

    caption_payload = {}
    if text.strip():
        caption_payload = {"caption": text, "parse_mode": "Markdown"}

    # Try download + upload
    media_bytes, filename, download_err = _download_media(media_url)
    if media_bytes:
        ok, err = _send(
            f"{base_url}/{api_method}",
            {"chat_id": destination_channel, **caption_payload},
            files={file_key: (filename or fallback_name, media_bytes.getvalue())},
        )
        if ok:
            return True
        logger.warning("%s by file failed, trying URL. Error: %s", api_method, err)

    # Try by URL
    ok, err = _send(
        f"{base_url}/{api_method}",
        {"chat_id": destination_channel, file_key: media_url, **caption_payload},
    )
    if ok:
        return True
    logger.warning("%s by URL failed, fallback to text. Error: %s", api_method, err)

    # Final fallback: send as text-only
    if text.strip():
        ok, err = _send(
            f"{base_url}/sendMessage",
            {"chat_id": destination_channel, "text": text, "parse_mode": "Markdown"},
        )
        if ok:
            return True
        logger.error("Text-only fallback also failed: %s", err)
    return False


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

            message_id, message_text, media_items = latest

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
                    media_items=media_items,
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
