from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from tempfile import NamedTemporaryFile
from typing import cast

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.core.catalog import (
    CatalogColumn,
    CatalogForeignKey,
    CatalogTable,
    CatalogTableProfile,
    SampleRow,
)
from app.core.types import JsonValue

_SENSITIVE_COLUMN_PARTS = (
    "email",
    "phone",
    "mobile",
    "address",
    "name",
    "salary",
    "ssn",
    "national_id",
    "birth",
    "password",
    "secret",
    "token",
)


def mask_sample_value(column_name: str, value: object) -> JsonValue:
    lowered = column_name.lower()
    if any(part in lowered for part in _SENSITIVE_COLUMN_PARTS):
        return "[REDACTED]" if value is not None else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class PostgresCatalogAdapter:
    def __init__(
        self,
        dsn: str,
        *,
        statement_timeout_ms: int = 10_000,
        ssl_ca_certificate: str | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("A catalog DSN is required")
        if statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be positive")
        self._dsn = dsn
        self._statement_timeout_ms = statement_timeout_ms
        self._ssl_ca_certificate = ssl_ca_certificate

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        if self._ssl_ca_certificate is None:
            with psycopg.connect(self._dsn) as connection:
                yield connection
            return
        with NamedTemporaryFile(mode="w", encoding="utf-8") as certificate_file:
            certificate_file.write(self._ssl_ca_certificate)
            certificate_file.flush()
            dsn = make_conninfo(self._dsn, sslrootcert=certificate_file.name)
            with psycopg.connect(dsn) as connection:
                yield connection

    def list_tables(self, schemas: tuple[str, ...] = ()) -> tuple[CatalogTable, ...]:
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
        ):
            self._set_read_only(cursor)
            cursor.execute(
                """
                SELECT tables.table_schema, tables.table_name, tables.table_type,
                       COALESCE(classes.reltuples::bigint, 0),
                       COUNT(columns.column_name)::integer
                FROM information_schema.tables AS tables
                LEFT JOIN pg_namespace AS namespaces
                  ON namespaces.nspname = tables.table_schema
                LEFT JOIN pg_class AS classes
                  ON classes.relnamespace = namespaces.oid
                 AND classes.relname = tables.table_name
                LEFT JOIN information_schema.columns AS columns
                  ON columns.table_schema = tables.table_schema
                 AND columns.table_name = tables.table_name
                WHERE table_type = 'BASE TABLE'
                  AND tables.table_schema NOT IN ('information_schema', 'pg_catalog')
                  AND left(tables.table_schema, 3) <> 'pg_'
                  AND (%s::text[] = ARRAY[]::text[] OR tables.table_schema = ANY(%s))
                GROUP BY tables.table_schema, tables.table_name, tables.table_type,
                         classes.reltuples
                ORDER BY tables.table_schema, tables.table_name
                """,
                (list(schemas), list(schemas)),
            )
            rows = cursor.fetchall()
        return tuple(
            CatalogTable(
                schema_name=str(row[0]),
                table_name=str(row[1]),
                table_type=str(row[2]),
                approx_rows=max(cast(int, row[3]), 0),
                column_count=cast(int, row[4]),
            )
            for row in rows
        )

    def profile_tables(
        self,
        tables: tuple[tuple[str, str], ...],
        *,
        sample_limit: int = 3,
    ) -> tuple[CatalogTableProfile, ...]:
        if sample_limit < 0 or sample_limit > 10:
            raise ValueError("sample_limit must be between 0 and 10")
        results: list[CatalogTableProfile] = []
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
        ):
            self._set_read_only(cursor)
            for schema_name, table_name in tables:
                results.append(
                    self._profile_one(
                        cursor,
                        schema_name,
                        table_name,
                        sample_limit=sample_limit,
                    )
                )
        return tuple(results)

    def _set_read_only(self, cursor: psycopg.Cursor[tuple[object, ...]]) -> None:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(self._statement_timeout_ms),),
        )

    def _profile_one(
        self,
        cursor: psycopg.Cursor[tuple[object, ...]],
        schema_name: str,
        table_name: str,
        *,
        sample_limit: int,
    ) -> CatalogTableProfile:
        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema_name, table_name),
        )
        column_rows = cursor.fetchall()
        if not column_rows:
            raise ValueError(f"Table {schema_name}.{table_name} is not visible")

        cursor.execute(
            """
            SELECT con.contype, con.conname, local_att.attname,
                   local_key.ordinality, remote_ns.nspname, remote_table.relname,
                   remote_att.attname
            FROM pg_constraint AS con
            JOIN pg_class AS local_table ON local_table.oid = con.conrelid
            JOIN pg_namespace AS local_ns ON local_ns.oid = local_table.relnamespace
            CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY
                AS local_key(attnum, ordinality)
            JOIN pg_attribute AS local_att
              ON local_att.attrelid = local_table.oid
             AND local_att.attnum = local_key.attnum
            LEFT JOIN LATERAL unnest(con.confkey) WITH ORDINALITY
                AS remote_key(attnum, ordinality)
              ON remote_key.ordinality = local_key.ordinality
            LEFT JOIN pg_class AS remote_table ON remote_table.oid = con.confrelid
            LEFT JOIN pg_namespace AS remote_ns
              ON remote_ns.oid = remote_table.relnamespace
            LEFT JOIN pg_attribute AS remote_att
              ON remote_att.attrelid = remote_table.oid
             AND remote_att.attnum = remote_key.attnum
            WHERE local_ns.nspname = %s AND local_table.relname = %s
              AND con.contype IN ('p', 'f')
            ORDER BY con.conname, local_key.ordinality
            """,
            (schema_name, table_name),
        )
        constraints = cursor.fetchall()
        primary_columns = {str(row[2]) for row in constraints if row[0] == "p"}
        foreign_key_rows: dict[str, list[tuple[object, ...]]] = defaultdict(list)
        for row in constraints:
            if row[0] == "f":
                foreign_key_rows[str(row[1])].append(row)

        columns = tuple(
            CatalogColumn(
                name=str(row[0]),
                data_type=str(row[1]),
                nullable=str(row[2]) == "YES",
                is_primary_key=str(row[0]) in primary_columns,
            )
            for row in column_rows
        )
        foreign_keys = tuple(
            CatalogForeignKey(
                name=constraint_name,
                columns=tuple(str(row[2]) for row in rows),
                referenced_schema=str(rows[0][4]),
                referenced_table=str(rows[0][5]),
                referenced_columns=tuple(str(row[6]) for row in rows),
            )
            for constraint_name, rows in sorted(foreign_key_rows.items())
        )

        table = CatalogTable(schema_name, table_name, "BASE TABLE")
        quoted_table = sql.Identifier(schema_name, table_name)
        cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(quoted_table))
        count_row = cursor.fetchone()
        if count_row is None:
            raise RuntimeError(f"Count query returned no row for {schema_name}.{table_name}")
        row_count = int(cast(int, count_row[0]))

        sample_rows: tuple[SampleRow, ...] = ()
        if sample_limit:
            identifiers = sql.SQL(", ").join(sql.Identifier(column.name) for column in columns)
            cursor.execute(
                sql.SQL("SELECT {} FROM {} LIMIT %s").format(identifiers, quoted_table),
                (sample_limit,),
            )
            samples = cursor.fetchall()
            sample_rows = tuple(
                {
                    column.name: mask_sample_value(column.name, value)
                    for column, value in zip(columns, row, strict=True)
                }
                for row in samples
            )

        return CatalogTableProfile(
            table=table,
            columns=columns,
            foreign_keys=foreign_keys,
            row_count=row_count,
            sample_rows=sample_rows,
        )
