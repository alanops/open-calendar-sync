# Architecture

The application has four boundaries:

- `providers.py`: OAuth and calendar API details. Returns normalized event identities, times, availability and private ownership markers. Builds only private Busy payloads.
- `models.py`: a pure all-to-all planner. Excludes managed copies and free events, and deduplicates equivalent invitations by UID plus occurrence times.
- `engine.py`: serializes preview, sync and cleanup; collects complete snapshots; reconciles durable copy intent against providers; records counts and safe errors.
- `store.py`: SQLite persistence, encrypted tokens, hashed sessions, expiring single-use OAuth state, copy mappings and bounded run history.

`app.py` exposes the single-owner dashboard API. `static/` contains dependency-free HTML, CSS and JavaScript. OAuth registrations are supplied by the operator, not embedded in the public repository.

## Source of truth

The original calendar event remains authoritative. Google `singleEvents=true` and Microsoft `calendarView` expand recurrence into instances in the active window. Each source account / source event / destination tuple has a stable key. A source change updates its managed copies; absence in a complete snapshot removes obsolete managed copies after all desired writes succeed.

Both providers are read with pagination. Unknown pagination origins, missing response collections, repeated page tokens and page-count exhaustion are failures, never empty calendars. The read phase completes before any write. A failed read of even one selected account stops the cycle.

## Idempotency and recovery

Before creating a copy, SQLite persists an intent and a random transaction identifier. Google uses that identifier as the event ID; Microsoft uses it as `transactionId`. Both receive an installation-specific extended-property marker containing the stable copy key.

When the result of a POST is uncertain, the next run searches by the marker and adopts the existing copy. It does not rely solely on Microsoft's transaction ID retention. When a tracked copy was manually deleted, a new intent ID is allocated. The engine validates a tracked copy's marker and refuses to alter copies with attendees; conditional updates/deletes use the provider's ETag when supplied.

Copies are skipped as sources even if their marker belongs to another installation. Other sync tools' unmarked copies cannot be distinguished reliably and should not be synced concurrently.

## Time and recurrence

Timed values are compared as instants, so equivalent UTC offsets do not cause repeated updates. All-day values retain calendar dates and exclusive end dates. Microsoft all-day sources are reread in their original timezone before extracting dates because Graph otherwise returns UTC. Destination Microsoft all-day copies use midnight UTC and `isAllDay=true`; verify their presentation in your destination clients during live acceptance testing.

Invitation deduplication is best-effort when providers expose matching iCalendar UIDs; some cross-provider transformations use different identifiers. Distinct recurring instances remain separate through their event IDs and occurrence times.

## Operating limits

The first version trades incremental delta feeds and webhooks for complete bounded snapshots. Existing copies inside target snapshots are reused to avoid one lookup per copy; missing/out-of-window copies and interrupted creates require direct lookups. Very large calendars may need a narrower horizon or a longer interval to remain within provider quotas.

All mutations run in one async engine lock, backed by a process-level OS file lock. This is a single-process design, not a distributed scheduler. Account disconnect pauses the engine, cleans all managed copies while credentials are still present, and only then forgets the account. A later resume rebuilds copies among the remaining selected accounts.

## Provider references

- [Google OAuth web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Google events.list](https://developers.google.com/workspace/calendar/api/v3/reference/events/list)
- [Google event IDs and event fields](https://developers.google.com/workspace/calendar/api/v3/reference/events)
- [Google extended properties](https://developers.google.com/workspace/calendar/api/guides/extended-properties)
- [Microsoft calendarView](https://learn.microsoft.com/en-us/graph/api/calendar-list-calendarview?view=graph-rest-1.0)
- [Microsoft event resource and transactionId](https://learn.microsoft.com/en-us/graph/api/resources/event?view=graph-rest-1.0)
- [Microsoft named extended properties](https://learn.microsoft.com/en-us/graph/api/resources/singlevaluelegacyextendedproperty?view=graph-rest-1.0)
