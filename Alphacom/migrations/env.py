import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base

# Migrations run as the table owner, never as the app's restricted "api" role.
URL = os.getenv("MIGRATIONS_DATABASE_URL", "postgresql+asyncpg://postgres:postgres-dev-password@localhost:55432/alphacom")


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def main() -> None:
    engine = create_async_engine(URL)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=URL, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(main())
