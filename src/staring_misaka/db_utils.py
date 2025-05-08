import datetime  # Required for timezone
import logging
from contextlib import asynccontextmanager
from datetime import timezone  # Required for timezone.utc

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import Settings
from .db_models import Base, GlobalBotSettings, LLMModel, MonitoredGroup, Prompt

logger = logging.getLogger(__name__)

engine = None
AsyncSessionFactory = None


def init_db(settings: Settings):
    global engine, AsyncSessionFactory
    if settings.db_url.startswith("sqlite"):
        engine = create_async_engine(settings.db_url, echo=False)
    else:
        engine = create_async_engine(settings.db_url, echo=False, pool_pre_ping=True)

    AsyncSessionFactory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    logger.info(f"Database engine initialized with URL ending in: ...{settings.db_url[-20:]}")


@asynccontextmanager
async def get_db_session() -> AsyncSession:
    if AsyncSessionFactory is None:
        raise RuntimeError("Database session factory not initialized. Call init_db() first.")

    session: AsyncSession = AsyncSessionFactory()
    try:
        yield session
        await session.commit()
    except Exception as e:
        logger.error(f"Database session error: {e}", exc_info=True)
        await session.rollback()
        raise
    finally:
        await session.close()


async def create_tables():
    if engine is None: raise RuntimeError("Database engine not initialized.")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables checked/created successfully.")


async def initialize_default_data(settings: Settings):
    async with get_db_session() as session:
        gs = await session.get(GlobalBotSettings, 1)
        if not gs:
            gs = GlobalBotSettings(id=1, super_admin_id=settings.admin_id)
            session.add(gs)
            logger.info(f"Initialized GlobalBotSettings: super_admin_id={settings.admin_id}")
        elif gs.super_admin_id != settings.admin_id:
            logger.warning(
                f"Super admin ID in DB ({gs.super_admin_id}) differs from config ({settings.admin_id}). Keeping DB value.")

        default_prompt_result = await session.execute(select(Prompt).where(Prompt.is_global_default))
        default_prompt = default_prompt_result.scalar_one_or_none()

        if not default_prompt:
            fallback_prompt = await session.scalar(select(Prompt).where(Prompt.name == "Global Default Spam Check"))
            if fallback_prompt:
                default_prompt = fallback_prompt
                default_prompt.is_global_default = True
                logger.info(f"Found existing prompt '{default_prompt.name}' and marked as global default.")
            else:
                default_prompt = Prompt(
                    name="Global Default Spam Check",
                    text=(
                        "Analyze the following message. Is it spam? If yes, provide a very brief (under 100 characters) "
                        "reason for the ban, suitable for public display in a chat group. "
                        "The chat group is for general discussion, but unsolicited advertising, "
                        "scams, or irrelevant content are considered spam.\n\n<message>\n{message_text}\n</message>"
                    ),
                    is_global_default=True,
                    created_at=datetime.datetime.now(timezone.utc)  # Explicitly set timezone
                )
                session.add(default_prompt)
                await session.flush()
                logger.info(f"Created new default global prompt: {default_prompt.name}")

        if not default_prompt.id: await session.flush()  # Ensure ID is available if just created
        if not gs.default_prompt_id or gs.default_prompt_id != default_prompt.id:
            gs.default_prompt_id = default_prompt.id
            logger.info(f"Set default_prompt_id in GlobalSettings to {default_prompt.id} ('{default_prompt.name}').")

        target_default_provider = "Anthropic"
        target_default_api_id = "claude-3-haiku-20240307"
        target_default_name = "Claude 3 Haiku"

        default_model = await session.scalar(
            select(LLMModel).where(LLMModel.provider == target_default_provider,
                                   LLMModel.api_identifier == target_default_api_id)
        )
        if not default_model:
            default_model = LLMModel(name=target_default_name, api_identifier=target_default_api_id,
                                     provider=target_default_provider,
                                     created_at=datetime.datetime.now(timezone.utc))  # Explicitly set timezone
            session.add(default_model)
            await session.flush()
            logger.info(f"Created default LLMModel entry: {default_model.name}")

        if not default_model.id: await session.flush()  # Ensure ID is available
        if not gs.default_model_id:
            gs.default_model_id = default_model.id
            logger.info(f"Set default_model_id in GlobalSettings to {default_model.id} ('{default_model.name}').")
        else:
            current_default_model = await session.get(LLMModel, gs.default_model_id)
            if not current_default_model:
                logger.warning(
                    f"Global default model ID {gs.default_model_id} in DB is invalid. Resetting to {default_model.name} (ID: {default_model.id}).")
                gs.default_model_id = default_model.id
    logger.info("Default data initialization complete.")


async def get_monitored_chat_ids() -> list[int]:
    async with get_db_session() as session:
        result = await session.execute(select(MonitoredGroup.chat_id))
        return [cid for cid, in result.all()]
