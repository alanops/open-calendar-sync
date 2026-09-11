import asyncio
from datetime import datetime, timedelta, timezone

from .models import plan
from .providers import Calendar, ProviderError


class Engine:
    def __init__(self, settings, store, factory=Calendar):
        self.settings, self.store, self.factory = settings, store, factory
        self.lock = asyncio.Lock()

    def marker(self, key):
        return f"ocs:v1:{self.store.get('installation')}:{key}"

    def clients(self):
        return {a["id"]: self.factory(self.settings, self.store, a) for a in self.store.accounts()}

    async def snapshot(self, clients):
        now = datetime.now(timezone.utc)
        start, end = (
            (now - timedelta(days=7)).isoformat(),
            (now + timedelta(days=self.settings.days_ahead)).isoformat(),
        )
        enabled = [a["id"] for a in self.store.accounts() if a["enabled"]]
        snapshots = {}
        for account in enabled:
            snapshots[account] = await clients[account].snapshot(start, end)
        return snapshots

    async def preview(self):
        async with self.lock:
            desired = plan(await self.snapshot(self.clients()))
            return {
                "busy_copies": len(desired),
                "accounts": sum(a["enabled"] for a in self.store.accounts()),
                "days_ahead": self.settings.days_ahead,
                "managed_copies": len(self.store.copies()),
            }

    async def resolve(self, client, row, marker, cached=None):
        if row["remote_id"]:
            event = (cached or {}).get(row["remote_id"]) or await client.get(row["remote_id"])
        else:
            # Recover a successful POST whose response was lost, even outside the active time window.
            event = await client.find(marker)
        if event and event.marker != marker:
            raise ProviderError(
                "A managed copy lost its ownership marker; no changes were made to that event."
            )
        if event and event.has_guests:
            raise ProviderError(
                "A managed copy has guests. Remove them before syncing; invitations are never sent."
            )
        return event

    async def remove_copy(self, client, key, row):
        event = await self.resolve(client, row, self.marker(key))
        if event:
            await client.delete(event.id, event.etag)
        self.store.forget(key)

    async def run(self):
        if self.lock.locked():
            return {"status": "running", "message": "A sync is already running."}
        async with self.lock:
            if self.store.get("paused") == "true":
                return {"status": "paused", "message": "Automatic sync is paused."}
            try:
                clients = self.clients()
                snapshots = await self.snapshot(clients)
                if len(snapshots) < 2:
                    raise ProviderError("Enable at least two calendars before starting sync.")
                # Read every source successfully before planning any write or cleanup.
                cached = {
                    account: {event.id: event for event in events} for account, events in snapshots.items()
                }
                # Detect tampered tracked copies before they could be mistaken for new originals.
                for key, row in self.store.copies().items():
                    if row["remote_id"] in cached.get(row["target"], {}):
                        await self.resolve(
                            clients[row["target"]], row, self.marker(key), cached[row["target"]]
                        )
                desired = plan(snapshots)
                created = updated = removed = 0
                for key, copy in desired.items():
                    client, marker = clients[copy.target], self.marker(key)
                    row = self.store.intent(key, copy.target)
                    remote = await self.resolve(client, row, marker, cached.get(copy.target))
                    if remote is None:
                        if row["remote_id"]:
                            # A manually deleted Google event ID cannot safely be reused.
                            self.store.forget(key)
                            row = self.store.intent(key, copy.target)
                        try:
                            remote_id = await client.create(copy.event, marker, row["transaction_id"])
                        except ProviderError as exc:
                            if exc.status != 409:
                                raise
                            recovered = await client.find(marker)
                            if recovered is None:
                                raise
                            remote_id = recovered.id
                        self.store.remember(key, remote_id)
                        created += 1
                    else:
                        self.store.remember(key, remote.id)
                        if (
                            remote.signature[1:] != copy.event.signature[1:]
                            or not remote.busy
                            or not remote.private_copy
                        ):
                            await client.update(remote.id, copy.event, marker, remote.etag)
                            updated += 1
                # Only remove obsolete copies after all desired writes succeeded.
                for key, row in self.store.copies().items():
                    if key not in desired:
                        if row["target"] not in clients:
                            raise ProviderError(
                                "Reconnect a missing destination before cleaning up its copies."
                            )
                        await self.remove_copy(clients[row["target"]], key, row)
                        removed += 1
                message = f"{created} created · {updated} updated · {removed} removed"
                self.store.record("success", message)
                return {"status": "success", "message": message}
            except ProviderError as exc:
                self.store.record("error", str(exc))
                return {"status": "error", "message": str(exc)}
            except Exception:
                # Never log provider bodies, OAuth codes or calendar data.
                message = "Sync stopped after an unexpected error. Existing originals were not changed."
                self.store.record("error", message)
                return {"status": "error", "message": message}

    async def disconnect(self, account_id):
        """Clean up while credentials still exist; keep account if any cleanup request fails."""
        async with self.lock:
            self.store.set("paused", "true")
            clients = self.clients()
            if account_id not in clients:
                raise ProviderError("Account not found.")
            # Paused, explicit cleanup removes all app-created copies, leaving originals untouched.
            # Re-enable sync afterwards to rebuild copies for the remaining selected accounts.
            for key, row in self.store.copies().items():
                await self.remove_copy(clients[row["target"]], key, row)
            self.store.remove_account(account_id)
            self.store.record("success", "Account disconnected. Managed busy copies cleaned up. Sync paused.")

    async def loop(self):
        while True:
            await self.run()
            await asyncio.sleep(self.settings.interval_seconds)
