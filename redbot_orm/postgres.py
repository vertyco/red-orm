import asyncio
import logging
from pathlib import Path

import asyncpg
from discord.ext import commands
from piccolo.engine.postgres import PostgresEngine
from piccolo.table import Table

from .common import find_piccolo_executable, get_root, is_unc_path, run_shell
from .errors import ConnectionTimeoutError, DirectoryError, UNCPathError

log = logging.getLogger("red.orm.postgres")


async def register_cog(
    cog_instance: commands.Cog | Path,
    tables: list[type[Table]],
    config: dict,
    *,
    trace: bool = False,
    max_size: int = 20,
    min_size: int = 1,
    skip_migrations: bool = False,
    extensions: list[str] = ("uuid-ossp",),
) -> PostgresEngine:
    """Registers a Discord cog with a database connection and runs migrations.

    Args:
        cog_instance (commands.Cog | Path): The instance/path of the cog to register.
        tables (list[type[Table]]): List of Piccolo Table classes to associate with the database engine.
        config (dict): Configuration dictionary containing database connection details.
        trace (bool, optional): Whether to enable tracing for migrations. Defaults to False.
        max_size (int, optional): Maximum size of the database connection pool. Defaults to 20.
        min_size (int, optional): Minimum size of the database connection pool. Defaults to 1.
        skip_migrations (bool, optional): Whether to skip running migrations. Defaults to False.
        extensions (list[str], optional): List of Postgres extensions to enable. Defaults to ("uuid-ossp",).

    Raises:
        UNCPathError: If the cog path is a UNC path, which is not supported.
        DirectoryError: If the cog files are not in a valid directory.

    Returns:
        PostgresEngine: The database engine associated with the registered cog.
    """
    cog_path = get_root(cog_instance)
    if is_unc_path(cog_path):
        raise UNCPathError(
            f"UNC paths are not supported, please move the cog's location: {cog_path}"
        )
    if not cog_path.is_dir():
        raise DirectoryError(f"Cog files are not in a valid directory: {cog_path}")

    if await ensure_database_exists(cog_instance, config):
        log.info(f"New database created for {cog_path.stem}")

    if not skip_migrations:
        log.info("Running migrations, if any")
        result = await run_migrations(cog_instance, config, trace)
        if "No migrations need to be run" in result:
            log.info("No migrations needed ✓")
        else:
            log.info(f"Migration result...\n{result}")
            if "Traceback" in result:
                diagnoses = await diagnose_issues(cog_instance, config)
                log.error(diagnoses + "\nOne or more migrations failed to run!")

    temp_config = config.copy()
    temp_config["database"] = db_name(cog_instance)
    log.debug("Fetching database engine")
    engine = await acquire_db_engine(temp_config, extensions)
    log.debug("Database engine acquired, starting pool")
    await engine.start_connection_pool(min_size=min_size, max_size=max_size)
    log.info("Database connection pool started ✓")
    for table_class in tables:
        table_class._meta.db = engine
    return engine


async def run_migrations(
    cog_instance: commands.Cog | Path,
    config: dict,
    trace: bool = False,
) -> str:
    """Runs database migrations for a given Discord cog.

    Args:
        cog_instance (commands.Cog | Path): The instance of the cog for which to run migrations.
        config (dict): Database connection details.
        trace (bool, optional): Whether to enable tracing for migrations. Defaults to False.

    Returns:
        str: The result of the migration process, including any output messages.
    """
    temp_config = config.copy()
    temp_config["database"] = db_name(cog_instance)
    commands = [
        str(find_piccolo_executable()),
        "migrations",
        "forwards",
        get_root(cog_instance).stem,
    ]
    if trace:
        commands.append("--trace")
    return await run_shell(cog_instance, commands, False)


