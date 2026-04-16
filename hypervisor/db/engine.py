"""
hypervisor/db/engine.py

Async SQLAlchemy engine + session factory backed by aiosqlite.

DATABASE_URL resolves to:
  - /app/data/arka.db  inside Docker  (WORKDIR /app)
  - ./data/arka.db     in local dev    (relative to project root)

Tables are created from data/db/schema.sql on first run via init_db().
The PRAGMA statements in schema.sql (journal_mode=WAL, synchronous=NORMAL)
are executed separately after the CREATE TABLE statements.
"""

import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Absolute path so it works regardless of cwd
_DB_DIR = Path(__file__).parent.parent.parent / "data"
_DB_PATH = _DB_DIR / "arka.db"
DATABASE_URL = f"sqlite+aiosqlite:///{_DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db() -> None:
    """
    Bootstrap tables from data/db/schema.sql.
    Safe to call on every startup — all statements use CREATE TABLE IF NOT EXISTS.
    PRAGMA statements are issued last.
    """
    schema_path = Path(__file__).parent.parent.parent / "data" / "db" / "schema.sql"
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.sql not found at {schema_path}")

    raw = schema_path.read_text()

    # Split on ";" and bucket into DDL vs PRAGMA
    ddl_stmts: list[str] = []
    pragma_stmts: list[str] = []

    for chunk in raw.split(";"):
        stmt = chunk.strip()
        if not stmt:
            continue
        if stmt.upper().startswith("PRAGMA"):
            pragma_stmts.append(stmt)
        else:
            ddl_stmts.append(stmt)

    async with engine.begin() as conn:
        for stmt in ddl_stmts:
            await conn.execute(text(stmt))
        # PRAGMAs must run outside a transaction on some drivers; execute them
        # individually after the DDL transaction commits.

    # SQLite PRAGMAs for WAL mode — run outside the DDL transaction
    async with engine.begin() as conn:
        for stmt in pragma_stmts:
            await conn.execute(text(stmt))
