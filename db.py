import datetime

from sqlalchemy import Integer, DateTime, create_engine, Text, Boolean, func, UniqueConstraint, Index, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, Session


class Base(DeclarativeBase):
    pass


class NewUser(Base):
    __tablename__ = 'new_users'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    join_time: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Composite unique constraint: user can only be monitored once per chat
    # Performance index for user lookups
    __table_args__ = (
        UniqueConstraint("user_id", "chat_id", name="uq_new_users_user_chat"),
        Index("ix_new_users_user_chat", "user_id", "chat_id"),
    )

    def __repr__(self) -> str:
        return f"NewUser(id={self.id!r}, user_id={self.user_id!r}, chat_id={self.chat_id!r}, join_time={self.join_time!r})"


class PendingBanRequest(Base):
    __tablename__ = 'pending_ban_requests'

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_message_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    sender_id: Mapped[int] = mapped_column(Integer, nullable=False)
    original_chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    original_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Performance index for admin sender lookups
    __table_args__ = (
        Index("ix_pending_ban_requests_sender_id", "sender_id"),
    )

    def __repr__(self) -> str:
        return (
            f"PendingBanRequest(id={self.id!r}, admin_message_id={self.admin_message_id!r}, "
            f"sender_id={self.sender_id!r}, original_chat_id={self.original_chat_id!r}, "
            f"original_message_id={self.original_message_id!r}, message_text={self.message_text!r}, "
            f"created_at={self.created_at!r})"
        )


class BannedUser(Base):
    __tablename__ = 'banned_users'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_name: Mapped[str] = mapped_column(Text, nullable=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    banned_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Performance indexes for user ban lookups
    __table_args__ = (
        Index("ix_banned_users_user_id", "user_id"),
        Index("ix_banned_users_user_chat", "user_id", "chat_id"),
    )

    def __repr__(self) -> str:
        return (
            f"BannedUser(id={self.id!r}, user_id={self.user_id!r}, user_name={self.user_name!r}, "
            f"chat_id={self.chat_id!r}, message_text={self.message_text!r}, banned_at={self.banned_at!r})"
        )


class ApprovedUser(Base):
    __tablename__ = 'approved_users'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Composite unique constraint: user can only be approved once per chat
    # Performance index for user lookups
    __table_args__ = (
        UniqueConstraint("user_id", "chat_id", name="uq_approved_users_user_chat"),
        Index("ix_approved_users_user_chat", "user_id", "chat_id"),
    )

    def __repr__(self) -> str:
        return (
            f"ApprovedUser(id={self.id!r}, user_id={self.user_id!r}, "
            f"chat_id={self.chat_id!r}, approved_at={self.approved_at!r})"
        )


class AdminSettings(Base):
    __tablename__ = 'admin_settings'

    id: Mapped[int] = mapped_column(primary_key=True)
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self) -> str:
        return f"AdminSettings(id={self.id!r}, require_approval={self.require_approval!r})"


class GroupSettings(Base):
    __tablename__ = 'group_settings'

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Performance index for chat lookups
    __table_args__ = (
        Index("ix_group_settings_chat_id", "chat_id"),
    )

    def __repr__(self) -> str:
        return (
            f"GroupSettings(id={self.id!r}, chat_id={self.chat_id!r}, "
            f"enabled={self.enabled!r}, updated_at={self.updated_at!r})"
        )


class MessageQueue(Base):
    __tablename__ = 'message_queue'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default='pending')  # pending, processing, completed, failed
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_retry_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    processed_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    spam_result: Mapped[bool] = mapped_column(Boolean, nullable=True)

    # Unique constraint to prevent duplicate messages from same user/chat/message
    # Performance indexes for queue processing hot paths
    __table_args__ = (
        UniqueConstraint("user_id", "chat_id", "message_id", name="uq_message_queue_triplet"),
        Index("ix_message_queue_status_nextretry_created", "status", "next_retry_at", "created_at"),
        Index("ix_message_queue_status_processed_at", "status", "processed_at"),
        Index("ix_message_queue_user_chat", "user_id", "chat_id"),
    )

    def __repr__(self) -> str:
        return (
            f"MessageQueue(id={self.id!r}, user_id={self.user_id!r}, chat_id={self.chat_id!r}, "
            f"message_id={self.message_id!r}, status={self.status!r}, retry_count={self.retry_count!r})"
        )


def make_session_factory(config):
    """Create a session factory (sessionmaker) with the given configuration.

    This returns a sessionmaker that can be used to create new sessions as needed,
    which is better for managing database connections across async handlers.
    """
    # Configure SQLite for better concurrency with WAL mode and longer timeout
    engine = create_engine(
        f'sqlite:///{config.db_path}',
        echo=False,
        pool_pre_ping=True,  # Verify connections before using
        pool_recycle=3600  # Recycle connections after 1 hour
    )

    # Set up SQLite optimizations for concurrency
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging for better concurrency
        cursor.execute("PRAGMA busy_timeout=30000")  # 30 second timeout in milliseconds
        cursor.execute("PRAGMA synchronous=NORMAL")  # Balance between safety and performance
        cursor.execute("PRAGMA cache_size=-64000")  # 64MB cache
        cursor.close()

    # Create tables
    Base.metadata.create_all(engine)

    # Return sessionmaker with expire_on_commit=False for better async handling
    return sessionmaker(bind=engine, expire_on_commit=False)


def initialize_database(session_factory, config):
    """Initialize database with default settings.

    This should be called once at startup to ensure required data exists.
    """
    with session_factory() as session:
        # Ensure we have a default AdminSettings entry
        admin_settings = session.query(AdminSettings).first()
        if not admin_settings:
            admin_settings = AdminSettings(require_approval=False)
            session.add(admin_settings)
            session.commit()

        # Ensure we have GroupSettings entries for all tracked chats (enabled by default)
        for chat_id in config.tracking_chat_ids:
            group_settings = session.query(GroupSettings).filter_by(chat_id=chat_id).first()
            if not group_settings:
                group_settings = GroupSettings(chat_id=chat_id, enabled=True)
                session.add(group_settings)
        session.commit()


# Backward compatibility: create_session returns a session
def create_session(config):
    """Create a session for backward compatibility with existing tests.

    For new code, use make_session_factory() and initialize_database() instead.
    """
    session_factory = make_session_factory(config)
    session = session_factory()
    initialize_database(session_factory, config)
    return session
