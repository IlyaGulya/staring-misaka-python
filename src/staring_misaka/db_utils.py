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
        # 1. GlobalBotSettings
        gs = await session.get(GlobalBotSettings, 1)
        if not gs:
            gs = GlobalBotSettings(id=1, super_admin_id=settings.admin_id)
            session.add(gs)
            logger.info(f"Initialized GlobalBotSettings: super_admin_id={settings.admin_id}")
            await session.flush()  # Ensure gs has an ID if new, for FK constraints
        elif gs.super_admin_id != settings.admin_id:
            logger.warning(
                f"Super admin ID in DB ({gs.super_admin_id}) differs from config ({settings.admin_id}). Keeping DB value."
            )

        # 2. Ensure Canonical Prompt ("Global Default Spam Check") Exists
        canonical_prompt_name = "Global Default Spam Check"
        canonical_prompt_text = (
            "Analyze the following message. Is it spam? If yes, provide a very brief (under 100 characters) "
            "reason for the ban, suitable for public display in a chat group. "
            "The chat group is for general discussion, but unsolicited advertising, "
            "scams, or irrelevant content are considered spam.\n\n<message>\n{message_text}\n</message>"
        )
        canonical_prompt = await session.scalar(select(Prompt).where(Prompt.name == canonical_prompt_name))
        if not canonical_prompt:
            canonical_prompt = Prompt(
                name=canonical_prompt_name,
                text=canonical_prompt_text,
                is_global_default=False,  # Default to False, logic below will manage it
                created_at=datetime.datetime.now(timezone.utc)
            )
            session.add(canonical_prompt)
            await session.flush()
            logger.info(f"Created canonical prompt: '{canonical_prompt.name}' (ID: {canonical_prompt.id}).")

        # 3. Determine and Set Global Default Prompt in GlobalBotSettings and Prompt table
        active_default_prompt_id_to_set = None
        user_chosen_default_prompt = None

        if gs.default_prompt_id:
            user_chosen_default_prompt = await session.get(Prompt, gs.default_prompt_id)
            if user_chosen_default_prompt:
                active_default_prompt_id_to_set = user_chosen_default_prompt.id
                logger.info(f"Preserving user-configured global default prompt: '{user_chosen_default_prompt.name}' (ID: {user_chosen_default_prompt.id}).")
            else:
                logger.warning(f"GlobalSettings.default_prompt_id ({gs.default_prompt_id}) points to a non-existent prompt. Will reset to canonical.")
                gs.default_prompt_id = None # Clear invalid ID

        if not active_default_prompt_id_to_set: # No valid user default, or gs.default_prompt_id was initially None
            active_default_prompt_id_to_set = canonical_prompt.id
            gs.default_prompt_id = canonical_prompt.id # Update GlobalSettings
            logger.info(f"Setting canonical prompt '{canonical_prompt.name}' (ID: {canonical_prompt.id}) as global default.")

        # Ensure the chosen active default prompt has its is_global_default flag set to True
        # and all other prompts have it set to False.
        await session.execute(
            update(Prompt)
            .where(Prompt.id == active_default_prompt_id_to_set)
            .values(is_global_default=True)
        )
        await session.execute(
            update(Prompt)
            .where(Prompt.id != active_default_prompt_id_to_set)
            .values(is_global_default=False)
        )
        await session.flush()


        # 4. Ensure Canonical LLM Model ("Claude 3 Haiku") Exists
        canonical_model_name = "Claude 3 Haiku"
        canonical_model_api_id = "claude-3-haiku-20240307"
        canonical_model_provider = "Anthropic"

        canonical_model = await session.scalar(
            select(LLMModel).where(LLMModel.name == canonical_model_name,
                                   LLMModel.api_identifier == canonical_model_api_id,
                                   LLMModel.provider == canonical_model_provider)
        )
        if not canonical_model:
            canonical_model = LLMModel(name=canonical_model_name, api_identifier=canonical_model_api_id,
                                       provider=canonical_model_provider,
                                       created_at=datetime.datetime.now(timezone.utc))
            session.add(canonical_model)
            await session.flush()
            logger.info(f"Created canonical LLMModel: '{canonical_model.name}' (ID: {canonical_model.id}).")

        # 5. Determine and Set Global Default Model in GlobalBotSettings
        user_chosen_default_model = None
        if gs.default_model_id:
            user_chosen_default_model = await session.get(LLMModel, gs.default_model_id)
            if user_chosen_default_model:
                logger.info(f"Preserving user-configured global default model: '{user_chosen_default_model.name}' (ID: {user_chosen_default_model.id}).")
            else:
                logger.warning(f"GlobalSettings.default_model_id ({gs.default_model_id}) points to a non-existent model. Will reset to canonical.")
                gs.default_model_id = None # Clear invalid ID

        if not gs.default_model_id: # No valid user default, or gs.default_model_id was initially None
            gs.default_model_id = canonical_model.id # Update GlobalSettings
            logger.info(f"Setting canonical model '{canonical_model.name}' (ID: {canonical_model.id}) as global default.")

        await session.flush() # Persist any changes to gs

    logger.info("Default data initialization complete (user configuration preserved where valid).")


async def get_monitored_chat_ids() -> list[int]:
    async with get_db_session() as session:
        result = await session.execute(select(MonitoredGroup.chat_id))
        return [cid for cid, in result.all()]