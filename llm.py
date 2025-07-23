import logging
import instructor
from pydantic import BaseModel

from env import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

class SpamCheckResponse(BaseModel):
    is_spam: bool

class Llm:
    def __init__(self, client):
        self.client = client
        logger.info("LLM instance initialized")

    async def is_spam(self, message_text):
        logger.info("Checking if message is spam")
        logger.info(f"Message text: {message_text[:50]}...")  # Log first 50 characters for privacy

        # Prepare the prompt
        prompt = (
            "Determine whether the following message is spam. It is posted in a chat where people discuss "
            "Mobile dependency injection solutions. \n"
            "<message>"
            f"{message_text}"
            "</message>"
        )

        try:
            # Send the request to Claude
            logger.info("Sending request to Claude")
            resp = await self.client.chat.completions.create(
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                response_model=SpamCheckResponse,
            )
            logger.info(f"Spam check result: {resp.is_spam}")
            return resp.is_spam
        except Exception as e:
            logger.error(f"Error during spam check: {str(e)}")
            raise

def create_llm() -> Llm:
    logger.info("Creating LLM instance")
    try:
        # Use instructor.from_provider with Anthropic
        client = instructor.from_provider("anthropic/claude-3-haiku-20240307", api_key=ANTHROPIC_API_KEY)
        logger.info("Instructor client created")
        return Llm(client)
    except Exception as e:
        logger.error(f"Error creating LLM instance: {str(e)}")
        raise