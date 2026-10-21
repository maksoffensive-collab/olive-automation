"""Known .record keys.

Everything here was OBSERVED in the 40 real files inspected on 2026-09-21
(`vms/operationRecords/*.record` and `GEternal/G ETERNAL_16-9.record`).
It is [VERIFIED] only for those files. Keys or operation ids outside these sets
are reported as "unknown" and are always preserved; they are never dropped.
"""
from __future__ import annotations

def hashable_key(value):
    """`value` itself if it can be a dict key, otherwise a tagged text form (a JSON list/object as a point id or
    state must be counted and reported, not crash the analysis). The tag keeps it distinct from a string."""
    try:
        hash(value)
    except TypeError:
        return ("<unhashable>", repr(value))
    return value


KNOWN_TOP_KEYS = frozenset({"operations", "recordInfo", "mergedRecords"})

KNOWN_RECORD_INFO_KEYS = frozenset({
    "loopType", "loopTimes", "circleDuration", "loopInterval", "loopDuration",
    "accelerateTimes", "accelerateTimesEx", "recordName", "createTime",
    "playOnBoot", "rebootTiming", "closeTiming", "resolutionWidth",
    "resolutionHeight", "randomTouch", "shortkey", "modifyKey",
})

KNOWN_MERGED_ENTRY_KEYS = frozenset({"operations", "recordInfo"})

# Keys every operation carries, plus the extra keys observed per operationId.
COMMON_OPERATION_KEYS = frozenset({"operationId", "timing"})
KNOWN_OPERATION_PARAMS = {
    "PutMultiTouch": frozenset({"points"}),
    "AndroidBack": frozenset(),
    "ImeClipboard": frozenset({"text"}),
    "StopApp": frozenset({"app"}),
}

KNOWN_POINT_KEYS = frozenset({"id", "x", "y", "state"})

# Canonical key order for objects created by code (matches observed files).
CANONICAL_TOP_ORDER = ("operations", "recordInfo")
CANONICAL_OPERATION_ORDER = ("timing", "operationId", "points")
CANONICAL_POINT_ORDER = ("id", "x", "y", "state")
CANONICAL_MERGED_ENTRY_ORDER = ("operations", "recordInfo")
