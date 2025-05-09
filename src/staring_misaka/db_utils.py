# src/staring_misaka/db_utils.py
import datetime  # Required for timezone
import logging
from contextlib import asynccontextmanager
from datetime import timezone  # Required for timezone.utc

from sqlalchemy import select, update
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
        await session.flush()

        # --- Robust Default Prompt Setup ---
        target_default_prompt_name = "Global Default Spam Check"
        # Try to find the canonical default prompt
        default_prompt = await session.scalar(select(Prompt).where(Prompt.name == target_default_prompt_name))

        if not default_prompt:
            # If it doesn't exist, create it.
            default_prompt = Prompt(
                name=target_default_prompt_name,
                text=(
                    "Analyze the following message. Is it spam? If yes, provide a very brief (under 100 characters) "
                    "reason for the ban, suitable for public display in a chat group. "
                    "The chat group is for general discussion, but unsolicited advertising, "
                    "scams, or irrelevant content are considered spam.\n\n<message>\n{message_text}\n</message>"
                ),
                is_global_default=False, # Will be set to True below
                created_at=datetime.datetime.now(timezone.utc)
            )
            session.add(default_prompt)
            await session.flush() # Ensure ID is available for default_prompt
            logger.info(f"Created new canonical default prompt: '{default_prompt.name}' (ID: {default_prompt.id}).")

        # Unset is_global_default for all other prompts
        await session.execute(
            update(Prompt)
            .where(Prompt.id != default_prompt.id)
            .values(is_global_default=False)
        )
        # Set the canonical prompt as the global default
        default_prompt.is_global_default = True
        gs.default_prompt_id = default_prompt.id # Assign ID to GlobalSettings
        logger.info(f"Ensured '{default_prompt.name}' (ID: {default_prompt.id}) is the global default prompt in GlobalSettings.")
        await session.flush() # Ensure changes to gs and prompt are persisted before commit

        # --- Default Model Setup ---
        target_default_model_name = "Claude 3 Haiku"
        target_default_api_id = "claude-3-haiku-20240307"
        target_default_provider = "Anthropic"

        # Find or create the canonical default model
        default_model = await session.scalar(
            select(LLMModel).where(LLMModel.name == target_default_model_name,
                                   LLMModel.api_identifier == target_default_api_id,
                                   LLMModel.provider == target_default_provider)
        )
        if not default_model:
            default_model = LLMModel(name=target_default_model_name, api_identifier=target_default_api_id,
                                     provider=target_default_provider,
                                     created_at=datetime.datetime.now(timezone.utc))
            session.add(default_model)
            await session.flush() # Ensure ID for default_model
            logger.info(f"Created default LLMModel entry: {default_model.name} (ID: {default_model.id}).")

        # Ensure GlobalSettings points to this canonical default model
        # If gs.default_model_id is already set, we check if it's valid.
        # If it's not our canonical default, we still override it to ensure the canonical one is set.
        if gs.default_model_id != default_model.id:
            logger.info(f"Resetting GlobalSettings.default_model_id from {gs.default_model_id} to canonical default '{default_model.name}' (ID: {default_model.id}).")
            gs.default_model_id = default_model.id

        await session.flush() # Ensure gs changes are persisted

    logger.info("Default data initialization complete.")


async def get_monitored_chat_ids() -> list[int]:
    async with get_db_session() as session:
        result = await session.execute(select(MonitoredGroup.chat_id))
        return [cid for cid, in result.all()]