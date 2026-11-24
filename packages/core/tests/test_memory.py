import pytest

from sensei_core.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


def actions(store, entry_id):
    return [c["action"] for c in store.changelog() if c["entry_id"] == entry_id]


def all_store_text(store):
    return "".join(p.read_text(encoding="utf-8") for p in store.root.rglob("*") if p.is_file())


def test_user_general_fact_is_active_and_probeable(store):
    e = store.write("Prefers green tea in the morning")
    assert e["status"] == "active"
    assert e["source"] == "user"
    assert e["sensitivity"] == "LOCAL_ONLY"
    assert [b["id"] for b in store.probe("GREEN TEA")] == [e["id"]]
    assert store.entries_dir.joinpath(f"{e['id']}.json").exists()
    assert actions(store, e["id"]) == ["add"]


def test_tool_content_fact_is_proposed_until_confirmed(store):
    e = store.propose("Meeting with Aiko moved to Friday", origin="email from aiko@example.com")
    assert e["status"] == "proposed"
    assert store.list_beliefs() == []
    assert store.probe("Aiko") == []
    assert [p["id"] for p in store.list_pending()] == [e["id"]]

    confirmed = store.confirm(e["id"])
    assert confirmed["status"] == "active"
    assert [b["id"] for b in store.probe("aiko")] == [e["id"]]
    assert store.list_pending() == []
    assert actions(store, e["id"]) == ["propose", "confirm"]


def test_user_permission_fact_waits_for_confirmation(store):
    e = store.write("Auto-approve calendar invites from Ken", category="permission")
    assert e["status"] == "pending_confirmation"
    assert store.list_beliefs() == []
    assert store.probe("Ken") == []
    assert store.confirm(e["id"])["status"] == "active"
    assert store.probe("Ken")


def test_delete_is_provable(store):
    e = store.write("Bank PIN hint is the cat's birthday")
    store.update(e["id"], "Bank PIN hint is the dog's birthday")
    store.delete(e["id"])

    assert not store.entries_dir.joinpath(f"{e['id']}.json").exists()
    assert store.probe("birthday") == []
    with pytest.raises(KeyError):
        store.get(e["id"])
    # No copy of any version survives anywhere under the store root.
    text = all_store_text(store)
    assert "birthday" not in text and "PIN" not in text
    tombstones = [c for c in store.changelog() if c["entry_id"] == e["id"] and c["action"] == "delete"]
    assert len(tombstones) == 1
    assert tombstones[0]["detail"]["tombstone"] is True


def test_update_then_rollback_restores_previous_version(store):
    e = store.write("Lives in Singapore")
    store.update(e["id"], "Lives in Kuala Lumpur")
    assert store.get(e["id"])["fact"] == "Lives in Kuala Lumpur"
    restored = store.rollback(e["id"])
    assert restored["fact"] == "Lives in Singapore"
    assert store.get(e["id"])["fact"] == "Lives in Singapore"
    assert actions(store, e["id"]) == ["add", "update", "rollback"]
    with pytest.raises(ValueError, match="no previous version"):
        store.rollback(e["id"])


def test_rollback_walks_back_through_multiple_versions(store):
    e = store.write("v1")
    store.update(e["id"], "v2")
    store.update(e["id"], "v3")
    assert store.rollback(e["id"])["fact"] == "v2"
    assert store.rollback(e["id"])["fact"] == "v1"


def test_no_path_creates_active_tool_content_without_confirm(store):
    statuses = []
    for category in ("general", "permission", "money", "trusted_contact"):
        e = store.propose("Vendor X is trusted", origin="web page", category=category)
        statuses.append(e["status"])
        # update and rollback must not launder a proposal into a belief
        statuses.append(store.update(e["id"], "Vendor X is very trusted")["status"])
        statuses.append(store.rollback(e["id"])["status"])
        statuses.append(store.get(e["id"])["status"])

    # A confirmed tool fact that changes must be confirmed again.
    c = store.propose("Office is on level 3", origin="calendar invite")
    store.confirm(c["id"])
    statuses.append(store.update(c["id"], "Office is on level 4")["status"])

    # write() has no source parameter: it cannot be used to claim tool content is the user's.
    with pytest.raises(TypeError):
        store.write("Vendor X is trusted", source="tool_content")

    assert "active" not in statuses
    tool_entries = [e for e in store.list_beliefs() if e["source"] == "tool_content"]
    assert [e["id"] for e in tool_entries] == []


def test_invalid_inputs_are_rejected(store):
    with pytest.raises(ValueError):
        store.write("   ")
    with pytest.raises(ValueError):
        store.write("x", category="secrets")
    with pytest.raises(ValueError):
        store.propose("x", origin="")
    with pytest.raises(KeyError):
        store.get("../../etc/passwd")
