"""The parallel bookkeeping store of the shared paid-call ledger.

Read "Parallel bookkeeping" in ``docs/SHARED_MODEL_BROKER.md`` before you
change anything here.

The ledger used to be one JSON file that every operation read whole, validated
whole and wrote whole. Measured on 2026-09-17 against the live ledger (8.2 MB,
6,036 request rows) that cost about 3 seconds of serialised bookkeeping per
paid call, and the number of calls in flight settles at the call length over
the admission length. The store keeps the same state and the same guarantees
and makes the cost of one operation proportional to what that operation
changed:

* the JSON ledger file stays where it is as the compacted **snapshot**,
* every committed mutation is one line appended to the **journal** beside it,
* the state of the ledger is the snapshot with every journal record of a
  higher sequence number applied, in order,
* a record is an absolute assignment, never an increment, so replaying it
  again changes nothing and a crash at any point leaves a store that
  reconstructs exactly.

A process holds the state in memory and refreshes it from the tail of the
journal, so a read costs the records another process appended since the last
one, not the whole history.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .util import atomic_json, atomic_write, canonical_json, sha256_bytes, sha256_file

JOURNAL_RECORD_SCHEMA = "shared-paid-call-ledger-journal-v1"
JOURNAL_BASE_SCHEMA = "shared-paid-call-ledger-journal-base-v1"

# The ledger maps whose keys are tracked one by one. Everything else at the top
# level of the ledger is a scalar or a small list that the broker assigns
# whole.
TRACKED_MAPS = (
    "requests",
    "stages",
    "papers",
    "live_test_papers",
    "family_bindings",
    "paper_bindings",
    "accepted_families",
)

# The snapshot is rewritten off the hot path. Every reader inside this
# repository reads the store, so the interval bounds only how stale an outside
# reader of the plain JSON file can be.
COMPACTION_INTERVAL_SECONDS = 30.0
COMPACTION_RECORDS = 400
# A journal larger than this is rotated aside at the next compaction. The new
# snapshot already holds every record of it.
JOURNAL_ROTATION_BYTES = 64 * 1024 * 1024


class _Tracked(dict):
    """A dict that reports the keys a caller changed.

    The broker mutates the ledger in place, deep inside 8,000 lines of
    accounting. Diffing two copies of a 6,000-row ledger costs more than the
    write it would save, so the containers report their own changes instead.
    A row reports to its map, and a map reports to the ledger.
    """

    __slots__ = ("_dirty", "_parent", "_parent_key", "_wrap_rows")

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        *,
        parent: "_Tracked | None" = None,
        parent_key: str | None = None,
        wrap_rows: bool = False,
    ) -> None:
        super().__init__(data or {})
        self._dirty: set[str] = set()
        self._parent = parent
        self._parent_key = parent_key
        # A map of rows wraps every row it is given, so a caller that replaces
        # a row with a plain dict and then edits that dict in place is still
        # reported. The broker does exactly this.
        self._wrap_rows = wrap_rows

    # -- change reporting ------------------------------------------------
    @property
    def dirty(self) -> set[str]:
        return self._dirty

    def clear_dirty(self) -> None:
        self._dirty = set()
        for value in self.values():
            if isinstance(value, _Tracked):
                value.clear_dirty()

    def _touch(self, key: Any) -> None:
        self._dirty.add(str(key))
        parent = self._parent
        if parent is not None and self._parent_key is not None:
            parent._touch(self._parent_key)

    def _touch_self(self) -> None:
        """Report this whole container as changed to its parent."""
        parent = self._parent
        if parent is not None and self._parent_key is not None:
            parent._touch(self._parent_key)

    # -- mutating dict interface -----------------------------------------
    def _prepare(self, key: Any, value: Any) -> Any:
        if self._wrap_rows and isinstance(value, dict):
            if isinstance(value, _Tracked):
                value._parent = self
                value._parent_key = str(key)
                return value
            return _Tracked(value, parent=self, parent_key=str(key))
        return value

    def __setitem__(self, key: Any, value: Any) -> None:
        self._touch(key)
        super().__setitem__(key, self._prepare(key, value))

    def __delitem__(self, key: Any) -> None:
        self._touch(key)
        super().__delitem__(key)

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        incoming: dict[str, Any] = dict(*args, **kwargs)
        for key, value in incoming.items():
            self._touch(key)
            super().__setitem__(key, self._prepare(key, value))

    def setdefault(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        if key not in self:
            self._touch(key)
            super().__setitem__(key, self._prepare(key, default))
        return super().__getitem__(key)

    def pop(self, key: Any, *default: Any) -> Any:  # type: ignore[override]
        if key in self:
            self._touch(key)
        return super().pop(key, *default)

    def popitem(self) -> Any:  # type: ignore[override]
        key, value = super().popitem()
        self._touch(key)
        return key, value

    def clear(self) -> None:  # type: ignore[override]
        for key in list(self):
            self._touch(key)
        super().clear()


def wrap(ledger: dict[str, Any]) -> _Tracked:
    """Return the ledger as tracked containers, with nothing marked changed."""
    root = _Tracked(ledger)
    for name in TRACKED_MAPS:
        source = ledger.get(name)
        if not isinstance(source, dict):
            continue
        tracked_map = _Tracked(parent=root, parent_key=name, wrap_rows=True)
        for key, value in source.items():
            dict.__setitem__(tracked_map, key, tracked_map._prepare(key, value))
        dict.__setitem__(root, name, tracked_map)
    root.clear_dirty()
    return root


def plain(value: Any) -> Any:
    """Return the same state as ordinary dicts and lists."""
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def _delta(ledger: _Tracked) -> dict[str, Any]:
    """Describe every change made to the ledger since the last commit.

    A delta is an absolute assignment. ``set`` gives the new value of a key
    and ``removed`` names a key that is gone, so applying the same record
    again changes nothing.
    """
    delta: dict[str, Any] = {}
    for name in sorted(ledger.dirty):
        value = ledger.get(name, _MISSING)
        if name in TRACKED_MAPS and isinstance(value, _Tracked):
            changed: dict[str, Any] = {}
            removed: list[str] = []
            for key in sorted(value.dirty):
                if key in value:
                    changed[key] = plain(value[key])
                else:
                    removed.append(key)
            entry: dict[str, Any] = {}
            if changed:
                entry["set"] = changed
            if removed:
                entry["removed"] = removed
            delta[name] = entry
            continue
        if value is _MISSING:
            delta[name] = {"removed_key": True}
        else:
            delta[name] = {"value": plain(value)}
    return delta


_MISSING = object()


def apply_delta(state: dict[str, Any], delta: dict[str, Any]) -> None:
    """Apply one journal record's delta to a materialized ledger."""
    for name, entry in delta.items():
        if not isinstance(entry, dict):
            raise ValueError("a paid-call journal delta entry is invalid")
        if "value" in entry:
            state[name] = entry["value"]
            continue
        if entry.get("removed_key"):
            state.pop(name, None)
            continue
        target = state.get(name)
        if not isinstance(target, dict):
            target = {}
            state[name] = target
        for key, value in (entry.get("set") or {}).items():
            target[key] = value
        for key in entry.get("removed") or []:
            target.pop(key, None)


