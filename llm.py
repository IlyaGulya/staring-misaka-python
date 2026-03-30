import logging
import warnings

# Suppress Pydantic v1 deprecation warnings from external libraries
warnings.filterwarnings(
    "ignore",
    message=r"Support for class-based.*config.*is deprecated.*",
    category=DeprecationWarning
)

import instructor
from pydantic import BaseModel, ConfigDict


logger = logging.getLogger(__name__)

class SpamCheckResponse(BaseModel):
    model_config = ConfigDict(
        # Enable frozen mode for immutability
        frozen=True,
        # Validate assignments
        validate_assignment=True,
    )
    
    is_spam: bool

class Llm:
    def __init__(self, client):
        self.client = client
        logger.debug("LLM instance initialized")

    async def is_spam(self, message_text):
        logger.debug("Running spam detection via LLM")
        logger.debug(f"Message preview: {message_text[:50]}...")  # Log first 50 characters for privacy

        # Prepare the prompt
        prompt = (
            "Determine whether the following message is spam. "
            "Messages with suspicious inline buttons, hidden mentions, or forwarded spam are very likely spam "
            "even if the visible text looks innocent.\n\n"
            "<message>"
            f"{message_text}"
            "</message>"
        )

        try:
            # Send the request to Claude
            logger.debug("Sending request to Claude API")
            resp = self.client.chat.completions.create(
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                response_model=SpamCheckResponse,
            )
            logger.debug(f"LLM response: is_spam={resp.is_spam}")
            return resp.is_spam
        except Exception as e:
            logger.error(f"Error during spam check: {str(e)}")
            raise

def create_llm(config) -> Llm:
    """Create LLM instance with the given configuration"""
    logger.debug("Creating LLM instance")
    try:
        # Use instructor.from_provider with Anthropic
        client = instructor.from_provider("anthropic/claude-3-haiku-20240307", api_key=config.anthropic_api_key)
        logger.debug("LLM client created")
        return Llm(client)
    except Exception as e:
        logger.error(f"Error creating LLM instance: {str(e)}")
        raise