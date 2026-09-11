import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client

from .models import Event

PROPERTY = "String {18f894bd-7ff8-4d0b-9b2f-69e945c550e9} Name OpenCalendarSync"
MARKER_NAME = "openCalendarSync"
SCOPES = {
    "google": "openid email https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "microsoft": "openid offline_access User.Read Calendars.ReadWrite",
}
ROOTS = {"google": "https://www.googleapis.com/calendar/v3", "microsoft": "https://graph.microsoft.com/v1.0"}


class ProviderError(Exception):
    """Safe to display: never contains a token, response body, or event title."""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def oauth_endpoints(settings, provider):
    if provider == "google":
        return "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token"
    root = f"https://login.microsoftonline.com/{settings.microsoft_tenant}/oauth2/v2.0"
    return root + "/authorize", root + "/token"


def oauth_client(settings, provider, **kwargs):
    return AsyncOAuth2Client(
        client_id=getattr(settings, f"{provider}_client_id"),
        client_secret=getattr(settings, f"{provider}_client_secret"),
        scope=SCOPES[provider],
        token_endpoint_auth_method="client_secret_post",
        redirect_uri=f"{settings.base_url}/oauth/{provider}/callback",
        code_challenge_method="S256",
        timeout=30,
        **kwargs,
    )


async def exchange(settings, provider, code, verifier):
    _, endpoint = oauth_endpoints(settings, provider)
    async with oauth_client(settings, provider) as client:
        token = dict(await client.fetch_token(endpoint, code=code, code_verifier=verifier))
    scopes = set(token.get("scope", "").lower().split())
    needed = (
        {
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        }
        if provider == "google"
        else {"calendars.readwrite", "user.read"}
    )
    if not needed.issubset(scopes):
        raise ProviderError(
            "Calendar permissions were not granted. Reconnect and allow the requested access."
        )
    async with httpx.AsyncClient(timeout=30) as client:
        url = (
            "https://openidconnect.googleapis.com/v1/userinfo"
            if provider == "google"
            else ("https://graph.microsoft.com/v1.0/me?$select=id,mail,userPrincipalName")
        )
        response = await client.get(url, headers={"Authorization": f"Bearer {token['access_token']}"})
        if response.status_code != 200:
            raise ProviderError("Could not verify the connected account.")
        profile = response.json()
    if provider == "google":
        if not profile.get("email_verified"):
            raise ProviderError("A verified Google email address is required.")
        return profile["sub"], profile["email"], token
    return profile["id"], profile.get("mail") or profile["userPrincipalName"], token


