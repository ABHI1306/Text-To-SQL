"""PostgreSQL warehouse adapter that runs validated queries in read-only transactions."""

from collections.abc import Generator
from contextlib import contextmanager
from tempfile import NamedTemporaryFile

import psycopg
from psycopg.conninfo import make_conninfo
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from app.adapters.warehouse.pool import get_pool
from app.core.types import QueryResult, ValidatedQuery


class UnsafeWarehouseQuery(ValueError):
    """Raised when a query is not a single read-only SELECT statement."""


class PostgresWarehouseAdapter:
    """Executes compiler-validated SELECTs read-only, with a timeout and a row cap."""
    def __init__(
        self,
        dsn: str,
        *,
        statement_timeout_ms: int = 10_000,
        max_rows: int = 1_000,
        ssl_ca_certificate: str | None = None,
        pool_max_size: int = 0,
    ) -> None:
        """Validate the DSN and limits and store the connection settings."""
        if not dsn:
            raise ValueError("A warehouse DSN is required")
        if statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be positive")
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        self._dsn = dsn
        self._statement_timeout_ms = statement_timeout_ms
        self._max_rows = max_rows
        self._ssl_ca_certificate = ssl_ca_certificate
        self._pool_max_size = pool_max_size

    @contextmanager
    def _connect(self) -> Generator[psycopg.Connection[tuple[object, ...]], None, None]:
        """Yield a pooled or direct connection (temporary CA file when one is configured)."""
        if self._ssl_ca_certificate is None:
            pool = get_pool(self._dsn, self._pool_max_size)
            if pool is not None:
                # Each use is one transaction (set_config(..., true) is transaction-local), so
                # nothing a query sets can leak to the next borrower.
                with pool.connection() as connection:
                    yield connection
                return
            with psycopg.connect(self._dsn) as connection:
                yield connection
            return
        with NamedTemporaryFile(mode="w", encoding="utf-8") as certificate_file:
            certificate_file.write(self._ssl_ca_certificate)
            certificate_file.flush()
            dsn = make_conninfo(self._dsn, sslrootcert=certificate_file.name)
            with psycopg.connect(dsn) as connection:
                yield connection

    @staticmethod
    def _ensure_read_only_select(sql: str) -> None:
        """Defence in depth: allow only one SELECT with no writes, INTO or locks."""
        try:
            statements = parse(sql, read="postgres")
        except ParseError as error:
            raise UnsafeWarehouseQuery("Invalid PostgreSQL query") from error

        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise UnsafeWarehouseQuery("Only one SELECT query is allowed")

        query = statements[0]
        if any(
            isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Command))
            for node in query.walk()
        ):
            raise UnsafeWarehouseQuery("Data-changing SQL is not allowed")
        if query.args.get("into") or any(
            isinstance(node, exp.Lock) for node in query.walk()
        ):
            raise UnsafeWarehouseQuery("SELECT INTO and locking SELECTs are not allowed")

    def execute_read_only(
        self,
        query: ValidatedQuery,
        *,
        max_rows: int,
    ) -> QueryResult:
        """Run the query read-only with a statement timeout, fetching at most ``max_rows`` rows."""
        if max_rows <= 0 or max_rows > self._max_rows:
            raise ValueError(f"max_rows must be between 1 and {self._max_rows}")
        # Query text is psycopg pyformat: a literal % is written %%, which only the driver reads.
        self._ensure_read_only_select(query.sql.replace("%%", "%"))

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(self._statement_timeout_ms),),
            )
            cursor.execute(query.sql, dict(query.parameters))
            columns = tuple(column.name for column in cursor.description or ())
            fetched_rows = cursor.fetchmany(max_rows + 1)
            rows = tuple(tuple(row) for row in fetched_rows[:max_rows])
            truncated = len(fetched_rows) > max_rows

        return QueryResult(columns=columns, rows=rows, truncated=truncated)

    def explain(self, query: ValidatedQuery) -> None:
        """Run ``EXPLAIN`` read-only to prove the query plans, without executing it."""
        self._ensure_read_only_select(query.sql.replace("%%", "%"))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(self._statement_timeout_ms),),
            )
            cursor.execute("EXPLAIN (FORMAT JSON) " + query.sql, dict(query.parameters))
            cursor.fetchall()
