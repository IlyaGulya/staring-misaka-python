import datetime
from datetime import timezone  # For timezone-aware datetime objects
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# Base class for all SQLAlchemy models
class Base(AsyncAttrs, DeclarativeBase):
    pass


class GlobalBotSettings(Base):
    __tablename__ = 'global_bot_settings'
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    default_prompt_id: Mapped[int | None] = mapped_column(ForeignKey('prompts.id'))
    default_model_id: Mapped[int | None] = mapped_column(ForeignKey('llm_models.id'))
    super_admin_id: Mapped[int] = mapped_column(Integer, nullable=False)

    default_prompt: Mapped[Optional["Prompt"]] = relationship(foreign_keys=[default_prompt_id])
    default_model: Mapped[Optional["LLMModel"]] = relationship(foreign_keys=[default_model_id])


class MonitoredGroup(Base):
    __tablename__ = 'monitored_groups'
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    added_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    added_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=lambda: datetime.datetime.now(timezone.utc))

    require_admin_approval_for_ban: Mapped[bool] = mapped_column(Boolean, default=True)
    pre_ban_message_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    delete_recent_messages_on_ban: Mapped[bool] = mapped_column(Boolean, default=True)
    num_messages_to_delete_on_ban: Mapped[int] = mapped_column(Integer, default=1)

    custom_prompt_id: Mapped[int | None] = mapped_column(ForeignKey('prompts.id'))
    custom_model_id: Mapped[int | None] = mapped_column(ForeignKey('llm_models.id'))

    custom_prompt: Mapped[Optional["Prompt"]] = relationship(foreign_keys=[custom_prompt_id])
    custom_model: Mapped[Optional["LLMModel"]] = relationship(foreign_keys=[custom_model_id])


class Prompt(Base):
    __tablename__ = 'prompts'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_global_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=lambda: datetime.datetime.now(timezone.utc))


class LLMModel(Base):
    __tablename__ = 'llm_models'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    api_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=lambda: datetime.datetime.now(timezone.utc))


class ModelPricing(Base):
    __tablename__ = 'model_pricing'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(ForeignKey('llm_models.id'), nullable=False)
    input_price_per_million_tokens: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    output_price_per_million_tokens: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="USD", nullable=False)
    effective_from_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    effective_to_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    model: Mapped["LLMModel"] = relationship()
    __table_args__ = (UniqueConstraint('model_id', 'effective_from_date', name='uq_model_pricing_period'),)


class NewUser(Base):
    __tablename__ = 'new_users'
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_groups.chat_id'), primary_key=True)
    join_time: Mapped[datetime.datetime] = mapped_column(DateTime, default=lambda: datetime.datetime.now(timezone.utc))


class PendingAdminAction(Base):
    __tablename__ = 'pending_admin_actions'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    admin_message_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    user_to_act_on_id: Mapped[int] = mapped_column(Integer, nullable=False)
    original_chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_groups.chat_id'), nullable=False)
    original_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_text_preview: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_action: Mapped[str] = mapped_column(String(50), default="ban")
    llm_reason_for_action: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=lambda: datetime.datetime.now(timezone.utc))


class BannedUser(Base):
    __tablename__ = 'banned_users'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_groups.chat_id'), nullable=False)
    banned_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    original_message_text_sample: Mapped[str | None] = mapped_column(Text)
    banned_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=lambda: datetime.datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint('user_id', 'chat_id', name='uq_banned_user_chat'),)


class LLMLog(Base):
    __tablename__ = 'llm_logs'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[int | None] = mapped_column(ForeignKey('monitored_groups.chat_id'))
    user_id: Mapped[int | None] = mapped_column(Integer)
    message_id: Mapped[int | None] = mapped_column(Integer)

    prompt_id: Mapped[int | None] = mapped_column(ForeignKey('prompts.id'))
    model_id: Mapped[int] = mapped_column(ForeignKey('llm_models.id'))

    full_prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    raw_request_payload: Mapped[str | None] = mapped_column(Text)
    raw_response_payload: Mapped[str | None] = mapped_column(Text)

    llm_is_spam: Mapped[bool] = mapped_column(Boolean, nullable=False)
    llm_reason: Mapped[str | None] = mapped_column(Text)

    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)

    calculated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    cost_currency: Mapped[str | None] = mapped_column(String(10))

    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime, default=lambda: datetime.datetime.now(timezone.utc))

    prompt: Mapped[Optional["Prompt"]] = relationship()
    model: Mapped["LLMModel"] = relationship()


class QueuedLLMCheck(Base):
    __tablename__ = 'queued_llm_checks'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_context_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    reason_for_queueing: Mapped[str] = mapped_column(Text, nullable=False)
    original_model_id_attempted: Mapped[int | None] = mapped_column(ForeignKey('llm_models.id'))
    original_prompt_id_attempted: Mapped[int | None] = mapped_column(ForeignKey('prompts.id'))

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    queued_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=lambda: datetime.datetime.now(timezone.utc))

    status: Mapped[str] = mapped_column(String(50), default="pending")

    original_model_attempted: Mapped[Optional["LLMModel"]] = relationship(foreign_keys=[original_model_id_attempted])
    original_prompt_attempted: Mapped[Optional["Prompt"]] = relationship(foreign_keys=[original_prompt_id_attempted])

    def __repr__(self) -> str:
        return f"<QueuedLLMCheck(id={self.id}, reason='{self.reason_for_queueing[:30]}...', status='{self.status}')>"
