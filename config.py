import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")
DESTINATION_CHANNEL = os.getenv("DESTINATION_CHANNEL")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")
