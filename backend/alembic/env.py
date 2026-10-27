from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from app.core.config import Settings, get_settings
from app.core.database import Base, build_engine, get_database_url

# Import model modules so their tables are registered with Base.metadata.
from app.models import ingestion  # noqa: F401


config = context.config

if config.config_file_name is not None:
    fileConfig(
        config.config_file_name,
        disable_existing_loggers=False,
    )

target_metadata = Base.metadata


def migration_settings() -> Settings:
    """Use application settings, with an explicit override for isolated tests."""
    supplied_settings = config.attributes.get("settings")

    if supplied_settings is None:
        return get_settings()

    if not isinstance(supplied_settings, Settings):
        raise TypeError(
            "Alembic's 'settings' attribute must contain a Settings instance."
        )

    return supplied_settings


def run_migrations_offline() -> None:
    """Generate migration SQL without opening a database connection."""
    database_url = get_database_url(migration_settings())

    # Pass the SQLAlchemy URL directly. Do not interpolate credentials
    # through ConfigParser, where percent characters have special meaning.
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=database_url.get_backend_name() == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_with_connection(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # Future autogeneration can use table-copy operations for SQLite
        # changes that cannot be expressed with ordinary ALTER TABLE.
        render_as_batch=connection.dialect.name == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations using the application's database configuration."""
    supplied_connection = config.attributes.get("connection")

    if supplied_connection is not None:
        # The caller owns this connection and its lifetime.
        run_with_connection(supplied_connection)
        return

    engine = build_engine(migration_settings())

    try:
        with engine.connect() as connection:
            run_with_connection(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()