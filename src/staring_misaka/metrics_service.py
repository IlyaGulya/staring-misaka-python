import logging

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from .config import Settings

logger = logging.getLogger(__name__)

# Define metrics (globally, but update them from appropriate places)
MESSAGES_PROCESSED = Counter(
    "staring_misaka_messages_processed_total", "Total number of messages processed by the bot", ["chat_id"]
)
SPAM_DETECTED = Counter(
    "staring_misaka_spam_detected_total",
    "Total number of spam messages detected",
    ["chat_id", "model_name", "detection_type"],  # detection_type: auto, admin_approved
)
USERS_BANNED = Counter(
    "staring_misaka_users_banned_total",
    "Total number of users banned",
    ["chat_id", "reason_type"],  # reason_type: auto_spam, admin_decision
)
LLM_API_REQUESTS = Counter(
    "staring_misaka_llm_api_requests_total",
    "Total LLM API requests",
    [
        "model_name",
        "chat_id_label",
    ],  # Use chat_id_label to avoid high cardinality if many chats; or group by "group_type"
)
LLM_API_ERRORS = Counter("staring_misaka_llm_api_errors_total", "Total LLM API errors", ["model_name", "error_type"])
LLM_TOKENS_USED = Counter(
    "staring_misaka_llm_tokens_used_total",
    "Total LLM tokens used",
    ["model_name", "token_type"],  # token_type: input, output
)
LLM_ESTIMATED_COST_CENTS = Counter(
    "staring_misaka_llm_estimated_cost_cents_total", "Estimated LLM cost in cents", ["model_name", "currency"]
)
ACTIVE_MONITORED_GROUPS = Gauge("staring_misaka_active_monitored_groups", "Number of currently monitored groups")
PENDING_ADMIN_ACTIONS = Gauge(
    "staring_misaka_pending_admin_actions", "Number of pending admin actions (e.g., ban approvals)"
)

# Latency for LLM responses (optional, but good)
LLM_RESPONSE_LATENCY_SECONDS = Histogram(
    "staring_misaka_llm_response_latency_seconds", "Latency of LLM API responses", ["model_name"]
)


def start_metrics_server(settings: Settings):
    try:
        start_http_server(settings.prometheus_port)
        logger.info(f"Prometheus metrics server started on port {settings.prometheus_port}")
    except Exception as e:
        logger.error(f"Failed to start Prometheus metrics server: {e}", exc_info=True)


# Helper functions to update metrics (examples)
def record_llm_tokens(model_name: str, input_tokens: int, output_tokens: int):
    if input_tokens:
        LLM_TOKENS_USED.labels(model_name=model_name, token_type="input").inc(input_tokens)
    if output_tokens:
        LLM_TOKENS_USED.labels(model_name=model_name, token_type="output").inc(output_tokens)


def record_llm_cost(model_name: str, cost_decimal, currency: str):
    if cost_decimal is not None:
        cost_cents = int(cost_decimal * 100)
        LLM_ESTIMATED_COST_CENTS.labels(model_name=model_name, currency=currency).inc(cost_cents)


# Call update_dynamic_gauges periodically or on change
async def update_dynamic_gauges():
    from sqlalchemy import func, select  # Local import for DB models

    from .db_models import MonitoredGroup, PendingAdminAction
    from .db_utils import get_db_session

    async with get_db_session() as session:
        num_groups_result = await session.execute(select(func.count(MonitoredGroup.chat_id)))
        num_groups = num_groups_result.scalar_one_or_none()
        ACTIVE_MONITORED_GROUPS.set(num_groups or 0)

        num_pending_actions_result = await session.execute(select(func.count(PendingAdminAction.id)))
        num_pending_actions = num_pending_actions_result.scalar_one_or_none()
        PENDING_ADMIN_ACTIONS.set(num_pending_actions or 0)
    logger.debug("Updated dynamic Prometheus gauges.")
