"""Knowledge routes: success, 401, 403 and cross-project isolation, with audit on every change."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.metadata.knowledge import InMemoryKnowledgeRepository
from app.api import dependencies
from app.api.knowledge_routes import router
from app.auth.models import AuthContext
from app.auth.service import AuthenticationError
from app.knowledge.service import KnowledgeService
from tests.support.ecommerce_mdl import build_mdl
from tests.support.knowledge import MemoryVectorStore, WordEmbedder

PROJECT = "00000000-0000-0000-0000-0000000000c1"
OTHER = "00000000-0000-0000-0000-0000000000c2"
ORDERS_SQL = "SELECT order_id FROM orders"
EVERYTHING = {"manage_knowledge", "approve_pairs", "give_feedback"}


class Repo:
    def get_active_mdl_version(self, project_id: str) -> int | None:
        return 1

    def get_mdl(self, project_id: str, version: int | None = None) -> dict[str, object]:
        return {**build_mdl().model_dump(by_alias=True, mode="json"), "project_id": project_id, "revision": 1}


class Rig:
    def __init__(self, capabilities: set[str], projects: tuple[str, ...] = (PROJECT,)) -> None:
        self.repo = InMemoryKnowledgeRepository()
        self.service = KnowledgeService(self.repo, WordEmbedder(), MemoryVectorStore())
        context = AuthContext(
            user_id="00000000-0000-0000-0000-0000000000d1", username="u", role_name="X",
            capabilities=frozenset(capabilities), project_ids=frozenset(projects),
            must_change_password=False,
        )
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[dependencies.get_current_context] = lambda: context
        app.dependency_overrides[dependencies.get_repository] = lambda: Repo()
        app.dependency_overrides[dependencies.get_knowledge_service] = lambda: self.service
        self.http = TestClient(app, raise_server_exceptions=False)

    def url(self, path: str, project: str = PROJECT) -> str:
        return f"/api/v1/projects/{project}/{path}"

    def actions(self) -> list[str]:
        return [e.action for e in self.repo.audit]


def feedback_body(rating: str = "up", **extra: Any) -> dict[str, Any]:
    return {"message_id": "m1", "rating": rating, "question": "orders by status",
            "logical_sql": ORDERS_SQL, **extra}


# ----------------------------------------------------------------- instructions


def test_instruction_crud_round_trip_with_audit() -> None:
    rig = Rig(EVERYTHING)
    created = rig.http.post(rig.url("knowledge/instructions"), json={"text": "fiscal year starts April"})
    assert created.status_code == 201 and created.json()["active"] is True
    iid = created.json()["id"]
    listed = rig.http.get(rig.url("knowledge/instructions"))
    assert [i["text"] for i in listed.json()] == ["fiscal year starts April"]
    patched = rig.http.patch(rig.url(f"knowledge/instructions/{iid}"), json={"active": False})
    assert patched.status_code == 200 and patched.json()["active"] is False
    assert rig.http.delete(rig.url(f"knowledge/instructions/{iid}")).status_code == 204
    assert rig.http.delete(rig.url(f"knowledge/instructions/{iid}")).status_code == 404
    assert rig.actions() == ["instruction_created", "instruction_updated", "instruction_deleted"]


def test_instruction_bodies_are_validated() -> None:
    rig = Rig(EVERYTHING)
    assert rig.http.post(rig.url("knowledge/instructions"), json={"text": "  "}).status_code == 422
    assert rig.http.post(rig.url("knowledge/instructions"), json={"text": "x", "z": 1}).status_code == 422
    iid = rig.http.post(rig.url("knowledge/instructions"), json={"text": "x"}).json()["id"]
    assert rig.http.patch(rig.url(f"knowledge/instructions/{iid}"), json={}).status_code == 422


def test_an_instruction_of_another_project_is_not_reachable_through_this_one() -> None:
    rig = Rig(EVERYTHING, (PROJECT, OTHER))
    foreign = rig.http.post(rig.url("knowledge/instructions", OTHER), json={"text": "theirs"}).json()["id"]
    assert rig.http.patch(rig.url(f"knowledge/instructions/{foreign}"), json={"text": "mine"}).status_code == 404
    assert rig.http.delete(rig.url(f"knowledge/instructions/{foreign}")).status_code == 404
    assert rig.http.get(rig.url("knowledge/instructions")).json() == []
    assert rig.repo.instructions[foreign].text == "theirs"


# -------------------------------------------------------------------- sql pairs


def test_sql_pair_create_list_delete() -> None:
    rig = Rig(EVERYTHING)
    created = rig.http.post(
        rig.url("knowledge/sql-pairs"), json={"question": "orders by status", "logical_sql": ORDERS_SQL}
    )
    assert created.status_code == 201 and created.json()["status"] == "approved"
    assert [p["id"] for p in rig.http.get(rig.url("knowledge/sql-pairs")).json()] == [created.json()["id"]]
    assert rig.http.delete(rig.url(f"knowledge/sql-pairs/{created.json()['id']}")).status_code == 204
    assert rig.http.delete(rig.url(f"knowledge/sql-pairs/{uuid4()}")).status_code == 404
    assert rig.actions() == ["sql_pair_created", "sql_pair_deleted"]


def test_a_pair_with_physical_names_is_a_422_and_a_duplicate_a_409() -> None:
    rig = Rig(EVERYTHING)
    bad = rig.http.post(
        rig.url("knowledge/sql-pairs"),
        json={"question": "q", "logical_sql": "SELECT * FROM ecommerce.orders"},
    )
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "INVALID_SQL"
    body = {"question": "q", "logical_sql": ORDERS_SQL}
    assert rig.http.post(rig.url("knowledge/sql-pairs"), json=body).status_code == 201
    assert rig.http.post(rig.url("knowledge/sql-pairs"), json=body).status_code == 409


def test_a_pair_of_another_project_cannot_be_deleted_here() -> None:
    rig = Rig(EVERYTHING, (PROJECT, OTHER))
    foreign = rig.http.post(
        rig.url("knowledge/sql-pairs", OTHER), json={"question": "q", "logical_sql": ORDERS_SQL}
    ).json()["id"]
    assert rig.http.delete(rig.url(f"knowledge/sql-pairs/{foreign}")).status_code == 404
    assert rig.http.get(rig.url("knowledge/sql-pairs")).json() == []
    assert foreign in rig.repo.pairs


# ---------------------------------------------------------- feedback and proposals


def test_feedback_proposal_review_flow() -> None:
    rig = Rig(EVERYTHING)
    sent = rig.http.post(rig.url("feedback"), json=feedback_body())
    assert sent.status_code == 201 and sent.json()["proposal"]["status"] == "proposed"
    pid = sent.json()["proposal"]["id"]
    assert rig.http.get(rig.url("knowledge/sql-pairs")).json() == []  # not approved yet
    queue = rig.http.get(rig.url("knowledge/proposals")).json()
    assert [p["id"] for p in queue] == [pid] and queue[0]["nearest_approved"] is None
    approved = rig.http.post(rig.url(f"knowledge/proposals/{pid}/approve"))
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert [p["id"] for p in rig.http.get(rig.url("knowledge/sql-pairs")).json()] == [pid]
    assert rig.http.post(rig.url(f"knowledge/proposals/{pid}/approve")).status_code == 409
    assert rig.http.post(rig.url(f"knowledge/proposals/{pid}/reject"), json={}).status_code == 409
    assert rig.actions() == ["feedback_given", "sql_pair_proposed", "sql_pair_approved"]


def test_reject_and_nearest_approved_pair_in_the_queue() -> None:
    rig = Rig(EVERYTHING)
    rig.http.post(rig.url("knowledge/sql-pairs"), json={"question": "orders by status", "logical_sql": ORDERS_SQL})
    pid = rig.http.post(
        rig.url("feedback"), json=feedback_body(question="orders by status please")
    ).json()["proposal"]["id"]
    [item] = rig.http.get(rig.url("knowledge/proposals")).json()
    assert item["nearest_approved"]["question"] == "orders by status"
    rejected = rig.http.post(rig.url(f"knowledge/proposals/{pid}/reject"), json={"reason": "dup"})
    assert rejected.json()["status"] == "rejected" and rig.actions()[-1] == "sql_pair_rejected"
    assert rig.http.get(rig.url("knowledge/proposals")).json() == []


def test_thumbs_down_and_bad_feedback_bodies() -> None:
    rig = Rig(EVERYTHING)
    down = rig.http.post(rig.url("feedback"), json=feedback_body("down"))
    assert down.status_code == 201 and down.json()["proposal"] is None
    assert rig.http.post(rig.url("feedback"), json={**feedback_body(), "message_id": None}).status_code == 422
    assert rig.http.post(rig.url("feedback"), json=feedback_body("meh")).status_code == 422
    thread = rig.http.post(
        rig.url("feedback"),
        json={**feedback_body(), "message_id": None, "thread_id": str(uuid4())},
    )
    assert thread.status_code == 201
    hostile = rig.http.post(
        rig.url("feedback"), json={**feedback_body(), "logical_sql": "SELECT * FROM ecommerce.orders"}
    )
    assert hostile.status_code == 422


def test_a_proposal_of_another_project_cannot_be_decided_here() -> None:
    rig = Rig(EVERYTHING, (PROJECT, OTHER))
    foreign = rig.http.post(rig.url("feedback", OTHER), json=feedback_body()).json()["proposal"]["id"]
    assert rig.http.post(rig.url(f"knowledge/proposals/{foreign}/approve")).status_code == 404
    assert rig.http.post(rig.url(f"knowledge/proposals/{foreign}/reject"), json={}).status_code == 404
    assert rig.http.get(rig.url("knowledge/proposals")).json() == []
    assert rig.repo.pairs[foreign].status == "proposed"


# ------------------------------------------------------------- access control

CALLS: list[tuple[str, str, str, dict[str, Any] | None]] = [
    ("GET", "knowledge/instructions", "manage_knowledge", None),
    ("POST", "knowledge/instructions", "manage_knowledge", {"text": "x"}),
    ("PATCH", f"knowledge/instructions/{uuid4()}", "manage_knowledge", {"active": True}),
    ("DELETE", f"knowledge/instructions/{uuid4()}", "manage_knowledge", None),
    ("GET", "knowledge/sql-pairs", "manage_knowledge", None),
    ("POST", "knowledge/sql-pairs", "approve_pairs", {"question": "q", "logical_sql": ORDERS_SQL}),
    ("DELETE", f"knowledge/sql-pairs/{uuid4()}", "manage_knowledge", None),
    ("POST", "feedback", "give_feedback", feedback_body()),
    ("GET", "knowledge/proposals", "approve_pairs", None),
    ("POST", f"knowledge/proposals/{uuid4()}/approve", "approve_pairs", None),
    ("POST", f"knowledge/proposals/{uuid4()}/reject", "approve_pairs", {}),
]


def test_each_route_needs_its_own_capability() -> None:
    for method, path, needed, body in CALLS:
        others = EVERYTHING - {needed}
        denied = Rig(others).http.request(method, Rig(set()).url(path), json=body)
        assert denied.status_code == 403, (method, path)
        allowed = Rig({needed}).http.request(method, Rig(set()).url(path), json=body)
        assert allowed.status_code not in {401, 403}, (method, path)


def test_a_project_the_account_is_not_in_is_a_404_on_every_route() -> None:
    for method, path, _, body in CALLS:
        rig = Rig(EVERYTHING, (OTHER,))
        assert rig.http.request(method, rig.url(path), json=body).status_code == 404, (method, path)


def test_no_bearer_token_is_a_401_on_every_route() -> None:
    class Rejecting:
        def authenticate_access_token(self, *_: Any, **__: Any) -> AuthContext:
            raise AuthenticationError("bad token")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_auth_service] = lambda: Rejecting()
    http = TestClient(app, raise_server_exceptions=False)
    for method, path, _, body in CALLS:
        url = f"/api/v1/projects/{PROJECT}/{path}"
        assert http.request(method, url, json=body).status_code == 401, (method, path)
        bad = http.request(method, url, json=body, headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401, (method, path)


def test_an_embedding_outage_is_a_503_not_a_500() -> None:
    rig = Rig(EVERYTHING)
    rig.service._embedder.fail = True  # type: ignore[attr-defined]
    response = rig.http.post(rig.url("knowledge/instructions"), json={"text": "x"})
    assert response.status_code == 503 and rig.repo.instructions == {}
