import json

import httpx
import pytest

from calendar_sync.models import Event
from calendar_sync.providers import MARKER_NAME, PROPERTY, Calendar, ProviderError


def client(provider, settings, store, token, handler):
    account_id = store.add_account(provider, "subject", "owner@example.com", token)
    account = next(a for a in store.accounts() if a["id"] == account_id)
    return Calendar(settings, store, account, httpx.MockTransport(handler))


def google_event(**kwargs):
    return {
        "id": "event1",
        "summary": "Private appointment",
        "iCalUID": "invitation",
        "start": {"dateTime": "2026-10-24T10:00:00+01:00"},
        "end": {"dateTime": "2026-10-24T11:00:00+01:00"},
        **kwargs,
    }


async def test_google_paginated_recurring_snapshot(settings, store, token):
    calls = []

    def handler(request):
        calls.append(request)
        if "pageToken" not in request.url.params:
            return httpx.Response(200, json={"items": [google_event()], "nextPageToken": "page2"})
        return httpx.Response(200, json={"items": [google_event(id="cancelled", status="cancelled")]})

    cal = client("google", settings, store, token, handler)
    events = await cal.snapshot("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    assert len(events) == 1
    assert calls[0].url.params["singleEvents"] == "true"
    assert calls[1].url.params["pageToken"] == "page2"


async def test_incomplete_or_failed_page_is_not_empty_calendar(settings, store, token):
    def handler(request):
        return httpx.Response(200, json={"unexpected": []})

    cal = client("google", settings, store, token, handler)
    with pytest.raises(ProviderError, match="incomplete"):
        await cal.snapshot("start", "end")


async def test_microsoft_external_pagination_never_receives_token(settings, store, token):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://evil.example/collect"})

    cal = client("microsoft", settings, store, token, handler)
    with pytest.raises(ProviderError, match="pagination"):
        await cal.snapshot("start", "end")
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["google", "microsoft"])
async def test_created_copy_contains_only_busy_time_and_no_invitations(provider, settings, store, token):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "new-id"})

    cal = client(provider, settings, store, token, handler)
    event = Event("source-id", "2026-10-24T10:00:00+01:00", "2026-10-24T11:00:00+01:00")
    await cal.create(event, "test-marker", "a" * 32)
    body = json.loads(calls[0].content)
    assert body["attendees"] == []
    if provider == "google":
        assert body["summary"] == "Busy" and body["visibility"] == "private"
        assert body["extendedProperties"]["private"][MARKER_NAME] == "test-marker"
        assert calls[0].url.params["sendUpdates"] == "none"
        assert body["id"] == "a" * 32
    else:
        assert calls[0].headers["Prefer"] == 'outlook.timezone="UTC", outlook.body-content-type="text"'
        assert body["subject"] == "Busy" and body["sensitivity"] == "private"
        assert body["start"] == {"dateTime": "2026-10-24T09:00:00", "timeZone": "UTC"}
        assert body["transactionId"] == "a" * 32
        assert body["singleValueExtendedProperties"] == [{"id": PROPERTY, "value": "test-marker"}]


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_all_day_end_is_exclusive(provider, settings, store, token):
    cal = client(provider, settings, store, token, lambda r: httpx.Response(200))
    body = cal.body(Event("all-day", "2026-10-24", "2026-10-26", all_day=True), "marker")
    assert "2026-10-26" in str(body["end"])
    assert "2026-10-24" in str(body["start"])


async def test_google_declined_and_free_events_do_not_block(settings, store, token):
    cal = client("google", settings, store, token, lambda r: httpx.Response(200))
    declined = await cal.normalize(google_event(attendees=[{"self": True, "responseStatus": "declined"}]))
    free = await cal.normalize(google_event(transparency="transparent"))
    assert not declined.busy and not free.busy


async def test_ms_all_day_uses_original_zone_calendar_dates(settings, store, token):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"start": {"dateTime": "2026-07-10T00:00:00"}, "end": {"dateTime": "2026-07-11T00:00:00"}},
        )

    cal = client("microsoft", settings, store, token, handler)
    event = await cal.normalize(
        {
            "id": "all-day",
            "isAllDay": True,
            "originalStartTimeZone": "GMT Standard Time",
            "start": {"dateTime": "2026-07-09T23:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-07-10T23:00:00", "timeZone": "UTC"},
        }
    )
    assert event.start == "2026-07-10" and event.end == "2026-07-11"
    assert calls[0].headers["Prefer"] == 'outlook.timezone="GMT Standard Time"'


async def test_conditional_delete_protects_concurrent_edit(settings, store, token):
    def handler(request):
        assert request.headers["If-Match"] == '"version-1"'
        return httpx.Response(412, json={"error": "sensitive content not shown"})

    cal = client("google", settings, store, token, handler)
    with pytest.raises(ProviderError, match="HTTP 412") as failure:
        await cal.delete("managed", '"version-1"')
    assert "sensitive" not in str(failure.value)


async def test_rate_limit_stops_without_ignoring_retry_after(settings, store, token):
    cal = client(
        "google", settings, store, token, lambda r: httpx.Response(429, headers={"Retry-After": "120"})
    )
    with pytest.raises(ProviderError, match="longer pause"):
        await cal.snapshot("start", "end")


async def test_duplicate_marker_stops_instead_of_guessing(settings, store, token):
    cal = client(
        "google",
        settings,
        store,
        token,
        lambda r: httpx.Response(200, json={"items": [google_event(), google_event(id="two")]}),
    )
    with pytest.raises(ProviderError, match="Duplicate"):
        await cal.find("test-marker")
