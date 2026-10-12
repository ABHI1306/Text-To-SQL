import psycopg
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from app.core.types import QueryResult, ValidatedQuery


class UnsafeWarehouseQuery(ValueError):
    """Raised when a query is not a single read-only SELECT statement."""


class PostgresWarehouseAdapter:
    def __init__(
        self,
        dsn: str,
        *,
        statement_timeout_ms: int = 10_000,
        max_rows: int = 1_000,
    ) -> None:
        if not dsn:
            raise ValueError("A warehouse DSN is required")
        if statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be positive")
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        self._dsn = dsn
        self._statement_timeout_ms = statement_timeout_ms
        self._max_rows = max_rows

    @staticmethod
    def _ensure_read_only_select(sql: str) -> None:
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
        if max_rows <= 0 or max_rows > self._max_rows:
            raise ValueError(f"max_rows must be between 1 and {self._max_rows}")
        self._ensure_read_only_select(query.sql)

        with psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(self._statement_timeout_ms),),
            )
            cursor.execute(query.sql, dict(query.parameters))
            columns = tuple(column.name for column in cursor.description or ())
            fetched_rows = cursor.fetchmany(max_rows + 1)
            rows = tuple(tuple(row) for row in fetched_rows[:max_rows])

        return QueryResult(columns=columns, rows=rows)
