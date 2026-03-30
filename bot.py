import asyncio
import logging
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramConflictError

from config import BOT_TOKEN, SOURCE_CHANNEL, DESTINATION_CHANNEL
from openai_processor import process_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

router = Router()


def get_source_channel_id(source: str) -> str:
    """Convert channel username to comparable format."""
    return source.lstrip("@").lower()


@router.channel_post()
async def handle_channel_post(message: Message, bot: Bot):
    """Handle incoming channel posts from the source channel."""
    
    if not message.sender_chat:
        return
    
    chat_username = message.sender_chat.username
    if not chat_username:
        return
    
    source_username = get_source_channel_id(SOURCE_CHANNEL)
    if chat_username.lower() != source_username:
        return
    
    logger.info(f"Received post from {SOURCE_CHANNEL}, message_id: {message.message_id}")

    has_media = any([
        message.photo,
        message.video,
        message.document,
        message.animation,
        message.audio,
        message.voice,
        message.video_note,
        message.sticker,
    ])

    original_text = message.text or message.caption or ""

    if not original_text.strip():
        logger.info(f"Post {message.message_id} has no text/caption to evaluate, skipping")
        return
    
    translated_text = await process_text(original_text)
    
    if translated_text is None:
        logger.info(f"Post {message.message_id} skipped (filtered by OpenAI)")
        return
    
    try:
        if message.photo:
            photo = message.photo[-1]
            await bot.send_photo(
                chat_id=DESTINATION_CHANNEL,
                photo=photo.file_id,
                caption=translated_text,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Published photo with translated caption to {DESTINATION_CHANNEL}")

        elif message.video:
            await bot.send_video(
                chat_id=DESTINATION_CHANNEL,
                video=message.video.file_id,
                caption=translated_text,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Published video with translated caption to {DESTINATION_CHANNEL}")

        elif message.document:
            await bot.send_document(
                chat_id=DESTINATION_CHANNEL,
                document=message.document.file_id,
                caption=translated_text,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Published document with translated caption to {DESTINATION_CHANNEL}")

        elif message.animation:
            await bot.send_animation(
                chat_id=DESTINATION_CHANNEL,
                animation=message.animation.file_id,
                caption=translated_text,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Published animation with translated caption to {DESTINATION_CHANNEL}")

        elif message.audio:
            await bot.send_audio(
                chat_id=DESTINATION_CHANNEL,
                audio=message.audio.file_id,
                caption=translated_text,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Published audio with translated caption to {DESTINATION_CHANNEL}")

        elif has_media:
            logger.info(
                f"Post {message.message_id} contains unsupported media for caption translation, skipping"
            )

        else:
            await bot.send_message(
                chat_id=DESTINATION_CHANNEL,
                text=translated_text,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Published text post to {DESTINATION_CHANNEL}")
    except Exception as e:
        logger.error(f"Failed to publish post to {DESTINATION_CHANNEL}: {e}")


async def main():
    """Initialize and start the bot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in .env file")
        return
    
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)
    
    logger.info("Bot starting...")
    logger.info(f"Listening for posts from: {SOURCE_CHANNEL}")
    logger.info(f"Publishing to: {DESTINATION_CHANNEL}")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["channel_post"])
    except TelegramConflictError:
        logger.error(
            "Telegram conflict: another instance is already using this bot token. "
            "Stop the other process and run only one instance."
        )


if __name__ == "__main__":
    asyncio.run(main())
