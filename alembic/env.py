from logging.config import fileConfig

from alembic import context

from app import models  # noqa: F401  (registers tables)
from app.config import database_url
from app.db import Base, make_engine

if context.config.config_file_name:
    fileConfig(context.config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = context.config.attributes.get("engine") or make_engine()
    with engine.connect() as conn:
        # render_as_batch stays on: the existing migrations were written in batch style (a no-op on PostgreSQL)
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
