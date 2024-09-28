import datetime

from sqlalchemy import Integer, DateTime, create_engine, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, Session

from env import DB_PATH


class Base(DeclarativeBase):
    pass


class NewUser(Base):
    __tablename__ = 'new_users'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    join_time: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    def __repr__(self) -> str:
        return f"NewUser(id={self.id!r}, user_id={self.user_id!r}, join_time={self.join_time!r})"


class PendingBanRequest(Base):
    __tablename__ = 'pending_ban_requests'

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_message_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    sender_id: Mapped[int] = mapped_column(Integer, nullable=False)
    original_chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    original_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
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
        DateTime, default=datetime.datetime.utcnow
    )

    def __repr__(self) -> str:
        return (
            f"BannedUser(id={self.id!r}, user_id={self.user_id!r}, user_name={self.user_name!r}, "
            f"chat_id={self.chat_id!r}, message_text={self.message_text!r}, banned_at={self.banned_at!r})"
        )

from sqlalchemy import Boolean
from sqlalchemy.orm import Mapped, mapped_column

class AdminSettings(Base):
    __tablename__ = 'admin_settings'

    id: Mapped[int] = mapped_column(primary_key=True)
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self) -> str:
        return f"AdminSettings(id={self.id!r}, require_approval={self.require_approval!r})"

def create_session() -> Session:
    engine = create_engine(f'sqlite:///{DB_PATH}', echo=False)
    Session = sessionmaker(bind=engine)
    session = Session()

    Base.metadata.create_all(engine)

    # Ensure we have a default AdminSettings entry
    admin_settings = session.query(AdminSettings).first()
    if not admin_settings:
        admin_settings = AdminSettings(require_approval=False)
        session.add(admin_settings)
        session.commit()

    return session
