"""Monitor state persisted to disk, so a bot restart neither re-announces every
open alert nor misses ones that cleared while it was down."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class KnownAlert:
    title: str
    level: str
    klass: str


@dataclass
class State:
    # None = never polled successfully -> first poll only records a baseline.
    known_alerts: dict[str, KnownAlert] | None = None
    app_states: dict[str, str] | None = None
    boottime: float | None = None
    hostname: str | None = None
    consecutive_failures: int = 0
    first_failure_at: float | None = None
    down_since: float | None = None  # set once the unreachable notification has been sent
    silence_until: float | None = None
    suppressed: list[str] = field(default_factory=list)
    last_digest_date: str | None = None
    # Media watcher, keyed by app ("sonarr" / "radarr"). An app missing from media_health has not been
    # baselined yet, so its first health check is silent (like the first TrueNAS alert poll).
    media_attention: dict[str, dict[str, dict]] = field(default_factory=dict)
    media_health: dict[str, dict[str, dict]] = field(default_factory=dict)
    media_failures: dict[str, int] = field(default_factory=dict)
    media_down_since: dict[str, float] = field(default_factory=dict)
    media_import_cursor: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        data = asdict(self)
        data["version"] = STATE_VERSION
        return data

    @classmethod
    def from_json(cls, data: dict) -> State:
        data = dict(data)
        data.pop("version", None)
        known = data.get("known_alerts")
        if known is not None:
            data["known_alerts"] = {k: KnownAlert(**v) for k, v in known.items()}
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> State:
        try:
            raw = json.loads(self.path.read_text())
            return State.from_json(raw)
        except FileNotFoundError:
            return State()
        except (OSError, ValueError, TypeError) as exc:
            backup = self.path.with_suffix(".corrupt")
            log.error("State file %s unreadable (%s); starting fresh, old copy kept at %s", self.path, exc, backup)
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return State()

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_json(), indent=2, sort_keys=True))
        os.replace(tmp, self.path)  # atomic: a crash mid-write never leaves half a file


class MemoryStateStore(StateStore):
    """For tests and the `check` command."""

    def __init__(self, state: State | None = None):
        self.state = state or State()
        self.saves = 0

    def load(self) -> State:
        return State.from_json(self.state.to_json())

    def save(self, state: State) -> None:
        self.state = State.from_json(state.to_json())
        self.saves += 1
