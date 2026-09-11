from dataclasses import replace

import pytest

from calendar_sync.engine import Engine
from calendar_sync.models import Event, plan
from calendar_sync.providers import ProviderError

MEETING = Event("meeting", "2026-10-24T10:00:00+01:00", "2026-10-24T11:00:00+01:00", uid="invite")


class FakeCalendar:
    def __init__(self):
        self.events = {}
        self.fail_read = False
        self.fail_write = False
        self.lose_response = False
        self.writes = []

    async def snapshot(self, start, end):
        if self.fail_read:
            raise ProviderError("Snapshot failed")
        return list(self.events.values())

    async def get(self, remote_id):
        return self.events.get(remote_id)

    async def find(self, marker):
        return next((e for e in self.events.values() if e.marker == marker), None)

    async def create(self, event, marker, transaction_id):
        if self.fail_write:
            raise ProviderError("Write failed")
        self.events[transaction_id] = replace(event, id=transaction_id, marker=marker)
        self.writes.append("create")
        if self.lose_response:
            self.lose_response = False
            raise ProviderError("Response lost")
        return transaction_id

    async def update(self, event_id, event, marker, etag=""):
        self.events[event_id] = replace(event, id=event_id, marker=marker)
        self.writes.append("update")

    async def delete(self, event_id, etag=""):
        self.events.pop(event_id, None)
        self.writes.append("delete")


@pytest.fixture
def setup(settings, store, token):
    ids = [store.add_account("google", name, f"{name}@example.com", dict(token)) for name in "abcd"]
    clients = {account: FakeCalendar() for account in ids}
    for account in ids:
        store.enable(account, True)
    store.set("paused", "false")
    engine = Engine(settings, store, lambda settings, store, account: clients[account["id"]])
    return engine, ids, clients


def test_all_to_all_skips_free_and_clones():
    snapshots = {
        "a": [
            MEETING,
            replace(MEETING, id="free", busy=False),
            replace(MEETING, id="clone", marker="ocs:other"),
        ],
        "b": [],
        "c": [],
        "d": [],
    }
    result = list(plan(snapshots).values())
    assert len(result) == 3
    assert {c.target for c in result} == {"b", "c", "d"}
    assert all(c.event.id == "meeting" for c in result)


def test_shared_invitation_not_duplicated_and_timezones_equivalent():
    same = replace(MEETING, id="other-id", start="2026-10-24T09:00:00Z", end="2026-10-24T10:00:00Z")
    result = list(plan({"a": [MEETING], "b": [same], "c": []}).values())
    assert len(result) == 1 and result[0].target == "c"


def test_recurring_instances_keep_distinct_dates():
    tomorrow = replace(MEETING, id="next-instance", start="2026-10-25T10:00:00Z", end="2026-10-25T11:00:00Z")
    assert len(plan({"a": [MEETING, tomorrow], "b": []})) == 2


async def test_repeat_cycle_does_not_create_loops(setup, store):
    engine, ids, clients = setup
    clients[ids[0]].events[MEETING.id] = MEETING
    assert (await engine.run())["status"] == "success"
    assert len(store.copies()) == 3
    assert (await engine.run())["message"] == "0 created · 0 updated · 0 removed"
    assert sum(len(c.events) for c in clients.values()) == 4
    assert clients[ids[0]].events[MEETING.id] == MEETING


async def test_update_and_cancel_propagate_only_to_copies(setup, store):
    engine, ids, clients = setup
    source = clients[ids[0]]
    source.events[MEETING.id] = MEETING
    await engine.run()
    moved = replace(MEETING, start="2026-10-24T13:00:00Z", end="2026-10-24T14:00:00Z")
    source.events[MEETING.id] = moved
    assert "3 updated" in (await engine.run())["message"]
    assert all(e.start == moved.start for c in clients.values() for e in c.events.values())
    source.events.clear()
    assert "3 removed" in (await engine.run())["message"]
    assert not store.copies()
    assert not source.writes


async def test_partial_snapshot_never_writes_or_cleans(setup, store):
    engine, ids, clients = setup
    clients[ids[0]].events[MEETING.id] = MEETING
    await engine.run()
    clients[ids[0]].events.clear()
    clients[ids[1]].fail_read = True
    before = [list(c.writes) for c in clients.values()]
    assert (await engine.run())["status"] == "error"
    assert len(store.copies()) == 3
    assert [c.writes for c in clients.values()] == before


async def test_lost_create_response_recovers_without_duplicate(setup, store):
    engine, ids, clients = setup
    clients[ids[0]].events[MEETING.id] = MEETING
    clients[sorted(ids[1:])[0]].lose_response = True
    assert (await engine.run())["status"] == "error"
    assert (await engine.run())["status"] == "success"
    assert sum(len(c.events) for c in clients.values()) == 4
    assert len(store.copies()) == 3


async def test_lost_marker_prevents_edit_or_delete(setup):
    engine, ids, clients = setup
    clients[ids[0]].events[MEETING.id] = MEETING
    await engine.run()
    target = clients[ids[1]]
    copy_id = next(iter(target.events))
    target.events[copy_id] = replace(target.events[copy_id], marker="")
    clients[ids[0]].events.clear()
    result = await engine.run()
    assert result["status"] == "error" and "ownership" in result["message"]
    assert copy_id in target.events
    assert "delete" not in target.writes


async def test_copies_with_guests_are_not_modified(setup):
    engine, ids, clients = setup
    clients[ids[0]].events[MEETING.id] = MEETING
    await engine.run()
    target = clients[ids[1]]
    copy_id = next(iter(target.events))
    target.events[copy_id] = replace(target.events[copy_id], has_guests=True)
    clients[ids[0]].events.clear()
    assert "guests" in (await engine.run())["message"]
    assert copy_id in target.events


async def test_manual_copy_deletion_recreates_new_id(setup):
    engine, ids, clients = setup
    clients[ids[0]].events[MEETING.id] = MEETING
    await engine.run()
    target = clients[ids[1]]
    old = next(iter(target.events))
    target.events.clear()
    await engine.run()
    assert len(target.events) == 1 and old not in target.events


async def test_preview_and_pause_never_write(setup, store):
    engine, ids, clients = setup
    clients[ids[0]].events[MEETING.id] = MEETING
    assert (await engine.preview())["busy_copies"] == 3
    store.set("paused", "true")
    assert (await engine.run())["status"] == "paused"
    assert all(not c.writes for c in clients.values())


async def test_disconnect_cleans_owned_copies_before_forgetting_token(setup, store):
    engine, ids, clients = setup
    clients[ids[0]].events[MEETING.id] = MEETING
    await engine.run()
    await engine.disconnect(ids[0])
    assert not store.copies()
    assert len(store.accounts()) == 3
    assert store.get("paused") == "true"
    assert clients[ids[0]].events[MEETING.id] == MEETING


async def test_failed_write_does_not_remove_obsolete_copies(setup, store):
    engine, ids, clients = setup
    source = clients[ids[0]]
    source.events[MEETING.id] = MEETING
    await engine.run()
    old_ids = {e.id for c in clients.values() for e in c.events.values() if e.marker}
    source.events = {"new": replace(MEETING, id="new", uid="new")}
    for account in ids[1:]:
        clients[account].fail_write = True
    assert (await engine.run())["status"] == "error"
    assert old_ids.issubset({e.id for c in clients.values() for e in c.events.values()})
