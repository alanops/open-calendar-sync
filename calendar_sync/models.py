import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Event:
    id: str
    start: str
    end: str
    all_day: bool = False
    uid: str = ""
    marker: str = ""
    busy: bool = True
    etag: str = ""
    has_guests: bool = False
    private_copy: bool = True

    def __post_init__(self):
        if self.all_day:
            if len(self.start) != 10 or len(self.end) != 10:
                raise ValueError("All-day events require calendar dates")
            start, end = date.fromisoformat(self.start), date.fromisoformat(self.end)
        else:
            start = datetime.fromisoformat(self.start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.end.replace("Z", "+00:00"))
            if start.tzinfo is None or end.tzinfo is None:
                raise ValueError("Timed events require explicit UTC offsets")
        if end <= start:
            raise ValueError("Event end must be after its start")

    @property
    def signature(self):
        def canonical(value):
            return value if self.all_day else datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

        return self.uid, canonical(self.start), canonical(self.end), self.all_day


@dataclass(frozen=True)
class Copy:
    key: str
    source: str
    target: str
    event: Event


def plan(snapshots):
    """All-to-all, excluding copies, free events and duplicate meeting invitations."""
    originals = {
        account: [e for e in events if e.busy and not e.marker] for account, events in snapshots.items()
    }
    desired = {}
    for target in sorted(originals):
        already = {e.signature for e in originals[target] if e.uid}
        for source in sorted(originals):
            if target == source:
                continue
            for event in sorted(originals[source], key=lambda e: e.id):
                if event.uid and event.signature in already:
                    continue
                if event.uid:
                    already.add(event.signature)
                key = hashlib.sha256(json.dumps([source, event.id, target]).encode()).hexdigest()
                desired[key] = Copy(key, source, target, event)
    return desired
