"""Connecting a data source. To support a new kind of source, add a Connector here, plus its
declared-keys query in profiler.py if it has one. Everything else works from the catalog.

Credentials never go into `CREATE CATALOG`, because Trino logs that statement in full. They go
into a file on a volume only Trino and this service can read, and the statement names the file.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import quote

import psycopg
import pymysql
from cryptography.fernet import Fernet
from pydantic import BaseModel, Field, SecretStr, field_validator

from agent.harness.errors import InvalidRequest

HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
DATABASE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
TRINO_UID = 1000
CONNECT_TIMEOUT_SECONDS = 8
WRITE_PRIVILEGES = re.compile(r"\b(ALL PRIVILEGES|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b")


class Connection(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["postgres", "mysql"]
    host: str
    port: int = Field(ge=1, le=65535)
    database: str
    username: str = Field(min_length=1, max_length=120)
    password: SecretStr

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        # These reach a JDBC URL, where a stray ? or & would add driver options.
        if not HOST.match(value):
            raise ValueError("that doesn't look like a host name")
        return value

    @field_validator("database")
    @classmethod
    def _database(cls, value: str) -> str:
        if not DATABASE.match(value):
            raise ValueError("that doesn't look like a database name")
        return value


class TestResult(BaseModel):
    read_only: bool
    detail: str = ""


def _test_postgres(connection: Connection) -> TestResult:
    with psycopg.connect(host=connection.host, port=connection.port, dbname=connection.database, user=connection.username,
                         password=connection.password.get_secret_value(), connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        superuser = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        writable = conn.execute("""
            SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE')""").fetchone()[0]
        creatable = conn.execute("""
            SELECT COUNT(*) FROM pg_namespace n
            WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
              AND has_schema_privilege(n.oid, 'CREATE')""").fetchone()[0]
    if superuser and superuser[0]:
        return TestResult(read_only=False, detail="This login is a superuser.")
    if writable or creatable:
        return TestResult(read_only=False, detail=f"This login can write to {writable} tables and create in {creatable} schemas.")
    return TestResult(read_only=True)


def _test_mysql(connection: Connection) -> TestResult:
    conn = pymysql.connect(host=connection.host, port=connection.port, database=connection.database, user=connection.username,
                           password=connection.password.get_secret_value(), connect_timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW GRANTS")
            grants = " ".join(row[0] for row in cur.fetchall())
    finally:
        conn.close()
    found = sorted(set(WRITE_PRIVILEGES.findall(grants)))
    return TestResult(read_only=not found, detail=f"This login has: {', '.join(found)}." if found else "")


@dataclass(frozen=True)
class Connector:
    trino_connector: str
    dialect: str
    default_port: int
    jdbc_url: Callable[[Connection], str]
    superset_driver: str
    test: Callable[[Connection], TestResult]


CONNECTORS = {
    "postgres": Connector("postgresql", "postgres", 5432, lambda c: f"jdbc:postgresql://{c.host}:{c.port}/{c.database}",
                          "postgresql+psycopg2", _test_postgres),
    "mysql": Connector("mysql", "mysql", 3306, lambda c: f"jdbc:mysql://{c.host}:{c.port}",
                       "mysql+pymysql", _test_mysql),
}


def test_connection(connection: Connection) -> TestResult:
    try:
        return CONNECTORS[connection.kind].test(connection)
    except (psycopg.Error, pymysql.Error, OSError) as error:
        # Driver messages name the host and user, never the password, but are cut short anyway.
        first_line = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
        raise InvalidRequest(f"couldn't connect: {first_line[:200]}") from error


def _properties_value(value: str) -> str:
    """Escape a value for a Java properties file, or a password could add its own settings."""
    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    return re.sub(r"([:=#!])", r"\\\1", escaped)


def write_credentials(connection: Connection, *, catalog: str, directory: str) -> None:
    """Write the file Trino reads the login from, readable only by Trino's user."""
    path = Path(directory) / f"{catalog}.properties"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(f"connection-user={_properties_value(connection.username)}\n")
        file.write(f"connection-password={_properties_value(connection.password.get_secret_value())}\n")
    if os.geteuid() == 0:
        os.chown(path, TRINO_UID, TRINO_UID)


def create_catalog_sql(connection: Connection, *, catalog: str, trino_secrets_dir: str = "/data/trino/secrets") -> str:
    """Holds no secret: this text ends up in Trino's logs and web UI."""
    connector = CONNECTORS[connection.kind]
    return (f'CREATE CATALOG {catalog} USING {connector.trino_connector} WITH ('
            f'"connection-url" = \'{connector.jdbc_url(connection)}\', '
            f'"credential-provider.type" = \'FILE\', '
            f'"connection-credential-file" = \'{trino_secrets_dir}/{catalog}.properties\')')


def superset_uri(connection: Connection) -> str:
    driver = CONNECTORS[connection.kind].superset_driver
    password = quote(connection.password.get_secret_value(), safe="")
    return f"{driver}://{quote(connection.username, safe='')}:{password}@{connection.host}:{connection.port}/{connection.database}"


def encrypt_credentials(connection: Connection, key: str) -> str:
    secret = {"username": connection.username, "password": connection.password.get_secret_value()}
    return Fernet(key).encrypt(json.dumps(secret).encode()).decode()


def decrypt_credentials(token: str, key: str) -> dict[str, str]:
    return json.loads(Fernet(key).decrypt(token.encode()))
