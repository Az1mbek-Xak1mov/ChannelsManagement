# Telegram Channel Automation Bot

Automated content curator and translator bot that listens to posts from a source Telegram channel, filters IT-related content, and publishes translated versions to a destination channel.

## Features

- **Content Filtering**: Automatically skips non-IT related posts and advertisements
- **Link Removal**: Strips promotional links and Telegram usernames
- **Translation**: Translates IT content to professional Uzbek
- **Media Support**: Handles photos, videos, documents, and animations with captions

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:
- `BOT_TOKEN`: Get from [@BotFather](https://t.me/BotFather)
- `OPENAI_API_KEY`: Get from [OpenAI Platform](https://platform.openai.com/api-keys)
- `SOURCE_CHANNEL`: Channel username to listen to (e.g. `@azims_vlog`)
- `DESTINATION_CHANNEL`: Channel username to publish to (e.g. `@testing_channel_tel`)
- `OPENAI_MODEL`: OpenAI model name (default: `gpt-5.4`)

### 3. Bot Permissions

The bot must be added as an **administrator** to both channels:
- Source channel: @malepeg (needs read access)
- Destination channel: @nazirovlix_blog (needs post access)

### 4. Run the Bot

```bash
python bot.py
```

## Project Structure

```
├── bot.py              # Main bot with aiogram router
├── config.py           # Configuration and environment loading
├── openai_processor.py # OpenAI API integration for filtering/translation
├── requirements.txt    # Python dependencies
├── .env               # Environment variables (not committed)
└── .env.example       # Environment template
```

## Configuration

Set values in `.env` to change:
- `SOURCE_CHANNEL`: Channel to listen to
- `DESTINATION_CHANNEL`: Channel to publish to
- `OPENAI_MODEL`: OpenAI model to use (default: `gpt-5.4`)
