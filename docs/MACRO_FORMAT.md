# MACRO_FORMAT (.record)

Tag legend: **[VERIFIED]** observed or tested on this machine; **[ASSUMPTION]** believed, unchecked; **[NEEDS TESTING]** must be tested before it is relied on.

Source of these findings: the 40 files in `C:\LDPlayer\LDPlayer14\vms\operationRecords\` (39) and `C:\LDPlayer\LDPlayer14\GEternal\` (1), examined read-only on 2026-09-21. The originals are never modified. Tests parse temporary copies and hash the originals before and after.

## File format (text level)
- [VERIFIED] All 40 files are UTF-8 without BOM, CRLF line endings, no trailing newline, JSON indented with 4 spaces, non-ASCII written literally (not `\u` escapes).
- [VERIFIED] No floating point values appear anywhere; all numbers are integers.
- [VERIFIED] `json.dumps(obj, indent=4, ensure_ascii=False)` with CRLF reproduces all 40 files byte-for-byte. The serializer reuses each file's own detected format, so parse -> serialize happens to be byte-exact for these 40 (test `test_byte_exact_round_trip`). This is an observation about LDPlayer's own formatting, NOT a guarantee: see "Round-trip contract" below.
- [ASSUMPTION] LDPlayer itself reads files with other whitespace, but that has not been tested.

## Structure
- [VERIFIED] Top-level keys: `operations` (29 files), `recordInfo` (40 files), `mergedRecords` (11 files). In normal files `operations` comes before `recordInfo`; in merged files `recordInfo` comes before `mergedRecords`.
- [VERIFIED] Merged files (11) have NO top-level `operations`. `mergedRecords` is a list (2 to 963 entries seen; 1,030 entries in total across the 11 files), and every entry is an object with exactly `operations` and `recordInfo`.
- [VERIFIED] `recordInfo` has these 17 keys in every file and every merged entry inspected: `loopType`, `loopTimes`, `circleDuration`, `loopInterval`, `loopDuration`, `accelerateTimes`, `accelerateTimesEx`, `recordName`, `createTime`, `playOnBoot`, `rebootTiming`, `closeTiming`, `resolutionWidth`, `resolutionHeight`, `randomTouch`, `shortkey`, `modifyKey`.
- [VERIFIED] Operation ids seen: `PutMultiTouch` (9,432 in the first survey), `AndroidBack`, `ImeClipboard`, `StopApp`. Fields: `PutMultiTouch` = `timing`, `points`; `AndroidBack` = `timing`; `ImeClipboard` = `timing`, `text`; `StopApp` = `timing`, `app` (a package name).
- [VERIFIED] A point has `id`, `x`, `y`, `state`. State values seen: `1` and `0` only. Every point-carrying `PutMultiTouch` has exactly one point; each is followed 3-8 ms later by a `PutMultiTouch` with an empty `points` array.
- [VERIFIED] `timing` never decreases within any timeline (main or merged entry). Checked by the analysis tool over all 40 files.
- [VERIFIED] Recorded resolution values: 960x540, 1920x1080 (1 file) and 0x0 (the merged files' outer `recordInfo`).
- [VERIFIED] `loopType` is 0 in 38 files and 2 in 2 files.
- [VERIFIED] The parser reports no unknown top-level key, `recordInfo` key, operation id, operation key, point key or merged-entry key in any of the 40 files.

## Internal representation (implemented, `app/macros/`)
- [VERIFIED] `model.py` holds a semantically lossless model: `Macro`, `Operation`, `Point`, `RecordInfo`, `MergedEntry`, `FileFormat`. Unknown keys go into `extras` (top level, points, merged entries) or `params` (operations); source key order and explicit nulls are kept. Tested with synthetic data (`tests/test_macro_parser.py`).
- [VERIFIED] `parser.py` opens files in binary read mode only, rejects duplicate keys, NaN/Infinity, numbers that overflow to infinity (`1e400`), integers too long to convert, over-deep nesting, non-UTF-8 and unexpected structure (raises `MacroFormatError`), and records softer problems as parse issues.
- [VERIFIED] `serializer.py` works in memory only. No function in the project writes `.record` files yet.
- [VERIFIED] `compare.py` gives a strict structural comparison (key order and exact types matter).

## Round-trip contract (Stage 8, C-05)
- [VERIFIED] **Byte-exact round trip is NOT guaranteed.** `serialize(parse_bytes(data))` is a *canonical semantic* round trip: it preserves the complete supported content and the type of every value, and it may normalize the text. Do not use `serialize` to patch a `.record` file in place and expect the untouched parts to keep their bytes.
- [VERIFIED] Preserved: every key and value, the type of every value (`1` stays int, `1.0` float, `true` bool, `null`, strings including non-ASCII and lone-surrogate escapes), unknown keys anywhere, list order, and dictionary key order (kept deterministically from the source; keys added in code follow the canonical schema order, unknown keys in insertion order).
- [VERIFIED] Normalized (may change): indentation and compact/expanded layout, spaces around separators, line endings (the detected one is used for the whole file), trailing newlines and trailing whitespace, empty-list layout, number spelling (`1.50` -> `1.5`, `1E3` -> `1000.0`, `-0` -> `0`), string escapes (`\/` and the `é` form become the literal or an ASCII escape according to the file's detected style; a lone surrogate forces ASCII escapes).
- [VERIFIED] Canonical output is a fixed point: parsing the output and serializing again gives identical bytes.
- [VERIFIED] Non-finite numbers are refused, never written: the parser raises `MacroFormatError` for `NaN`, `Infinity`, `-Infinity` and for any number that overflows to infinity (`1e400`); `serialize` raises `MacroFormatError` for a non-finite number in a macro built in code (it used to emit `Infinity`, which the parser then rejected).
- [VERIFIED] Tests: `tests/test_record_canonical_roundtrip.py` (semantic equivalence ignores dictionary key order and formatting, and is type-strict). What LDPlayer itself accepts beyond its own formatting is still unknown [NEEDS TESTING]; no writer for `.record` files exists.

## Unresolved (reverse-engineering data, do not guess)
- [NEEDS TESTING] Meaning of `state`. Hypothesis H1: `1` = finger down or moving, `0` = finger up. Evidence: state 1 appears more often than state 0 (about 3,000 vs 1,700 in the first survey), which fits swipes being runs of state 1. Not verified.
- [VERIFIED] Consistency check for H1 (not proof of meaning): in all 40 real files every `state 1` for a pointer is eventually followed by a `state 0` for the same pointer, no `state 0` appears without a preceding `state 1`, and no state other than 0 and 1 occurs (`app/macros/validator.py`, run over all files, 0 errors).
- [VERIFIED] Zero-millisecond gaps between consecutive operations are normal in real recordings (3,261 in the 11 merged files), so a short-gap warning is opt-in.
- [NEEDS TESTING] Multi-touch encoding. No real file contains more than one point in an operation, so how two fingers are stored is unknown. The parser handles multiple points structurally (tested with a synthetic file only).
- [NEEDS TESTING] Meaning of `circleDuration`, `accelerateTimes`, `accelerateTimesEx`, `randomTouch`, `shortkey`, `modifyKey`, `rebootTiming`, `closeTiming`. In one file `circleDuration` (4247) is larger than the last operation timing (2189).
- [NEEDS TESTING] Whether `createTime` is local time (it looked one hour off the file's modification time).
- [NEEDS TESTING] How LDPlayer plays a merged file (top-level `recordInfo` vs each entry's `recordInfo`, and how entry timelines are joined). Each entry has its own timeline starting near 0.

## Coordinates (unresolved)
- [VERIFIED] Coordinates are NOT pixels in the recorded resolution. Of 22,333 points, 22,325 lie beyond 960x540, including in files whose `recordInfo` says 960x540.
- [VERIFIED] Across all 22,333 points x ranges 120..18780 and y ranges 339..10620. **No point exceeds 19200 x 10800.**
- [VERIFIED] 97.7% of x and y values are multiples of 20 (and of 10); the only last digits are 0 and 9 (about 1,400 values end in 9).
- [VERIFIED] The 960x540 files and the one 1920x1080 file (12 points, x 9740..17370, y 4850..9880) use the same numeric range.
- [NEEDS TESTING] Hypothesis H2: coordinates live in a fixed logical canvas of 19200 x 10800 (= 960x540 x 20 = 1920x1080 x 10), independent of the instance's screen size, and pixel = coordinate x (screen width / 19200). Supported by the range and step size above, but not proven. It would also explain values ending in 9 as rounding.
- [NEEDS TESTING] Alternative H3: a 20x scale applied only for 960x540 recordings, with the 1920x1080 file using another scale.
- [NEEDS TESTING] Experiment to decide (needs the user to start one instance manually, see docs/ADB.md): on a known-resolution instance, record a short macro tapping known screen positions (corners and centre) with the LDPlayer recorder, parse the copy, and compare the recorded coordinates with the pixel positions. Repeat on an instance with a different resolution (for example 1280x720) to separate H2 from H3.
- [ASSUMPTION] No coordinate transform is applied by this project until an experiment supports one. Nothing is played back.
- [VERIFIED] `app/macros/record_coords.py` keeps `.record` units (`RecordPoint`) a different type from screenshot pixels (`PixelPoint`, generic, in `app/vision/coords.py`); `record_to_pixel` refuses without an explicit `AssumedRecordCanvas`. This does not resolve H2/H3. (Moved out of the vision layer so vision stays target-agnostic.)

## Timing and merge engine (`app/macros/timeline.py`, `validator.py`)
- [VERIFIED] `append(A, B, gap)` places B as one block starting at A's last timing plus `gap`, so A 0..5000 + B 0..1000 gives B 5000..6000 and B's own lead-in is preserved. Also `insert_at`, `repeat`, `offset`, `scale_speed`, `preview`. Tested with synthetic macros and with two real macros (on temporary copies).
- [VERIFIED] Inputs are never mutated; results that would have negative or out-of-order timing, mismatched recorded resolutions, or that involve `mergedRecords` files are refused.
- [ASSUMPTION] Copying `recordInfo` from the first macro is acceptable; `circleDuration` is not recomputed because its meaning is unresolved.
- [NEEDS TESTING] Whether LDPlayer plays a merged output correctly, and how it joins `mergedRecords` entries. Nothing has been played.
- [VERIFIED] `validate()` reports ERROR / WARNING / INFO: out-of-order or negative timing, invalid fields, missing touch-up (depends on H1), touch-up without down, unknown states, negative coordinates, multi-touch (INFO), merged-file and unset-resolution notes.

## Parser rules
- [VERIFIED] The source file is never written: parsing uses `open(path, "rb")`, and the real-file tests assert that each original's hash, size and modification time are unchanged and that a read-only copy parses and serializes.
- [VERIFIED] Unknown fields are preserved (synthetic tests). No real file currently contains any.
