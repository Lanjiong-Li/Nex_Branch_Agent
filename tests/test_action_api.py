"""Structured user actions exercise the real API/engine without a model worker."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from branch_agent.app import BASE, create_app
from branch_agent.records import new_record
from branch_agent.workflow import all_records, update
from test_runtime import runtime
from test_api import create_space


@pytest.fixture
def action_api(runtime, tmp_path, monkeypatch):
    engine, model, _, _ = runtime
    monkeypatch.setenv("BRANCH_EMBEDDED_WORKER", "0")
    app = create_app(store=engine.store, engine=engine, data_dir=tmp_path / "auth")
    with TestClient(app) as client:
        password = (tmp_path / "auth" / "local-login.txt").read_text().splitlines()[1].split("：", 1)[1]
        login = client.post(BASE + "/auth/login", json={"username": "developer", "password": password})
        assert login.status_code == 200
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        pid, cid = create_space(client)
        yield client, engine, model, pid, cid


def queue_card(client, engine, pid, cid):
    engine.submit_message(pid, cid, "稍后处理的测试请求")
    queue = all_records(engine.store, pid, "queued_request")[0]
    update(engine.store, queue, state="blocked", blocked_reason="queue_hold")
    endpoint = BASE + f"/projects/{pid}/conversations/{cid}/actions"
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    card = next(c for c in response.json()["cards"] if c["id"] == "queue:" + queue["id"])
    return endpoint, card, queue


def test_actions_are_readable_and_cancel_is_idempotent_without_model(action_api):
    client, engine, model, pid, cid = action_api
    endpoint, card, queue = queue_card(client, engine, pid, cid)
    payload = {"card_id": card["id"], "action_id": "cancel", "expected_revision": card["revision"], "values": {}}
    client.headers["Idempotency-Key"] = str(uuid4())
    first = client.post(endpoint, json=payload)
    assert first.status_code == 200, first.text
    count = len(all_records(engine.store, pid, "history_record"))
    replay = client.post(endpoint, json=payload)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert len(all_records(engine.store, pid, "history_record")) == count
    assert engine.store.get(queue["id"], pid)["state"] == "cancelled"
    assert model.calls == []
    conflict = client.post(endpoint, json={**payload, "action_id": "release"})
    assert conflict.status_code == 409


def test_action_api_rejects_stale_payload_and_malformed_values_without_writes(action_api):
    client, engine, model, pid, cid = action_api
    endpoint, card, queue = queue_card(client, engine, pid, cid)
    payload = {"card_id": card["id"], "action_id": "cancel", "expected_revision": "stale", "values": {}}
    count = len(all_records(engine.store, pid, "history_record"))
    assert client.post(endpoint, json=payload).status_code == 409
    assert len(all_records(engine.store, pid, "history_record")) == count
    assert engine.store.get(queue["id"], pid)["state"] == "blocked"
    for field, value in [("card_id", []), ("action_id", 3), ("expected_revision", None), ("values", [])]:
        invalid = {**payload, "expected_revision": card["revision"], field: value}
        assert client.post(endpoint, json=invalid).status_code == 400
    assert model.calls == []


def test_action_api_checks_csrf_project_and_conversation_ownership(action_api):
    client, engine, model, pid, cid = action_api
    endpoint, card, queue = queue_card(client, engine, pid, cid)
    payload = {"card_id": card["id"], "action_id": "cancel", "expected_revision": card["revision"], "values": {}}
    client.headers.pop("X-CSRF-Token")
    assert client.post(endpoint, json=payload).status_code == 403
    other = engine.store.put(new_record("project", None, title="不可访问", owner_account_id="another-account"))
    other_c = engine.store.put(new_record("conversation", other["id"], title="不可访问"))
    assert client.get(BASE + f'/projects/{other["id"]}/conversations/{other_c["id"]}/actions').status_code == 404
    assert client.get(BASE + f'/projects/{pid}/conversations/{other_c["id"]}/actions').status_code == 404
    assert model.calls == []
