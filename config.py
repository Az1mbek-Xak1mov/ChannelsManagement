import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")
DESTINATION_CHANNEL = os.getenv("DESTINATION_CHANNEL")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")


def _parse_admin_ids(raw_value: str) -> set[int]:
	ids: set[int] = set()
	for token in (raw_value or "").split(","):
		token = token.strip()
		if not token:
			continue
		if token.lstrip("-").isdigit():
			ids.add(int(token))
	return ids


ADMIN_USER_IDS = _parse_admin_ids(
	os.getenv("ADMIN_USER_IDS", "283033165,5430618568")
)
