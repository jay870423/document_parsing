"""Small, explicit PyMySQL adapter used by the existing store methods.

Business methods keep their transactions and return shapes.  Only DB-API details
(placeholders, mapping rows and numeric Decimal values) are adapted here.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor

IntegrityError = pymysql.IntegrityError
OperationalError = pymysql.OperationalError


def _normalise(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class Row(dict[str, Any]):
    """A dict row that also supports the legacy row[0] count access."""

    def __init__(self, values: dict[str, Any]):
        super().__init__((key, _normalise(value)) for key, value in values.items())

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, cursor: DictCursor):
        self._cursor = cursor

    @property
    def lastrowid(self) -> int:
        return int(self._cursor.lastrowid or 0)

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def fetchone(self) -> Row | None:
        row = self._cursor.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self) -> list[Row]:
        return [Row(row) for row in self._cursor.fetchall()]


def placeholders(query: str) -> str:
    """Convert ? placeholders outside SQL strings/identifiers to PyMySQL %s."""
    output: list[str] = []
    quote = ""
    index = 0
    while index < len(query):
        char = query[index]
        if quote:
            output.append(char)
            if char == "\\" and quote in {"'", '"'} and index + 1 < len(query):
                index += 1
                output.append(query[index])
            elif char == quote:
                if index + 1 < len(query) and query[index + 1] == quote:
                    index += 1
                    output.append(query[index])
                else:
                    quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        else:
            output.append(char)
        index += 1
    return "".join(output)


class Connection:
    def __init__(self, raw: pymysql.connections.Connection):
        self.raw = raw

    def execute(self, query: str, params: Any = None) -> Cursor:
        cursor = self.raw.cursor()
        cursor.execute(placeholders(query), params or ())
        return Cursor(cursor)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def raw_connection(settings: Any) -> pymysql.connections.Connection:
    options: dict[str, Any] = {
        "host": settings.mysql_host,
        "port": int(settings.mysql_port),
        "user": settings.mysql_user,
        "database": settings.mysql_database,
        "charset": "utf8mb4",
        "autocommit": False,
        "cursorclass": DictCursor,
        "connect_timeout": int(settings.mysql_connect_timeout),
        "read_timeout": int(settings.mysql_read_timeout),
        "write_timeout": int(settings.mysql_write_timeout),
    }
    options["password"] = settings.mysql_password
    if settings.mysql_unix_socket:
        options["unix_socket"] = settings.mysql_unix_socket
    return pymysql.connect(**options)


@contextmanager
def connect(settings: Any) -> Iterator[Connection]:
    raw = raw_connection(settings)
    conn = Connection(raw)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
