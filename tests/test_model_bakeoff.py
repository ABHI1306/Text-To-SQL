from scripts.model_bakeoff import collect_tables, is_allowed_query


def test_model_harness_accepts_expected_local_schema() -> None:
    sql = "SELECT COUNT(*) FROM ecommerce.orders"
    tables = collect_tables(sql)

    assert tables == {"orders"}
    assert is_allowed_query(sql, tables)


def test_model_harness_rejects_unknown_schema() -> None:
    sql = "SELECT COUNT(*) FROM eecommerce.orders"

    assert not is_allowed_query(sql, collect_tables(sql))


def test_model_harness_rejects_unqualified_relation() -> None:
    sql = "SELECT COUNT(*) FROM orders"

    assert not is_allowed_query(sql, collect_tables(sql))


def test_model_harness_rejects_write_cte() -> None:
    sql = "WITH changed AS (DELETE FROM ecommerce.orders RETURNING *) SELECT * FROM changed"

    assert not is_allowed_query(sql, collect_tables(sql))
