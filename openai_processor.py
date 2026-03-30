import logging
from openai import AsyncOpenAI
from config import OPENAI_API_KEY, OPENAI_MODEL

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """You are a content curator and translator assistant. Your task is to process Telegram channel posts with the following rules:

1. **Relevance Check**: Determine if the text is related to the IT sphere (programming, software development, tech news, startups, AI, cybersecurity, gadgets, tech industry news, etc.). If the content is NOT related to IT/tech, return exactly: SKIP_POST
   Strictly skip content about fitness/bodybuilding, sports, entertainment gossip, personal life stories, motivation quotes, politics without tech angle, and other non-IT topics.

2. **Advertisement Check**: Skip only if the post is clearly promotional/self-ad content such as channel promotion, referral links, sponsored sales language, course/service marketing, or direct calls to buy/subscribe/join. If yes, return exactly: SKIP_POST
   IMPORTANT: Do NOT skip normal IT/tech news, industry updates, policy/news about companies (Apple, Google, Microsoft, etc.), product announcements, or analytical posts just because a brand is mentioned.

3. **Link Removal**: Remove all:
   - Telegram channel links (t.me/...)
   - Telegram usernames (@username)
   - External promotional links
   - Any URLs that appear to be promotional
   Keep informational links (like GitHub repos, documentation, official tech resources) if they add value.

4. **Translation**: Translate the clean, IT-related text into natural, professional Uzbek language. Maintain the original formatting and structure. Technical terms can remain in English where appropriate.

IMPORTANT RULES:
- If the post should be skipped (not IT-related OR is an ad), return ONLY the word: SKIP_POST
- If the post is valid, return ONLY the translated Uzbek text without any explanations or prefixes.
- Do not add any commentary, headers, or explanations to your response.
- Preserve any code snippets, technical terms, and formatting from the original."""


async def process_text(text: str) -> str | None:
    """
    Process text through OpenAI API for filtering and translation.
    
    Returns:
        - None if the post should be skipped
        - Translated Uzbek text if valid
    """
    if not text or not text.strip():
        logger.info("Empty text received, skipping")
        return None
    
    try:
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=0.3,
            max_completion_tokens=2000
        )
        
        result = response.choices[0].message.content.strip()
        
        if result == "SKIP_POST":
            logger.info("Post marked as SKIP_POST by OpenAI (not IT-related or is an ad)")
            return None
        
        logger.info("Post successfully processed and translated")
        return result
        
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        return None
