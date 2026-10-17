"""Day 9 failure drills: model down, database down, worker restart mid-job, clock skew, expired tokens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from app.agent.graph import classify_execution_error
from app.auth.service import CLOCK_SKEW_LEEWAY_SECONDS, AuthenticationError, AuthService, TokenError
from app.observability.readiness import run_checks
from tests.test_agent_graph import FakeWarehouse, ScriptedLlm, SqlStateError, run, sql
from tests.test_auth_service import MemoryAuthRepository
from tests.test_scheduler import run_all, runner, world

SECRET = "unit-test-secret-that-is-at-least-32-bytes"
SELECT = sql("SELECT order_id FROM orders")


class OperationalError(Exception):
    """Stands in for psycopg.OperationalError: a driver error with no SQLSTATE."""


# --- model server down ---------------------------------------------------------------------


def test_model_down_gives_a_retryable_failure_and_never_touches_the_database() -> None:
    state, warehouse = run(ScriptedLlm(RuntimeError("connection refused")))
    assert state["failure"].code == "LLM_UNAVAILABLE" and state["failure"].retryable
    assert warehouse.explained == [] and warehouse.executed == []


def test_model_down_in_the_small_model_also_fails_cleanly() -> None:
    class Down:
        def generate(self, *_: Any, **__: Any) -> str:
            raise RuntimeError("down")

    state, warehouse = run(ScriptedLlm(SELECT), small=Down())
    assert state["failure"].code == "LLM_UNAVAILABLE" and warehouse.executed == []


# --- database down -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [OperationalError("connection refused"), ConnectionRefusedError(), TimeoutError(), SqlStateError("08006"),
     SqlStateError("57P03")],
)
def test_database_down_at_execution_is_retryable_and_named(error: Exception) -> None:
    state, _ = run(ScriptedLlm(SELECT), FakeWarehouse(execute_error=error))
    assert state["failure"].code == "DATABASE_UNAVAILABLE" and state["failure"].retryable


def test_database_down_at_the_dry_run_does_not_spend_correction_attempts() -> None:
    llm = ScriptedLlm(SELECT)
    state, warehouse = run(llm, FakeWarehouse(explain_error=OperationalError("connection refused")))
    assert state["failure"].code == "DATABASE_UNAVAILABLE" and state["failure"].retryable
    assert len(llm.prompts) == 1  # one generation, no repair loop against a dead server
    assert warehouse.executed == []


def test_a_refused_or_failed_query_is_not_mistaken_for_an_outage() -> None:
    assert classify_execution_error(SqlStateError("42501")).code == "FORBIDDEN"
    assert classify_execution_error(SqlStateError("XX000")).code == "DRY_RUN_FAILED"
    assert classify_execution_error(SqlStateError("57014")).code == "TIMEOUT"


def test_readiness_reports_each_dependency_without_raising() -> None:
    def down() -> None:
        raise OperationalError("no route")

    assert run_checks({"metadata_db": lambda: None, "model_server": down}) == {
        "metadata_db": "up",
        "model_server": "down",
    }


# --- worker restarted mid-job ----------------------------------------------------------------


def test_a_worker_killed_mid_job_is_resumed_once_by_another_after_the_lease_expires() -> None:
    store, owners, handler, audit = world()
    dying = runner(store, owners, handler, audit, "dying", lease=60)
    dying.poll(lambda claim: None)  # claimed, then the process is killed before it finishes
    survivor = runner(store, owners, handler, audit, "survivor", lease=60)
    assert run_all(survivor) == []  # lease still held: no double run
    store.advance(seconds=61)
    assert run_all(survivor) == ["succeeded"]
    assert [c.attempt for c in handler.runs] == [2]
    store.advance(seconds=3600)
    assert len(run_all(survivor)) <= 1  # later slots run on schedule, the killed one is not repeated
    assert handler.runs[0].attempt == 2


# --- clock skew ------------------------------------------------------------------------------


def _shifted_token(shift: timedelta) -> tuple[AuthService, str]:
    service = AuthService(MemoryAuthRepository(), jwt_secret=SECRET, access_token_ttl_seconds=600)
    token = service.login("demo", "temporary-strong-password", "127.0.0.1").access_token
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_signature": False,
                                                                       "verify_exp": False,
                                                                       "verify_iat": False,
                                                                       "verify_iss": False})
    claims["iat"] += int(shift.total_seconds())
    claims["exp"] += int(shift.total_seconds())
    return service, jwt.encode(claims, SECRET, algorithm="HS256")


def test_a_token_issued_a_few_seconds_in_the_future_is_accepted() -> None:
    service, token = _shifted_token(timedelta(seconds=CLOCK_SKEW_LEEWAY_SECONDS - 10))
    assert service.authenticate_access_token(token).user_id == "user-1"


def test_a_token_from_far_in_the_future_is_refused() -> None:
    service, token = _shifted_token(timedelta(seconds=CLOCK_SKEW_LEEWAY_SECONDS + 120))
    with pytest.raises(TokenError):
        service.authenticate_access_token(token)


def test_the_scheduler_clock_stepping_back_does_not_rerun_a_finished_slot() -> None:
    store, owners, handler, audit = world()
    assert run_all(runner(store, owners, handler, audit)) == ["succeeded"]
    store.advance(seconds=-600)  # NTP steps the clock back ten minutes
    assert run_all(runner(store, owners, handler, audit, "w2")) == []
    assert len(handler.runs) == 1


def test_the_scheduler_clock_jumping_forward_runs_one_catch_up_not_one_per_missed_slot() -> None:
    store, owners, handler, audit = world()
    store.advance(days=2)
    assert run_all(runner(store, owners, handler, audit)) == ["succeeded"]
    assert len(handler.runs) == 1


# --- expired tokens --------------------------------------------------------------------------


def test_an_expired_access_token_is_refused_even_with_the_skew_allowance() -> None:
    service, token = _shifted_token(timedelta(seconds=-(600 + CLOCK_SKEW_LEEWAY_SECONDS + 60)))
    with pytest.raises(TokenError, match="invalid or expired"):
        service.authenticate_access_token(token)


def test_an_expired_refresh_token_is_refused() -> None:
    repository = MemoryAuthRepository()
    service = AuthService(repository, jwt_secret=SECRET)
    login = service.login("demo", "temporary-strong-password", "127.0.0.1")
    for session in repository.sessions.values():
        session["expires_at"] = datetime.now(UTC) - timedelta(seconds=5)
    with pytest.raises(AuthenticationError):
        service.refresh(login.refresh_token)
