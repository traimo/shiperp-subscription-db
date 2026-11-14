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
        sqlite = conn.dialect.name == "sqlite"
        if sqlite:      # batch mode rebuilds tables; FK checks would block dropping a referenced table
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.commit()
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        if sqlite:
            bad = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()
            if bad:
                raise RuntimeError(f"Foreign key check failed after migration: {bad[:5]}")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