async def reverse_migration(
    cog_instance: commands.Cog | Path,
    config: dict,
    timestamp: str,
    trace: bool = False,
) -> str:
    """Reverses a database migration for a given Discord cog to a specific timestamp.

    Args:
        cog_instance (commands.Cog | Path): The instance of the cog for which to reverse the migration.
        config (dict): Configuration dictionary containing database connection details.
        timestamp (str): The timestamp to which the migration should be reversed.
        trace (bool, optional): Whether to enable tracing for migrations. Defaults to False.

    Returns:
        str: The result of the reverse migration process, including any output messages.
    """
    temp_config = config.copy()
    temp_config["database"] = db_name(cog_instance)
    commands = [
        str(find_piccolo_executable()),
        "migrations",
        "backwards",
        get_root(cog_instance).stem,
        timestamp,
    ]
    if trace:
        commands.append("--trace")
    return await run_shell(cog_instance, commands, False)


async def create_migrations(
    cog_instance: commands.Cog | Path,
    config: dict,
    trace: bool = False,
    description: str = None,
) -> str:
    """Creates new database migrations for the cog

    THIS SHOULD BE RUN MANUALLY!

    Args:
        cog_instance (commands.Cog | Path): The instance of the cog for which to create migrations.
        config (dict): Configuration dictionary containing database connection details.
        trace (bool, optional): Whether to enable tracing for migrations. Defaults to False.
        description (str, optional): Description of the migration. Defaults to None.

    Returns:
        str: The result of the migration creation process, including any output messages.
    """
    temp_config = config.copy()
    temp_config["database"] = db_name(cog_instance)
    commands = [
        str(find_piccolo_executable()),
        "migrations",
        "new",
        get_root(cog_instance).stem,
        "--auto",
    ]
    if trace:
        commands.append("--trace")
    if description is not None:
        commands.append(f"--desc={description}")
    return await run_shell(cog_instance, commands, True)


async def diagnose_issues(cog_instance: commands.Cog | Path, config: dict) -> str:
    """Diagnoses potential issues with the database setup for a given Discord cog.

    Args:
        cog_instance (commands.Cog | Path): The instance of the cog to diagnose.
        config (dict): Configuration dictionary containing database connection details.

    Returns:
        str: The result of the diagnosis process, including any output messages.
    """
    piccolo_path = find_piccolo_executable()
    temp_config = config.copy()
    temp_config["database"] = db_name(cog_instance)
    diagnoses = await run_shell(
        cog_instance,
        [str(piccolo_path), "--diagnose"],
        False,
    )
    check = await run_shell(
        cog_instance,
        [str(piccolo_path), "migrations", "check"],
        False,
    )
    return f"{diagnoses}\n{check}"


async def ensure_database_exists(
    cog_instance: commands.Cog | Path,
    config: dict,
) -> bool:
    """Create a database for the cog if it doesn't exist.

    Args:
        cog_instance (commands.Cog | Path): The cog instance
        config (dict): the database connection information

    Returns:
        bool: True if a new database was created
    """
    conn = await asyncpg.connect(**config, timeout=10)
    database_name = db_name(cog_instance)
    try:
        databases = await conn.fetch("SELECT datname FROM pg_database;")
        if database_name not in [db["datname"] for db in databases]:
            await conn.execute(f"CREATE DATABASE {database_name};")
            return True
    finally:
        await conn.close()
    return False


async def acquire_db_engine(config: dict, extensions: list[str]) -> PostgresEngine:
    """Acquire a database engine
    The PostgresEngine constructor is blocking and must be run in a separate thread.

    Args:
        config (dict): The database connection information
        extensions (list[str]): The Postgres extensions to enable

    Returns:
        PostgresEngine: The database engine
    """

    async def get_conn():
        return await asyncio.to_thread(
            PostgresEngine,
            config=config,
            extensions=extensions,
        )

    try:
        return await asyncio.wait_for(get_conn(), timeout=10)
    except asyncio.TimeoutError:
        raise ConnectionTimeoutError("Database connection timed out")


def db_name(cog_instance: commands.Cog | Path) -> str:
    """Get the name of the database for the cog

    Args:
        cog_instance (commands.Cog | Path): The cog instance

    Returns:
        str: The database name
    """
    if isinstance(cog_instance, Path):
        return cog_instance.stem.lower()
    return cog_instance.qualified_name.lower()
