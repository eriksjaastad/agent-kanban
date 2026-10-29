"""Client-side unique ID generator (pt #6044).

Background
----------
Rows are keyed by 63-bit integer IDs minted at insert time instead of by
SQLite's rowid/AUTOINCREMENT counter. The scheme was built so two machines
syncing through cr-sqlite could never assign the same PK to different rows.
Sync was retired (#8093) and there is one machine now, but the machine bits
are kept so every ID already stored stays unique against every new one, and
the IDs keep fitting SQLite's signed ``INTEGER`` (2^63 - 1) with no schema
change.

Bit layout (Snowflake-style, 63 bits total)
-------------------------------------------

    bit 62                    22 12          0
     [  timestamp_ms (41)  ][ mid(10) ][ ctr(12) ]
     MSB                                      LSB

- ``timestamp_ms`` (41 bits): milliseconds since ``PT_ID_EPOCH_MS``
  (2026-01-01T00:00:00Z). Rolls over in ~69 years (year 2095).
- ``machine_id`` (10 bits): 0..1023. Identifies the originating
  machine; kept so existing IDs never collide with new ones.
- ``counter`` (12 bits): monotonic counter within each millisecond,
  resets at each new ms. Overflow (4096 IDs in one ms on one machine)
  causes a busy-wait to the next ms — a nominal signal of an anomalous
  burst rate, not a realistic path.

Why 41/10/12 over TodoMVC's 32/16/16:
- ms instead of s timestamp: eliminates "same-second" collisions across
  fast process restarts and sub-second bursts.
- 12-bit counter: 4096/ms = 4M/s per machine, far above any burst
  scenario (agent-driven card factory, task_history on migration).

Machine ID assignment
---------------------

The 10 machine bits come from ``_metadata['pt.machine_id']`` (0..1023). On
the live database, migration 018 wrote the value the old cr-sqlite code had
derived from ``crsql_site_id()``, so IDs minted after the removal carry the
same machine bits as before. A database with no such row (fresh checkout,
tests) uses ``DEFAULT_MACHINE_ID``. IDs stay unique within one database
because the timestamp and counter bits differ; the machine bits only matter
for never colliding with IDs that already exist.

Monotonicity guarantees
-----------------------

- **Within a process:** IDs are strictly increasing. Time advances or
  counter increments; never the reverse.
- **Clock moves backward:** If the wall clock regresses (NTP step,
  laptop hibernation drift), ``next_id`` continues using the last
  observed ms and increments counter. If counter exhausts (rare), it
  advances last_ms by 1 and resets counter. This preserves
  monotonicity at the cost of ID space ahead of wall clock — a mild
  future-dating that resolves when real time catches up.
- **Across process restarts:** no guarantee of strict monotonicity
  across boundaries — IDs can repeat if the clock hasn't advanced and
  machine_id is the same. In practice: clock almost always advances
  between restarts. For strict cross-process monotonicity a persisted
  high-water mark would be required; we don't build that here because
  (a) uniqueness doesn't need it (machine_id plus the counter already
  prevent collisions) and (b) it adds IO per insert.

Collision analysis
------------------

Within one machine_id, two IDs collide only if they share a millisecond and
a counter value, which the counter prevents inside a process. Across process
restarts the clock almost always advances between starts.

**Usage**::

    from db.pt_id import next_id
    from db.schema import get_db_path

    new_id = next_id(db_path=get_db_path())
    conn.execute(
        "INSERT INTO tasks (id, title, ...) VALUES (?, ?, ...)",
        (new_id, title, ...),
    )

The generator is a process-wide thread-safe singleton. First call
performs ``machine_id`` resolution; subsequent calls reuse it.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("pt.id")

# 2026-01-01T00:00:00Z in ms since Unix epoch. Verify with:
#   python3 -c "import datetime; print(datetime.datetime.fromtimestamp(1767225600, tz=datetime.timezone.utc))"
# Chosen as a recent epoch to maximize future timestamp runway (~69 years
# from this epoch, so 41-bit timestamp rollover hits around year 2095).
# Changing this is a BREAKING change — all new IDs shift relative to old ones.
PT_ID_EPOCH_MS = 1767225600000

# Bit widths. Total = 63 so signed SQLite INTEGER (2^63 - 1) fits.
_TS_BITS = 41
_MID_BITS = 10
_CTR_BITS = 12
assert _TS_BITS + _MID_BITS + _CTR_BITS == 63

_MID_MAX = (1 << _MID_BITS) - 1     # 1023
_CTR_MAX = (1 << _CTR_BITS) - 1     # 4095
_TS_MAX = (1 << _TS_BITS) - 1       # ~2.2e12 ms ≈ 69 years past epoch

_MID_SHIFT = _CTR_BITS              # 12
_TS_SHIFT = _CTR_BITS + _MID_BITS   # 22


class PtIdGenerator:
    """Thread-safe 63-bit ID generator. One instance per process."""

    def __init__(self, machine_id: int) -> None:
        if not (0 <= machine_id <= _MID_MAX):
            raise ValueError(
                f"machine_id {machine_id} out of range 0..{_MID_MAX}"
            )
        self._machine_id = machine_id
        self._lock = threading.Lock()
        self._last_ms = 0
        self._counter = -1  # incremented to 0 on first id in a new ms

    @property
    def machine_id(self) -> int:
        return self._machine_id

    def next_id(self) -> int:
        """Return the next 63-bit ID. Thread-safe."""
        with self._lock:
            now_ms = int(time.time() * 1000) - PT_ID_EPOCH_MS
            if now_ms < 0:
                raise RuntimeError(
                    "system clock is before PT_ID_EPOCH_MS "
                    f"(2026-01-01T00:00:00Z); refusing to generate IDs "
                    f"(got offset {now_ms} ms)"
                )
            if now_ms > _TS_MAX:
                raise RuntimeError(
                    f"epoch-relative ms {now_ms} exceeds 41-bit budget "
                    f"(max {_TS_MAX}); pt_id layout needs rotation "
                    "(expected ~year 2095)"
                )

            if now_ms > self._last_ms:
                self._last_ms = now_ms
                self._counter = 0
            elif now_ms == self._last_ms:
                self._counter += 1
                if self._counter > _CTR_MAX:
                    # 4096 IDs in one ms — wait for next ms rather than
                    # collide. Busy-wait is acceptable: this path is
                    # vanishingly rare (4M IDs/s per machine sustained).
                    nxt = int(time.time() * 1000) - PT_ID_EPOCH_MS
                    while nxt <= self._last_ms:
                        nxt = int(time.time() * 1000) - PT_ID_EPOCH_MS
                    self._last_ms = nxt
                    self._counter = 0
            else:
                # Clock moved backward. Preserve monotonicity by staying
                # at last_ms and incrementing counter. Log — this is
                # unusual (NTP step, hibernate drift).
                self._counter += 1
                if self._counter > _CTR_MAX:
                    self._last_ms += 1
                    self._counter = 0
                log.warning(
                    "pt_id: clock moved backward (wall=%d, last=%d); "
                    "continuing at last_ms+counter=%d+%d",
                    now_ms, self._last_ms, self._last_ms, self._counter,
                )

            return (
                (self._last_ms << _TS_SHIFT)
                | (self._machine_id << _MID_SHIFT)
                | self._counter
            )

    def decompose(self, pt_id: int) -> tuple[int, int, int]:
        """Return ``(timestamp_ms_since_epoch, machine_id, counter)``.

        Inverse of ``next_id``'s bit-packing. Useful for debugging and
        tests; not a hot-path function.
        """
        ctr = pt_id & _CTR_MAX
        mid = (pt_id >> _MID_SHIFT) & _MID_MAX
        ts = (pt_id >> _TS_SHIFT) & _TS_MAX
        return ts, mid, ctr


# ---------------------------------------------------------------------
# Machine-ID resolution
# ---------------------------------------------------------------------

# Machine bits used when the database has no explicit _metadata value.
DEFAULT_MACHINE_ID = 0


def load_machine_id(db_path: Optional[Path]) -> int:
    """Resolve this process's ``machine_id``.

    Returns ``_metadata['pt.machine_id']`` when the database has it, else
    ``DEFAULT_MACHINE_ID`` (no db_path, missing file, no ``_metadata`` table
    or no row). A stored value that is not an int in ``0..1023`` raises
    ``ValueError``.

    Caller should hold the resulting int for the process lifetime.
    """
    if db_path is None or not db_path.exists():
        return DEFAULT_MACHINE_ID

    # Only a missing table means "use the default". Any read error (locked,
    # I/O) propagates: caching the default would change this process's id bits.
    conn = sqlite3.connect(db_path)
    try:
        has_metadata = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_metadata'"
        ).fetchone() is not None
        row = conn.execute(
            "SELECT value FROM _metadata WHERE key = 'pt.machine_id'"
        ).fetchone() if has_metadata else None
    finally:
        conn.close()

    if row is None:
        return DEFAULT_MACHINE_ID
    try:
        mid = int(row[0])
    except (TypeError, ValueError):
        raise ValueError(
            f"_metadata['pt.machine_id'] must be an int, got {row[0]!r}"
        )
    if not (0 <= mid <= _MID_MAX):
        raise ValueError(
            f"_metadata['pt.machine_id']={mid} out of range 0..{_MID_MAX}"
        )
    return mid


# ---------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------

_singleton: Optional[PtIdGenerator] = None
_singleton_lock = threading.Lock()


def get_generator(db_path: Optional[Path] = None) -> PtIdGenerator:
    """Return the process-wide ``PtIdGenerator`` singleton.

    First call resolves ``machine_id`` via ``load_machine_id(db_path)``
    and instantiates; subsequent calls return the same instance and
    ignore ``db_path``. Thread-safe.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            mid = load_machine_id(db_path)
            _singleton = PtIdGenerator(mid)
        return _singleton


def next_id(db_path: Optional[Path] = None) -> int:
    """Convenience: get next ID from the process singleton.

    Suitable for one-liner INSERT paths::

        conn.execute(
            "INSERT INTO tasks (id, title, ...) VALUES (?, ?, ...)",
            (next_id(), title, ...),
        )
    """
    return get_generator(db_path).next_id()


def reset_for_testing() -> None:
    """Clear the process singleton.

    Tests only. Guarded by ``pytest in sys.modules`` to prevent
    accidental production use — resetting mid-run could produce IDs
    under a different ``machine_id`` if the operator changed
    ``_metadata['pt.machine_id']`` between calls, which is exactly
    the identity drift this module is designed to prevent.
    """
    if "pytest" not in sys.modules:
        raise RuntimeError(
            "reset_for_testing() called outside a pytest run; refusing. "
            "Resetting the singleton mid-process can produce IDs under "
            "different machine_ids, which is the exact identity drift "
            "this module prevents."
        )
    global _singleton
    with _singleton_lock:
        _singleton = None
