"""The API must reject deletion even when no frontend validation ran."""

import pytest
from pymongo.errors import OperationFailure

from candlelab.domain.errors import AppError
from candlelab.schemas import ErrorResponse
from test_combinations import custom
from test_strategy_configurations import configuration

PATH = "/api/v1/strategy-combinations"


def save(client, definition):
    response = client.post(PATH, json=definition)
    assert response.status_code == 201, response.text
    return response.json()


def snapshot(saved):
    return {key: value for key, value in saved.items() if key != "id"}


def group(name, child):
    return custom(
        name=name,
        members=[{"combination": child}, {"strategy_id": "EMA_CROSS@1"}],
    )


def run_record(store, saved, run_id="linked-run", status="queued"):
    doc = {
        "id": run_id,
        "session_id": "another-session",
        "strategy_id": saved["strategy_id"] or "CUSTOM_COMBINATION@1",
        "combination_snapshot": saved,
        "status": status,
    }
    store.db.runs.insert_one(doc)
    return doc


def blocked(client, saved):
    response = client.delete(PATH + "/" + saved["id"])
    assert response.status_code == 409, response.text
    body = response.json()
    ErrorResponse.model_validate(body)
    assert body["error"]["code"] == "CONFIGURATION_IN_USE"
    assert saved["id"] in {item["id"] for item in client.get(PATH).json()["items"]}
    return body["error"]["details"][0]


@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed"])
def test_direct_run_blocks_in_every_state_after_source_edits(client, store, status):
    saved = save(client, configuration())
    run_record(store, saved, status=status)
    response = client.put(
        PATH + "/" + saved["id"],
        json=configuration(name="SMA editada", parameters={"short_window": 3, "long_window": 8}),
    )
    assert response.status_code == 200, response.text
    detail = blocked(client, response.json())
    assert detail["runs"] == ["linked-run"]
    assert detail["combinations"] == []


def test_nested_group_blocks_and_removing_group_allows_deletion(client):
    saved = save(client, configuration())
    parent = save(client, group("Grupo externo", group("Grupo interno", snapshot(saved))))
    detail = blocked(client, saved)
    assert detail["combinations"] == [parent["name"]]
    assert detail["runs"] == []
    assert client.delete(PATH + "/" + parent["id"]).status_code == 204
    assert client.delete(PATH + "/" + saved["id"]).status_code == 204


def test_historical_nested_copy_blocks_after_parent_was_deleted(client, store):
    saved = save(client, configuration())
    parent = save(client, group("Grupo histórico", group("Grupo interno", snapshot(saved))))
    run_record(store, parent)
    assert client.delete(PATH + "/" + parent["id"]).status_code == 204
    detail = blocked(client, saved)
    assert detail["runs"] == ["linked-run"]
    assert detail["combinations"] == []
    assert store.db.runs.find_one({"id": "linked-run"})["combination_snapshot"] == parent


def test_other_configuration_using_same_base_does_not_block_direct_run(client, store):
    saved = save(client, configuration())
    other = save(
        client,
        configuration(name="Outra SMA", parameters={"short_window": 3, "long_window": 8}),
    )
    run_record(store, other)
    assert client.delete(PATH + "/" + saved["id"]).status_code == 204
    assert store.combination(other["id"]) == other
    assert store.db.runs.find_one({"id": "linked-run"})["combination_snapshot"] == other


def test_unlinked_configuration_and_missing_id(client):
    saved = save(client, configuration())
    assert client.delete(PATH + "/" + saved["id"]).status_code == 204
    assert client.delete(PATH + "/" + saved["id"]).status_code == 404
    assert client.delete(PATH + "/missing").status_code == 404


@pytest.mark.parametrize("historical", [False, True])
def test_ambiguous_copy_is_reported_instead_of_assuming_it_is_unrelated(client, store, historical):
    saved = save(client, configuration())
    copy = configuration(name="Nome antigo", parameters={"short_window": 3, "long_window": 8})
    parent = save(client, group("Grupo com cópia sem ID", copy))
    if historical:
        run_record(store, parent)
        assert client.delete(PATH + "/" + parent["id"]).status_code == 204
    detail = blocked(client, saved)
    assert detail["uncertain_copies"] == [
        "Processamento: linked-run" if historical else f"Combinação: {parent['name']}"
    ]
    assert detail["runs"] == []
    assert detail["combinations"] == []


def test_usage_scan_is_not_limited_to_a_page_of_records(client, store):
    saved = save(client, configuration())
    other = {**saved, "id": "unrelated-source"}
    for index in range(150):
        run_record(store, other, run_id=f"unrelated-{index}")
    run_record(store, saved, run_id="last-linked-run")
    assert blocked(client, saved)["runs"] == ["last-linked-run"]


def test_data_access_failure_does_not_remove_configuration(client, store, monkeypatch):
    saved = save(client, configuration())

    def unavailable(*args, **kwargs):
        raise OperationFailure("temporarily unavailable")

    monkeypatch.setattr(store.db.runs, "find", unavailable)
    response = client.delete(PATH + "/" + saved["id"])
    assert response.status_code == 503, response.text
    assert store.combination(saved["id"]) == saved


def test_store_cannot_bypass_the_deletion_restriction(client, store):
    saved = save(client, configuration())
    run_record(store, saved)
    with pytest.raises(AppError) as exc:
        store.delete_combination(saved["id"])
    assert exc.value.status == 409
    assert exc.value.code == "CONFIGURATION_IN_USE"
    assert store.combination(saved["id"]) == saved
