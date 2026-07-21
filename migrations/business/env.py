from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
database_url = os.environ.get("AIQ_BUSINESS_DATABASE_URL")
database_url_file = os.environ.get("AIQ_BUSINESS_DATABASE_URL_FILE")
if database_url and database_url_file:
    raise RuntimeError("business database URL and URL file are mutually exclusive")
if database_url_file:
    with open(database_url_file, encoding="utf-8") as stream:
        database_url = stream.read().strip()
if database_url:
    sqlalchemy_url = database_url
    if sqlalchemy_url.startswith("postgresql://"):
        sqlalchemy_url = sqlalchemy_url.replace(
            "postgresql://",
            "postgresql+psycopg://",
            1,
        )
    config.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
