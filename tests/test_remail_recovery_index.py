from unittest.mock import patch

import pytest

from core.remail_manager import RemailManager, _default_state


@pytest.mark.parametrize("history_count", [0, 500, 2000])
def test_recovery_binds_active_job_without_rescanning_history(tmp_path, history_count):
    manager = RemailManager(state_path=tmp_path / "state.json")
    state = _default_state()
    slots = [{
        "reservation_id": f"OR{index}:main",
        "status": "succeeded",
        "job_id": index + 1,
    } for index in range(history_count)]
    slots.append({"reservation_id": "TARGET:main", "status": "available"})
    state["orders"] = [{"slots": slots}]
    manager._save(state)
    jobs = [{
        "id": index + 1,
        "email_source": "remail",
        "status": "success",
        "mailbox_reservation_id": f"OR{index}:main",
    } for index in range(history_count)]
    jobs.append({
        "id": history_count + 1,
        "email_source": "remail",
        "status": "pending",
        "mailbox_reservation_id": "TARGET:main",
        "created_at": "2026-11-09T01:45:00",
    })

    with (
        patch("core.db.list_jobs", return_value=jobs),
        patch.object(manager, "_find_slot", side_effect=AssertionError("full slot rescan")),
        patch.object(manager, "_iter_slots", wraps=manager._iter_slots) as scans,
    ):
        manager.recover()

    assert manager._recovered
    # One pass for recovery, one for the pickup cache, regardless of job count.
    assert scans.call_count == 2
    recovered = manager._load()["orders"][0]["slots"]
    assert recovered[:-1] == slots[:-1]
    assert recovered[-1]["status"] == "reserved"
    assert recovered[-1]["job_id"] == history_count + 1
    assert recovered[-1]["reserved_at"] == "2026-11-09T01:45:00"


def test_recovery_preserves_first_match_for_duplicate_reservations(tmp_path):
    manager = RemailManager(state_path=tmp_path / "state.json")
    state = _default_state()
    state["orders"] = [{"slots": [
        {"reservation_id": "DUP:main", "status": "available"},
        {"reservation_id": "DUP:main", "status": "available"},
    ]}]
    manager._save(state)
    with patch("core.db.list_jobs", return_value=[{
        "id": 1,
        "email_source": "remail",
        "status": "pending",
        "mailbox_reservation_id": "DUP:main",
    }]):
        manager.recover()
    first, second = manager._load()["orders"][0]["slots"]
    assert first["status"] == "reserved"
    assert second["status"] == "available"
