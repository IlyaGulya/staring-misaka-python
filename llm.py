import instructor
from anthropic import Anthropic
from pydantic import BaseModel

from env import ANTHROPIC_API_KEY


class SpamCheckResponse(BaseModel):
    is_spam: bool


class Llm:
    instructor_client: instructor.Instructor | instructor.AsyncInstructor

    def __init__(self, instructor_client: instructor.Instructor | instructor.AsyncInstructor):
        self.instructor_client = instructor_client

    async def is_spam(self, message_text):
        # Prepare the prompt
        prompt = (
            "Determine whether the following message is spam. "
            "Return a JSON object with a single field 'is_spam' which is true if the message is spam, false otherwise.\n\n"
            "<message>"
            f"{message_text}"
            "</message>"
        )

        # Send the request to Claude
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
        return resp.is_spam


def create_llm() -> Llm:
    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    instructor_client = instructor.from_anthropic(anthropic_client)
    return Llm(instructor_client)
