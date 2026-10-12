"""Prepare an isolated app.main database before startup integration tests."""

from pathlib import Path
import sqlite3
import tempfile

from app.migrations import migrate


def prepare_nexo_database(main) -> None:
    path = Path(main.DB_PATH).expanduser().absolute()
    temp_root = Path(tempfile.gettempdir()).resolve()
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_relative_to(temp_root) or not resolved.parent.is_dir():
        raise ValueError("app.main test database must be below the temporary root")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        migrate(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
