import logging
import warnings
from typing import Optional

# Suppress Pydantic v1 deprecation warnings from external libraries
warnings.filterwarnings(
    "ignore",
    message=r"Support for class-based.*config.*is deprecated.*",
    category=DeprecationWarning
)

import instructor
from pydantic import BaseModel, ConfigDict

from config import SpamConfig


logger = logging.getLogger(__name__)


METADATA_HINT = (
    "\n\nThe message may include a <metadata> block with extra context about the sender, "
    "forwarded origin, inline buttons, hidden mentions, media, etc. Messages with suspicious "
    "inline buttons, hidden mentions, or forwarded spam are very likely spam even if the visible "
    "text looks innocent."
)

USER_PROMPT = """Classify this message:

<message>
{message_text}
</message>"""


class SpamCheckResponse(BaseModel):
    model_config = ConfigDict(
        # Enable frozen mode for immutability
        frozen=True,
        # Validate assignments
        validate_assignment=True,
    )

    reason: str
    is_spam: bool


class Llm:
    def __init__(self, client, spam_config: SpamConfig):
        self.client = client
        self.spam_config = spam_config
        logger.debug(f"LLM instance initialized with model: {spam_config.model}")

    async def is_spam(self, message_text: str, chat_id: Optional[int] = None) -> SpamCheckResponse:
        logger.debug("Running spam detection via LLM")
        logger.debug(f"Message preview: {message_text[:50]}...")  # Log first 50 characters for privacy

        # Prepare the prompts
        system_prompt = self.spam_config.get_system_prompt(chat_id) + METADATA_HINT
        user_prompt = USER_PROMPT.format(message_text=message_text)

        try:
            # Send the request to Claude
            logger.debug("Sending request to Claude API")
            resp = self.client.chat.completions.create(
                max_tokens=256,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    }
                ],
                response_model=SpamCheckResponse,
            )
            logger.debug(f"LLM response: is_spam={resp.is_spam}, reason={resp.reason}")
            return resp
        except Exception as e:
            logger.error(f"Error during spam check: {str(e)}")
            raise


def create_llm(config, spam_config: SpamConfig) -> Llm:
    """Create LLM instance with the given configuration"""
    logger.debug("Creating LLM instance")
    try:
        # Use instructor.from_provider with Anthropic and configured model
        model_name = f"anthropic/{spam_config.model}"
        client = instructor.from_provider(model_name, api_key=config.anthropic_api_key)
        logger.debug(f"LLM client created with model: {spam_config.model}")
        return Llm(client, spam_config)
    except Exception as e:
        logger.error(f"Error creating LLM instance: {str(e)}")
        raise