def journal_file(ledger_file: Path) -> Path:
    return ledger_file.with_name(f".{ledger_file.name}.journal")


def journal_base_file(ledger_file: Path) -> Path:
    return ledger_file.with_name(f".{ledger_file.name}.journal.base.json")


def compaction_lock_file(ledger_file: Path) -> Path:
    return ledger_file.with_name(f".{ledger_file.name}.compaction.lock")


@contextlib.contextmanager
def held_compaction_lock(ledger_file: Path) -> Iterator[bool]:
    """Hold the compaction lock, or report that another compactor holds it.

    Compaction never takes the shared ledger lock, so it never delays a paid
    call. It takes this one instead, which orders the base record and the
    snapshot it names against every other compactor of the same ledger.
    """
    path = compaction_lock_file(ledger_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read_json(path: Path) -> Any:
    with path.open("rb") as handle:
        return json.loads(handle.read().decode())


def _iter_records(data: bytes) -> Iterator[dict[str, Any]]:
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        record = json.loads(line.decode())
        if (
            not isinstance(record, dict)
            or record.get("schema") != JOURNAL_RECORD_SCHEMA
            or not isinstance(record.get("seq"), int)
            or not isinstance(record.get("delta"), dict)
        ):
            raise ValueError("a paid-call journal record is invalid")
        yield record


def _complete_bytes(data: bytes) -> tuple[bytes, int]:
    """Return the records that are whole, and how many bytes they occupy.

    A record is one line ending in a newline. A torn tail, which only a crash
    in the middle of an append can leave, is not a record and is ignored until
    it is complete.
    """
    end = data.rfind(b"\n")
    if end < 0:
        return b"", 0
    return data[: end + 1], end + 1


def apply_journal(ledger_file: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Apply the journal to a snapshot a reader already has in hand.

    A reader that took its own bytes of the snapshot file uses this, so the
    snapshot it read and the journal it applies are the two halves of one
    state.
    """
    ledger_file = Path(ledger_file)
    applied_seq = snapshot_applied_seq(ledger_file)
    path = journal_file(ledger_file)
    if not path.is_file():
        return snapshot
    whole, _offset = _complete_bytes(path.read_bytes())
    for record in _iter_records(whole):
        if int(record["seq"]) <= applied_seq:
            continue
        apply_delta(snapshot, record["delta"])
    return snapshot


def read_ledger(ledger_file: Path) -> dict[str, Any]:
    """Materialize the ledger for a reader outside the broker.

    Every reader in this repository calls this, never ``json.load`` of the
    ledger file: the file is the compacted snapshot and the journal beside it
    holds everything committed since that compaction.
    """
    state, _seq, _offset = materialize(Path(ledger_file))
    return state


def snapshot_applied_seq(ledger_file: Path) -> int:
    """Say which journal records the snapshot on disk already holds.

    The base record binds the snapshot by its hash. A snapshot that matches
    neither the base record nor the one it superseded was changed outside the
    store, which is an integrity failure: replaying the journal over it would
    quietly repair whatever a hand edit did to a field no record names.
    """
    base_path = journal_base_file(ledger_file)
    journal = journal_file(ledger_file)
    has_journal = journal.is_file() and journal.stat().st_size > 0
    if not base_path.is_file():
        if has_journal:
            raise ValueError("the shared paid-call ledger journal has no base record")
        return 0
    base = _read_json(base_path)
    if (
        not isinstance(base, dict)
        or base.get("schema") != JOURNAL_BASE_SCHEMA
        or not isinstance(base.get("applied_seq"), int)
    ):
        raise ValueError("the shared paid-call ledger base record is invalid")
    digest = sha256_file(ledger_file)
    if digest == base.get("snapshot_sha256"):
        return int(base["applied_seq"])
    superseded = base.get("supersedes")
    if isinstance(superseded, dict) and digest == superseded.get("snapshot_sha256"):
        # The base record is written before the snapshot it names, so a stop
        # between the two leaves the snapshot it superseded. That one is
        # bound too, and the records after it replay onto it.
        return int(superseded["applied_seq"])
    raise ValueError("the shared paid-call ledger snapshot changed outside the store")


def materialize(ledger_file: Path) -> tuple[dict[str, Any], int, int]:
    state = _read_json(ledger_file)
    if not isinstance(state, dict):
        raise ValueError("the shared paid-call ledger snapshot is invalid")
    applied_seq = snapshot_applied_seq(ledger_file)
    path = journal_file(ledger_file)
    if not path.is_file():
        return state, applied_seq, 0
    data = path.read_bytes()
    whole, offset = _complete_bytes(data)
    highest = applied_seq
    for record in _iter_records(whole):
        seq = int(record["seq"])
        if seq <= applied_seq:
            continue
        apply_delta(state, record["delta"])
        highest = max(highest, seq)
    return state, highest, offset


class LedgerStore:
    """The snapshot, the journal and one process's view of both.

    One instance belongs to one broker. Every method that touches the files
    runs with the shared ledger lock already held by the caller: the store
    does not lock, it is the thing the lock protects.
    """

    def __init__(self, ledger_file: Path) -> None:
        self.ledger_file = Path(ledger_file)
        self.journal_file = journal_file(self.ledger_file)
        self.base_file = journal_base_file(self.ledger_file)
        self._state: _Tracked | None = None
        self._seq = 0
        self._offset = 0
        self._journal_identity: tuple[int, int] | None = None
        # The snapshot file this view was loaded from. A compaction, a
        # reviewed repair or a test rewrites the snapshot behind a live
        # process, and the next read must see it, exactly as every read of
        # the one-file ledger did.
        self._snapshot_identity: tuple[int, int, int] | None = None
        self._handle: Any = None
        self._records_since_compaction = 0
        self._compacted_at = 0.0
        # What the broker still owes a proof for: every row after a reload,
        # or the rows another process's journal records moved.
        self._pending_full = True
        self._pending_requests: set[str] = set()
        # Group commit. One thread flushes the journal for everybody that
        # appended before it, which is what makes a durable write cost one
        # `fsync` for a wave of concurrent calls instead of one each.
        self._durability = threading.Condition()
        self._written_offset = 0
        self._durable_offset = 0
        self._flushing = False
        self.reload_count = 0
        self.append_count = 0
        self.fsync_count = 0

    @property
    def sequence(self) -> int:
        """The journal sequence number this process has applied."""
        return self._seq

    # -- the state -------------------------------------------------------
    def load(self) -> _Tracked:
        """Materialize the whole store from disk and forget the cached view."""
        state, seq, offset = materialize(self.ledger_file)
        self._state = wrap(state)
        self._seq = seq
        self._offset = offset
        self._journal_identity = self._identity()
        self._snapshot_identity = self._snapshot_stat()
        self.reload_count += 1
        self._pending_full = True
        self._pending_requests = set()
        self._compacted_at = self._compacted_at or time.monotonic()
        return self._state

    def _snapshot_stat(self) -> tuple[int, int, int] | None:
        try:
            stat = os.stat(self.ledger_file)
        except FileNotFoundError:
            return None
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _identity(self) -> tuple[int, int] | None:
        try:
            stat = os.stat(self.journal_file)
        except FileNotFoundError:
            return None
        return (stat.st_dev, stat.st_ino)

    def read(self) -> _Tracked:
        """Return the current ledger state, refreshed from the journal tail."""
        state = self._state
        if state is None:
            return self.load()
        identity = self._identity()
        if identity != self._journal_identity or self._snapshot_stat() != (
            self._snapshot_identity
        ):
            # The journal was rotated or created by another process, or the
            # snapshot was rewritten: by a compaction, which changes nothing,
            # or by a reviewed repair, which must be seen. The snapshot and
            # the journal together are the cheapest truth.
            return self.load()
        if identity is None:
            state.clear_dirty()
            return state
        try:
            size = os.path.getsize(self.journal_file)
        except FileNotFoundError:
            return self.load()
        if size < self._offset:
            return self.load()
        if size > self._offset:
            with self.journal_file.open("rb") as handle:
                handle.seek(self._offset)
                data = handle.read(size - self._offset)
            whole, used = _complete_bytes(data)
            for record in _iter_records(whole):
                seq = int(record["seq"])
                if seq <= self._seq:
                    continue
                apply_delta(state, record["delta"])
                self._note_applied(record["delta"])
                self._seq = max(self._seq, seq)
            self._offset += used
        state.clear_dirty()
        return state

    def _note_applied(self, delta: dict[str, Any]) -> None:
        entry = delta.get("requests")
        if not isinstance(entry, dict):
            return
        if "value" in entry:
            self._pending_full = True
            return
        self._pending_requests.update(entry.get("set") or {})
        self._pending_requests.update(entry.get("removed") or [])

    def take_changes(self) -> tuple[bool, set[str]]:
        """Say what the broker still owes a proof for, and forget it."""
        full = self._pending_full
        keys = self._pending_requests
        self._pending_full = False
        self._pending_requests = set()
        return full, keys

    def is_dirty(self) -> bool:
        """Report a mutation that was never committed."""
        state = self._state
        return state is not None and bool(state.dirty)

    @staticmethod
    def changed_request_keys(ledger: _Tracked) -> set[str]:
        requests = ledger.get("requests")
        if isinstance(requests, _Tracked):
            return set(requests.dirty)
        return set()

    def discard(self) -> None:
        """Forget the cached view after a mutation that was never committed."""
        self._state = None

    # -- the journal -----------------------------------------------------
    def _journal_handle(self) -> Any:
        handle = self._handle
        if handle is not None:
            try:
                current = os.fstat(handle.fileno())
                live = os.stat(self.journal_file)
            except OSError:
                handle = None
            else:
                if (current.st_dev, current.st_ino) != (live.st_dev, live.st_ino):
                    handle = None
            if handle is None and self._handle is not None:
                try:
                    self._handle.close()
                except OSError:
                    pass
                self._handle = None
        if self._handle is None:
            self.journal_file.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.journal_file.open("ab")
            self._written_offset = self._handle.tell()
            self._durable_offset = self._written_offset
        return self._handle

    def commit(self, ledger: _Tracked, *, now: str) -> bool:
        """Append the changes of one mutation, then make them durable.

        Returns False when the mutation changed nothing. The caller holds the
        shared ledger lock for the append and no longer holds it for the
        ``fsync``: the record is already visible to every other reader of the
        file, and a wave of concurrent calls shares one flush.
        """
        if ledger is not self._state:
            raise ValueError("the committed ledger is not the store's state")
        delta = _delta(ledger)
        if not delta:
            ledger.clear_dirty()
            return False
        self._seq += 1
        record = {
            "schema": JOURNAL_RECORD_SCHEMA,
            "seq": self._seq,
            "at": now,
            "delta": delta,
        }
        line = (canonical_json(record) + "\n").encode()
        handle = self._journal_handle()
        handle.write(line)
        handle.flush()
        end = handle.tell()
        with self._durability:
            self._written_offset = max(self._written_offset, end)
        self._offset = end
        self._records_since_compaction += 1
        self.append_count += 1
        ledger.clear_dirty()
        return True

    def flush(self, *, target: int | None = None) -> None:
        """Make every appended record durable, sharing one flush.

        The money rule is that a reservation is durable before the provider
        call. One thread does the ``fsync`` and every thread that appended
        before it returns as soon as that flush covers its own bytes, so a
        wave of sixteen concurrent calls pays one flush, not sixteen.
        """
        handle = self._handle
        if handle is None:
            return
        want = self._offset if target is None else target
        with self._durability:
            while self._durable_offset < want:
                if self._flushing:
                    self._durability.wait()
                    continue
                self._flushing = True
                covered = self._written_offset
                self._durability.release()
                try:
                    os.fsync(handle.fileno())
                    self.fsync_count += 1
                finally:
                    self._durability.acquire()
                    self._flushing = False
                    self._durable_offset = max(self._durable_offset, covered)
                    self._durability.notify_all()

    # -- the compacted snapshot ------------------------------------------
    def compaction_due(self) -> bool:
        return (
            self._records_since_compaction >= COMPACTION_RECORDS
            or (
                self._records_since_compaction > 0
                and time.monotonic() - self._compacted_at >= COMPACTION_INTERVAL_SECONDS
            )
        )

    def compact(self) -> bool:
        """Write the snapshot, so an outside reader of the JSON file is current.

        The caller holds the shared ledger lock. Nothing here is on the path
        of a paid call.
        """
        state = self._state
        if state is None:
            return False
        self.flush()
        with held_compaction_lock(self.ledger_file) as held:
            if not held:
                return False
            write_snapshot(self.ledger_file, state, self._seq)
        self._records_since_compaction = 0
        self._compacted_at = time.monotonic()
        self._rotate_if_large()
        return True

    def _rotate_if_large(self) -> None:
        try:
            size = os.path.getsize(self.journal_file)
        except FileNotFoundError:
            return
        if size < JOURNAL_ROTATION_BYTES:
            return
        archive = self.journal_file.with_name(
            f"{self.journal_file.name}.{self._seq:012d}"
        )
        os.replace(self.journal_file, archive)
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        self._offset = 0
        self._journal_identity = None
        with self._durability:
            self._written_offset = 0
            self._durable_offset = 0

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def write_snapshot(ledger_file: Path, state: dict[str, Any], seq: int) -> None:
    """Rewrite the compacted snapshot and bind it to the journal.

    The base record is written first and names both the snapshot it is about
    to write and the one that snapshot supersedes, so a stop between the two
    writes leaves a store that still reconstructs and a snapshot that is
    still bound. The caller holds the compaction lock.
    """
    data = (canonical_json(plain(state)) + "\n").encode()
    previous = {
        "applied_seq": snapshot_applied_seq(ledger_file),
        "snapshot_sha256": sha256_file(ledger_file),
    }
    atomic_json(
        journal_base_file(ledger_file),
        {
            "schema": JOURNAL_BASE_SCHEMA,
            "snapshot_sha256": sha256_bytes(data),
            "applied_seq": int(seq),
            "supersedes": previous,
        },
    )
    atomic_write(ledger_file, data)


def initialize_store(ledger_file: Path) -> None:
    """Give a ledger file that has no journal yet an empty one."""
    path = journal_file(ledger_file)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    atomic_json(
        journal_base_file(ledger_file),
        {
            "schema": JOURNAL_BASE_SCHEMA,
            "snapshot_sha256": sha256_file(ledger_file),
            "applied_seq": 0,
            "supersedes": None,
        },
    )


def store_files(ledger_file: Path) -> Iterable[Path]:
    """Every file of the store, for a migration or an archive."""
    ledger_file = Path(ledger_file)
    return (ledger_file, journal_file(ledger_file), journal_base_file(ledger_file))
