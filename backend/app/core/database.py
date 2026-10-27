from collections.abc import Generator
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from sqlalchemy import DateTime, MetaData, create_engine, event
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.core.config import BACKEND_DIR, Settings, get_settings


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC timestamps consistently in SQLite and PostgreSQL.

    SQLite stores naive UTC values. Results are always returned as
    timezone-aware UTC datetimes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Any,
    ) -> datetime | None:
        if value is None:
            return None

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Database timestamps must be timezone-aware.")

        utc_value = value.astimezone(timezone.utc)
        if dialect.name == "sqlite":
            return utc_value.replace(tzinfo=None)

        return utc_value

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Any,
    ) -> datetime | None:
        if value is None:
            return None

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)


def get_database_url(settings: Settings | None = None) -> URL:
    """Normalize database URLs without opening a connection."""
    config = settings if settings is not None else get_settings()
    url = make_url(config.database_url)

    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")

    if url.get_backend_name() == "sqlite":
        database = url.database

        if database not in {None, "", ":memory:"}:
            from pathlib import Path

            database_path = Path(database).expanduser()
            if not database_path.is_absolute():
                database_path = BACKEND_DIR / database_path

            url = url.set(database=str(database_path.resolve()))

    return url


def build_engine(settings: Settings) -> Engine:
    """Create an engine without creating application tables."""
    url = get_database_url(settings)

    engine_options: dict[str, Any] = {
        "pool_pre_ping": True,
        # Prevent bound SQL values appearing in SQLAlchemy error output.
        "hide_parameters": True,
    }

    if url.get_backend_name() == "sqlite":
        engine_options["connect_args"] = {
            "check_same_thread": False,
            "timeout": 30,
        }

        if url.database in {None, "", ":memory:"}:
            # Share one in-memory SQLite connection across test requests.
            engine_options["poolclass"] = StaticPool
        else:
            from pathlib import Path

            Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    else:
        engine_options["connect_args"] = {"connect_timeout": 10}
        engine_options["pool_size"] = 5
        engine_options["max_overflow"] = 5
        engine_options["pool_timeout"] = 30
        engine_options["pool_recycle"] = 1800

    engine = create_engine(url, **engine_options)

    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def configure_sqlite(
            dbapi_connection: Any,
            connection_record: Any,
        ) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return build_engine(get_settings())


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(),
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def get_db() -> Generator[Session, None, None]:
    """Provide one session per request.

    Services must explicitly commit their own transactions.
    Uncommitted changes are rolled back when the session closes.
    """
    with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise


def dispose_database() -> None:
    """Release pooled connections and clear process-local database caches."""
    get_session_factory.cache_clear()

    if get_engine.cache_info().currsize:
        get_engine().dispose()

    get_engine.cache_clear()
