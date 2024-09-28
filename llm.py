import logging
import instructor
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from env import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

class SpamCheckResponse(BaseModel):
    is_spam: bool

class Llm:
    instructor_client: instructor.Instructor | instructor.AsyncInstructor

    def __init__(self, instructor_client: instructor.Instructor | instructor.AsyncInstructor):
        self.instructor_client = instructor_client
        logger.info("LLM instance initialized")

    async def is_spam(self, message_text):
        logger.info("Checking if message is spam")
        logger.debug(f"Message text: {message_text[:50]}...")  # Log first 50 characters for privacy

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
            logger.debug("Sending request to Claude")
            resp, completion = await self.instructor_client.messages.create_with_completion(
                model="claude-3-haiku-20240307",  # Replace with the appropriate model name
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
        anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        logger.debug("AsyncAnthropic client created")
        instructor_client = instructor.from_anthropic(anthropic_client)
        logger.debug("Instructor client created")
        return Llm(instructor_client)
    except Exception as e:
        logger.error(f"Error creating LLM instance: {str(e)}")
        raise