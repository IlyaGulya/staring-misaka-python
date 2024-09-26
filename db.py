import datetime

from sqlalchemy import Integer, DateTime, create_engine
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
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    def __repr__(self) -> str:
        return (
            f"PendingBanRequest(id={self.id!r}, admin_message_id={self.admin_message_id!r}, "
            f"sender_id={self.sender_id!r}, original_chat_id={self.original_chat_id!r}, "
            f"original_message_id={self.original_message_id!r}, created_at={self.created_at!r})"
        )


def create_session() -> Session:
    engine = create_engine(f'sqlite:///{DB_PATH}', echo=False)
    Session = sessionmaker(bind=engine)
    session = Session()

    Base.metadata.create_all(engine)

    return session
