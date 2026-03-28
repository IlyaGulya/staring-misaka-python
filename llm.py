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


SYSTEM_PROMPT = """<task>
You are a spam classifier for a Telegram group chat. Your job is to determine whether a message is spam based on the context and rules below. Accuracy is critical — false positives disrupt real users, and false negatives allow spam through.
</task>

<context>
This message was posted in {context}.
</context>

<rules>
{rules}
</rules>

<spam_conditions>
Flag as spam if the message matches any of these: {spam_conditions}
</spam_conditions>

<instructions>
Analyze the message and classify it as spam or not spam. Consider the chat context — a message that would be spam in one group might be on-topic in another. When uncertain, err on the side of NOT flagging as spam.
The message may include a <metadata> block with extra context about the sender, forwarded origin, inline buttons, hidden mentions, media, etc. Messages with suspicious inline buttons, hidden mentions, or forwarded spam are very likely spam even if the visible text looks innocent.
</instructions>"""

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

        # Get config for this chat, or use default
        if chat_id is not None and chat_id in self.spam_config.chats:
            chat_config = self.spam_config.chats[chat_id]
            logger.debug(f"Using custom config for chat {chat_id}")
        else:
            chat_config = self.spam_config.default
            logger.debug(f"Using default config for chat {chat_id}")

        # Prepare the prompts
        system_prompt = SYSTEM_PROMPT.format(
            context=chat_config.context,
            rules=chat_config.rules,
            spam_conditions=chat_config.spam_conditions
        )
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