class Calendar:
    def __init__(self, settings, store, account, transport=None):
        self.settings, self.store, self.account = settings, store, account
        self.provider = account["provider"]
        self.root = ROOTS[self.provider]
        self.transport = transport

    async def access_token(self, force=False):
        token = self.store.token(self.account["id"])
        if force or token.get("expires_at", 0) < time.time() + 90:
            try:
                async with oauth_client(self.settings, self.provider, token=token) as client:
                    new = dict(
                        await client.refresh_token(
                            oauth_endpoints(self.settings, self.provider)[1],
                            refresh_token=token["refresh_token"],
                        )
                    )
                new["refresh_token"] = new.get("refresh_token") or token["refresh_token"]
                self.store.save_token(self.account["id"], new)
                token = new
            except Exception as exc:
                raise ProviderError("Account access expired or was revoked. Reconnect the account.") from exc
        return token["access_token"]

    async def request(self, method, path, *, params=None, body=None, headers=None, absent_ok=False):
        url = path if path.startswith("https://") else self.root + path
        parsed, root = urlsplit(url), urlsplit(self.root)
        if (parsed.scheme, parsed.netloc) != (root.scheme, root.netloc) or not parsed.path.startswith(
            root.path + "/"
        ):
            raise ProviderError("Provider returned an unexpected pagination address.")
        token = await self.access_token()
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        json=body,
                        headers={"Authorization": f"Bearer {token}", **(headers or {})},
                    )
            except httpx.HTTPError as exc:
                # The next cycle recovers interrupted writes by marker and durable intent.
                raise ProviderError("Calendar service could not be reached. No cleanup will run.") from exc
            if response.status_code == 401 and attempt == 0:
                token = await self.access_token(force=True)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                try:
                    delay = float(response.headers.get("Retry-After", 2**attempt))
                except ValueError:
                    delay = 2**attempt
                if delay > 30:
                    raise ProviderError("Calendar service requested a longer pause; try the next cycle.", 429)
                await asyncio.sleep(max(0, delay))
                continue
            if response.status_code in {404, 410} and absent_ok:
                return None
            if response.is_error:
                raise ProviderError(
                    f"{self.provider.title()} calendar request failed (HTTP {response.status_code}).",
                    response.status_code,
                )
            return response.json() if response.content else {}
        raise ProviderError("Calendar service is temporarily unavailable.")

    @property
    def collection(self):
        return "/calendars/primary/events" if self.provider == "google" else "/me/calendar/events"

    def item_path(self, event_id):
        return self.collection + "/" + quote(event_id, safe="")

    async def pages(self, path, params):
        items, seen = [], set()
        for _ in range(1000):
            data = await self.request("GET", path, params=params)
            field = "items" if self.provider == "google" else "value"
            if field not in data:
                # An incomplete/malformed response is never an empty calendar.
                raise ProviderError("Calendar service returned an incomplete snapshot.")
            items.extend(data[field])
            next_page = (
                data.get("nextPageToken") if self.provider == "google" else data.get("@odata.nextLink")
            )
            if not next_page:
                return items
            if next_page in seen:
                break
            seen.add(next_page)
            if self.provider == "google":
                params = {**params, "pageToken": next_page}
            else:
                path, params = next_page, None
        raise ProviderError("Calendar pagination did not complete; sync stopped before making changes.")

    async def snapshot(self, start, end):
        if self.provider == "google":
            params = {"timeMin": start, "timeMax": end, "singleEvents": "true", "maxResults": 2500}
            raw = await self.pages(self.collection, params)
        else:
            params = {
                "startDateTime": start,
                "endDateTime": end,
                "$top": 1000,
                "$expand": f"singleValueExtendedProperties($filter=id eq '{PROPERTY}')",
            }
            raw = await self.pages("/me/calendar/calendarView", params)
        return [await self.normalize(item) for item in raw if not self.cancelled(item)]

    def cancelled(self, item):
        return item.get("status") == "cancelled" or item.get("isCancelled", False)

    async def normalize(self, item):
        if self.provider == "google":
            marker = item.get("extendedProperties", {}).get("private", {}).get(MARKER_NAME, "")
            all_day = "date" in item["start"]
            field = "date" if all_day else "dateTime"
            declined = any(
                a.get("self") and a.get("responseStatus") == "declined" for a in item.get("attendees", [])
            )
            return Event(
                item["id"],
                item["start"][field],
                item["end"][field],
                all_day,
                item.get("iCalUID", ""),
                marker,
                item.get("transparency") != "transparent"
                and not declined
                and item.get("eventType") != "workingLocation",
                item.get("etag", ""),
                bool(item.get("attendees")),
                item.get("summary") == "Busy"
                and item.get("visibility") == "private"
                and not item.get("description")
                and not item.get("location")
                and not item.get("reminders", {}).get("useDefault", True)
                and not item.get("reminders", {}).get("overrides"),
            )
        marker = next(
            (p["value"] for p in item.get("singleValueExtendedProperties", []) if p["id"] == PROPERTY), ""
        )
        all_day = item.get("isAllDay", False)
        if all_day and item.get("originalStartTimeZone", "UTC") != "UTC":
            # Graph normally returns UTC; restore the original zone before extracting calendar dates.
            zone = item["originalStartTimeZone"].replace('"', "")
            dates = await self.request(
                "GET", self.item_path(item["id"]), headers={"Prefer": f'outlook.timezone="{zone}"'}
            )
        else:
            dates = item

        def value(part):
            raw = dates[part]["dateTime"]
            if all_day:
                return raw[:10]
            # Graph default responses use UTC. Refuse ambiguous times instead of shifting busy blocks.
            if dates[part].get("timeZone", "UTC") != "UTC":
                raise ProviderError("Microsoft returned an unexpected time zone.")
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).isoformat()

        return Event(
            item["id"],
            value("start"),
            value("end"),
            all_day,
            item.get("iCalUId", ""),
            marker,
            item.get("showAs") not in {"free", "workingElsewhere"}
            and item.get("responseStatus", {}).get("response") != "declined",
            item.get("@odata.etag", ""),
            bool(item.get("attendees")),
            item.get("subject") == "Busy"
            and item.get("sensitivity") == "private"
            and not item.get("isReminderOn", True)
            and not item.get("body", {}).get("content")
            and not item.get("location", {}).get("displayName"),
        )

    async def get(self, remote_id):
        params = (
            None
            if self.provider == "google"
            else {"$expand": f"singleValueExtendedProperties($filter=id eq '{PROPERTY}')"}
        )
        item = await self.request("GET", self.item_path(remote_id), params=params, absent_ok=True)
        return None if item is None or self.cancelled(item) else await self.normalize(item)

    async def find(self, marker):
        if self.provider == "google":
            params = {"privateExtendedProperty": f"{MARKER_NAME}={marker}", "maxResults": 2500}
        else:
            params = {
                "$filter": f"singleValueExtendedProperties/Any(ep: ep/id eq '{PROPERTY}' "
                f"and ep/value eq '{marker}')",
                "$expand": f"singleValueExtendedProperties($filter=id eq '{PROPERTY}')",
            }
        items = [item for item in await self.pages(self.collection, params) if not self.cancelled(item)]
        if len(items) > 1:
            raise ProviderError("Duplicate managed copies found. Sync paused for inspection.")
        return await self.normalize(items[0]) if items else None

    def body(self, event, marker):
        if self.provider == "google":
            field = "date" if event.all_day else "dateTime"
            return {
                "summary": "Busy",
                "start": {field: event.start},
                "end": {field: event.end},
                "visibility": "private",
                "transparency": "opaque",
                "description": "",
                "location": "",
                "attendees": [],
                "reminders": {"useDefault": False},
                "extendedProperties": {"private": {MARKER_NAME: marker}},
            }

        def graph_time(value):
            if event.all_day:
                return {"dateTime": value + "T00:00:00", "timeZone": "UTC"}
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
            return {"dateTime": stamp.replace(tzinfo=None).isoformat(), "timeZone": "UTC"}

        return {
            "subject": "Busy",
            "start": graph_time(event.start),
            "end": graph_time(event.end),
            "isAllDay": event.all_day,
            "showAs": "busy",
            "sensitivity": "private",
            "isReminderOn": False,
            "attendees": [],
            "body": {"contentType": "text", "content": ""},
            "location": {"displayName": ""},
            "singleValueExtendedProperties": [{"id": PROPERTY, "value": marker}],
        }

    async def create(self, event, marker, transaction_id):
        body = self.body(event, marker)
        body["id" if self.provider == "google" else "transactionId"] = transaction_id
        params = {"sendUpdates": "none"} if self.provider == "google" else None
        response = await self.request("POST", self.collection, body=body, params=params)
        return response["id"]

    async def update(self, remote_id, event, marker, etag=""):
        # Engine validates ownership before this request; originals never enter this path.
        params = {"sendUpdates": "none"} if self.provider == "google" else None
        await self.request(
            "PATCH",
            self.item_path(remote_id),
            body=self.body(event, marker),
            params=params,
            headers={"If-Match": etag} if etag else None,
        )

    async def delete(self, remote_id, etag=""):
        params = {"sendUpdates": "none"} if self.provider == "google" else None
        await self.request(
            "DELETE",
            self.item_path(remote_id),
            params=params,
            absent_ok=True,
            headers={"If-Match": etag} if etag else None,
        )
