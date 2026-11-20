"""Splice video segments while keeping PTS, GOP and audio continuity intact.

Segment model
-------------
A segment is a plain dict::

    {
        "in": int,           # inclusive start PTS, milliseconds
        "out": int,          # exclusive end PTS, milliseconds
        "gop": int,          # frames per GOP (the keyframe interval, in frames)
        "keyframes": [int],  # sorted keyframe PTS values, milliseconds
    }

A valid segment has ``in < out``. All public functions use integer
milliseconds for PTS; 1 ms is the accepted rounding tolerance unless a
function says otherwise.

``gop`` is a frame count, not a duration. For a regular segment the source
frame duration is inferred from the keyframe spacing::

    frame_ms = (keyframes[-1] - keyframes[0])
               / ((len(keyframes) - 1) * gop)

For example, ``gop=25`` with keyframes every 1000 ms means 25 fps and a 40 ms
frame. This keeps the inline segment dict small while still supporting
fractional rates such as 29.97 fps.

``timebase`` is a video frame rate: either a positive int in frames/second or
a positive ``(num, den)`` rational pair. A 29.97 fps timebase is
``(30000, 1001)``. Timebase conversions must use integer arithmetic; float
math drifts at NTSC rates and is not acceptable.
"""

from __future__ import annotations

from fractions import Fraction


class SpliceError(Exception):
    """Raised when a splice request is invalid or cannot be completed atomically."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _round_div(a: int, b: int) -> int:
    """Round ``a / b`` to the nearest integer, halves rounding up.

    Uses integer arithmetic only; ``b`` must be positive.
    """
    q, r = divmod(a, b)
    if 2 * r >= b:
        q += 1
    return q


def _parse_timebase(timebase: int | tuple[int, int]) -> tuple[int, int]:
    """Normalise a frames/second timebase to a positive ``(num, den)`` pair."""
    if _is_int(timebase):
        num, den = timebase, 1
    elif (
        isinstance(timebase, (tuple, list))
        and len(timebase) == 2
        and _is_int(timebase[0])
        and _is_int(timebase[1])
    ):
        num, den = timebase
    else:
        raise SpliceError(f"invalid timebase: {timebase!r}")
    if num <= 0 or den <= 0:
        raise SpliceError(f"non-positive timebase: {timebase!r}")
    return num, den


def _frame_ms(segment: dict) -> Fraction:
    """Exact frame duration in milliseconds derived from keyframe spacing."""
    keyframes = segment["keyframes"]
    return Fraction(
        keyframes[-1] - keyframes[0],
        (len(keyframes) - 1) * segment["gop"],
    )


def _check_segment_geometry(segment: dict) -> None:
    in_pts = segment.get("in")
    out_pts = segment.get("out")
    gop = segment.get("gop")
    keyframes = segment.get("keyframes")
    if not _is_int(in_pts) or not _is_int(out_pts):
        raise SpliceError("segment has missing or non-int in/out")
    if out_pts <= in_pts:
        raise SpliceError(f"segment has out <= in ({in_pts}, {out_pts})")
    if not _is_int(gop) or gop <= 0:
        raise SpliceError(f"segment has invalid gop: {gop!r}")
    if (
        not isinstance(keyframes, list)
        or not keyframes
        or any(not _is_int(k) for k in keyframes)
    ):
        raise SpliceError("segment has a missing or invalid keyframes list")
    if any(keyframes[i] >= keyframes[i + 1] for i in range(len(keyframes) - 1)):
        raise SpliceError("segment keyframes are not sorted")


def _nearest_keyframe(keyframes: list[int], pts: int) -> int:
    """Nearest keyframe to ``pts``; ties go to the earlier keyframe."""
    best = keyframes[0]
    for kf in keyframes[1:]:
        if abs(kf - pts) < abs(best - pts):
            best = kf
    return best


def align_to_gop(segments: list[dict]) -> list[dict]:
    """Snap each segment's ``in`` and ``out`` cuts to nearby keyframes.

    For every segment, each cut is moved to the nearest PTS listed in its
    ``keyframes`` array; ties go to the earlier keyframe. A cut may move by at
    most one GOP. The GOP duration in milliseconds is derived from the keyframe
    spacing and the segment's frame-count ``gop``. If no keyframe lies within
    one GOP, that cut is left unchanged.

    Snapping the end cut is skipped when it would make ``out <= in``. Input
    segments are not mutated: a new list of new dicts is returned, preserving
    all other keys. PTS values are milliseconds; fractional frame rates are
    rounded with a 1 ms tolerance.

    Invalid geometry (``out <= in``, ``gop <= 0``, or a missing/unsorted
    ``keyframes`` list) raises :class:`SpliceError`.
    """
    aligned = []
    for segment in segments:
        _check_segment_geometry(segment)
        keyframes = segment["keyframes"]
        if len(keyframes) >= 2:
            gop_ms = _frame_ms(segment) * segment["gop"]
        else:
            gop_ms = None  # spacing unknown: no distance limit

        new_in = segment["in"]
        nearest = _nearest_keyframe(keyframes, segment["in"])
        if gop_ms is None or abs(nearest - segment["in"]) <= gop_ms:
            new_in = nearest

        new_out = segment["out"]
        nearest = _nearest_keyframe(keyframes, segment["out"])
        if gop_ms is None or abs(nearest - segment["out"]) <= gop_ms:
            if nearest > new_in:
                new_out = nearest

        aligned.append({**segment, "in": new_in, "out": new_out})
    return aligned


def concat_pts(segments: list[dict], timebase: int | tuple[int, int]) -> list[int]:
    """Return cumulative end PTS for a gapless concatenation.

    The result contains one integer millisecond value per segment::

        [d0, d0 + d1, ...]

    where ``di = out - in`` is the segment duration. Segment *i* starts at the
    previous returned value (or 0) and ends at ``result[i]``, so no timeline
    hole is inserted between clips. Cumulative boundaries are snapped to the
    nearest frame boundary for ``timebase``. The cumulative frame count is
    computed with integer arithmetic, so 29.97 fps does not accumulate a
    float-drift error.

    The output is non-decreasing and the rounding error at each boundary is at
    most 1 ms (half a frame for fractional rates). Empty input returns ``[]``.
    ``timebase`` is frames/second, either an int or a ``(num, den)`` rational
    pair; PTS values are milliseconds. A non-positive timebase or a segment
    with ``out <= in`` raises :class:`SpliceError`.
    """
    num, den = _parse_timebase(timebase)
    result = []
    cumulative_ms = 0
    for segment in segments:
        in_pts = segment.get("in")
        out_pts = segment.get("out")
        if not _is_int(in_pts) or not _is_int(out_pts) or out_pts <= in_pts:
            raise SpliceError(
                f"segment has invalid in/out: {in_pts!r}, {out_pts!r}"
            )
        cumulative_ms += out_pts - in_pts
        frames = _round_div(cumulative_ms * num, den * 1000)
        result.append(_round_div(frames * den * 1000, num))
    return result


def retime_audio(segments: list[dict], audio_rate: int) -> list[int]:
    """Recompute per-segment audio sample counts from segment durations.

    ``audio_rate`` is in samples/second. The result has one non-negative int
    per segment, and its sum is the sample count for the full concatenation.
    Boundaries are derived from the exact cumulative duration::

        end_i = round(cumulative_ms_i * audio_rate / 1000)
        count_i = end_i - end_{i - 1}

    Every boundary is therefore within 0.5 sample of exact, and cumulative
    error never exceeds 1 sample no matter how many clips are joined. Use
    integer arithmetic only; do not accumulate per-segment rounded counts.

    Empty input returns ``[]`` and a non-positive ``audio_rate`` raises
    :class:`SpliceError`. Durations and PTS values are in milliseconds.
    """
    if not _is_int(audio_rate) or audio_rate <= 0:
        raise SpliceError(f"non-positive audio rate: {audio_rate!r}")
    counts = []
    cumulative_ms = 0
    previous_end = 0
    for segment in segments:
        cumulative_ms += segment["out"] - segment["in"]
        end = _round_div(cumulative_ms * audio_rate, 1000)
        counts.append(end - previous_end)
        previous_end = end
    return counts


def detect_gaps(
    segments: list[dict], max_gap_ms: int
) -> list[tuple[int, int, int]]:
    """Find timeline gaps strictly larger than ``max_gap_ms``.

    Invalid segments (negative PTS or ``out <= in``) are ignored completely
    rather than becoming zero-length anchors. The remaining segments are
    considered in ascending ``in`` order. Each adjacent pair whose separation
    ``next_in - prev_out`` is strictly greater than ``max_gap_ms`` produces a
    tuple::

        (prev_out_ms, next_in_ms, gap_ms)

    Overlaps and touching boundaries are not gaps. Ignoring invalid clips
    means a large gap spanning an invalid segment is still reported.

    ``max_gap_ms`` is in milliseconds. A negative threshold raises
    :class:`SpliceError` before any segment is inspected. Empty input returns
    ``[]``.
    """
    if not _is_int(max_gap_ms) or max_gap_ms < 0:
        raise SpliceError(f"negative max_gap_ms: {max_gap_ms!r}")
    valid = [
        s
        for s in segments
        if _is_int(s.get("in"))
        and _is_int(s.get("out"))
        and s["in"] >= 0
        and s["out"] >= 0
        and s["out"] > s["in"]
    ]
    valid.sort(key=lambda s: s["in"])
    gaps = []
    for prev, nxt in zip(valid, valid[1:]):
        gap = nxt["in"] - prev["out"]
        if gap > max_gap_ms:
            gaps.append((prev["out"], nxt["in"], gap))
    return gaps


def insert_transition(segments: list[dict], index: int, duration_ms: int) -> list[dict]:
    """Insert a transition between ``segments[index]`` and ``segments[index + 1]``.

    Both neighbouring clips give up half of ``duration_ms``: the left clip's
    ``out`` moves earlier by about half, and the right clip's ``in`` moves later
    by the other half. A transition descriptor of exactly ``duration_ms`` is
    placed between them, so the total timeline duration is unchanged (at most
    1 ms error when the duration is odd).

    The returned list has the form::

        [..., left, {"kind": "transition", "in": ..., "out": ...,
                     "duration_ms": duration_ms}, right, ...]

    ``index`` is the zero-based index of the left segment. The two clips must
    touch at that boundary (``left["out"] == right["in"]``); otherwise the
    transition cannot preserve duration. Input is not mutated. PTS values are
    milliseconds, and odd halves are rounded within 1 ms.

    An invalid index, non-positive duration, non-touching boundary, or clip too
    short to give up its half raises :class:`SpliceError`.
    """
    if not _is_int(index) or not 0 <= index < len(segments) - 1:
        raise SpliceError(f"invalid transition index: {index!r}")
    if not _is_int(duration_ms) or duration_ms <= 0:
        raise SpliceError(f"non-positive transition duration: {duration_ms!r}")
    left = segments[index]
    right = segments[index + 1]
    if left["out"] != right["in"]:
        raise SpliceError(
            f"segments do not touch: {left['out']} != {right['in']}"
        )
    left_half = duration_ms // 2
    right_half = duration_ms - left_half
    if left["out"] - left["in"] <= left_half:
        raise SpliceError("left clip too short to give up its half")
    if right["out"] - right["in"] <= right_half:
        raise SpliceError("right clip too short to give up its half")

    new_left = {**left, "out": left["out"] - left_half}
    new_right = {**right, "in": right["in"] + right_half}
    transition = {
        "kind": "transition",
        "in": new_left["out"],
        "out": new_right["in"],
        "duration_ms": duration_ms,
    }
    return (
        list(segments[:index])
        + [new_left, transition, new_right]
        + list(segments[index + 2 :])
    )


def split_at(segment: dict, pts: int) -> tuple[dict, dict]:
    """Split ``segment`` at ``pts``, snapping the cut to the nearest frame.

    PTS values are integer milliseconds. The frame duration is inferred from
    the keyframe spacing and ``gop`` (see the module docstring). The cut is the
    nearest frame boundary to ``pts``, with ties going to the later frame, and
    is clamped so both halves stay non-empty. The left result ends at the cut
    and the right result starts at the cut, so the boundary error is at most
    1 ms for fractional frame rates.

    The returned ``(left, right)`` are new dicts; ``keyframes`` is partitioned
    at the cut and the other fields are preserved. The function requires
    ``in < pts < out``, at least two keyframes, and ``gop > 0``; otherwise it
    raises :class:`SpliceError`.
    """
    in_pts = segment.get("in")
    out_pts = segment.get("out")
    gop = segment.get("gop")
    keyframes = segment.get("keyframes")
    if not _is_int(in_pts) or not _is_int(out_pts) or not in_pts < pts < out_pts:
        raise SpliceError(f"split point {pts!r} not inside segment")
    if not isinstance(keyframes, list) or len(keyframes) < 2:
        raise SpliceError("split requires at least two keyframes")
    if not _is_int(gop) or gop <= 0:
        raise SpliceError(f"invalid gop: {gop!r}")

    frame_ms = Fraction(
        keyframes[-1] - keyframes[0], (len(keyframes) - 1) * gop
    )
    if frame_ms <= 0:
        raise SpliceError("non-positive frame duration from keyframes")

    # Nearest frame index, ties going to the later frame (round half up).
    offset = Fraction(pts - keyframes[0]) / frame_ms
    frame_index = (2 * offset.numerator + offset.denominator) // (
        2 * offset.denominator
    )
    cut_fraction = keyframes[0] + frame_index * frame_ms
    cut = (2 * cut_fraction.numerator + cut_fraction.denominator) // (
        2 * cut_fraction.denominator
    )
    # Clamp so both halves stay non-empty.
    cut = max(cut, in_pts + 1)
    cut = min(cut, out_pts - 1)

    left = {
        **segment,
        "out": cut,
        "keyframes": [k for k in keyframes if k < cut],
    }
    right = {
        **segment,
        "in": cut,
        "keyframes": [k for k in keyframes if k >= cut],
    }
    return left, right


def validate(segments: list[dict]) -> list[str]:
    """Return human-readable problems with ``segments``; ``[]`` means valid.

    Per segment the checks cover:

    * missing or non-int ``in``, ``out``, ``gop``, ``keyframes``;
    * negative PTS (``in < 0`` or ``out < 0``);
    * ``out == in`` reports ``"zero duration"``, while ``out < in`` reports
      ``"negative duration"``;
    * non-positive ``gop``;
    * keyframes that are not strictly ascending;
    * a keyframe outside ``[in, out]``;
    * segments out of order by ``in`` and overlapping segments
      (``in < previous out``).

    Each message starts with ``"segment <i>: "`` and contains a stable keyword
    such as ``"negative pts"``, ``"zero duration"``, ``"negative duration"``,
    ``"invalid gop"``, ``"keyframes not sorted"``, ``"keyframe out of range"``,
    ``"out of order"``, or ``"overlap"``. PTS values are milliseconds and the
    list is empty for empty input.
    """
    issues = []
    prev_in = prev_out = None
    for i, segment in enumerate(segments):
        prefix = f"segment {i}: "
        if not isinstance(segment, dict):
            issues.append(prefix + "not a segment dict")
            continue
        in_pts = segment.get("in")
        out_pts = segment.get("out")
        gop = segment.get("gop")
        keyframes = segment.get("keyframes")

        pts_ok = _is_int(in_pts) and _is_int(out_pts)
        if not pts_ok:
            issues.append(prefix + "missing or non-int in/out")

        if not _is_int(gop) or gop <= 0:
            issues.append(prefix + "invalid gop")

        kf_ok = isinstance(keyframes, list) and all(
            _is_int(k) for k in keyframes
        )
        if not kf_ok:
            issues.append(prefix + "missing or non-int keyframes")

        if pts_ok:
            if in_pts < 0 or out_pts < 0:
                issues.append(prefix + "negative pts")
            if out_pts == in_pts:
                issues.append(prefix + "zero duration")
            elif out_pts < in_pts:
                issues.append(prefix + "negative duration")

        if kf_ok:
            if any(
                keyframes[j] >= keyframes[j + 1]
                for j in range(len(keyframes) - 1)
            ):
                issues.append(prefix + "keyframes not sorted")
            if pts_ok and any(k < in_pts or k > out_pts for k in keyframes):
                issues.append(prefix + "keyframe out of range")

        if pts_ok:
            if prev_in is not None and in_pts < prev_in:
                issues.append(prefix + "out of order")
            if prev_out is not None and in_pts < prev_out:
                issues.append(prefix + "overlap")
            prev_in, prev_out = in_pts, out_pts
    return issues


def splice_batch(
    jobs: list[dict], timebase: int | tuple[int, int]
) -> dict:
    """Splice a batch of jobs atomically, preserving the input order.

    Each job has the shape ``{"name": str, "segments": [segment, ...]}``. Jobs
    are processed in order. For each job the segments are validated, cuts are
    aligned to GOPs, and cumulative PTS values are computed with ``timebase``.
    A successful call returns::

        {"ok": True, "jobs": [job_result, ...], "error": None}

    where every ``job_result`` contains ``name``, ``segments``, ``pts``, and
    ``duration_ms``.

    If any segment in any job is invalid, the whole batch rolls back and no
    partial job list is returned::

        {"ok": False, "jobs": [], "error": "<reason>"}

    A :class:`SpliceError` from a lower-level step is handled the same way.
    Empty ``jobs`` returns ``{"ok": True, "jobs": [], "error": None}``.
    ``timebase`` uses the same frames/second form as :func:`concat_pts`; PTS is
    in milliseconds and the 1 ms tolerance applies.
    """
    results = []
    try:
        for job in jobs:
            segments = job["segments"]
            problems = validate(segments)
            if problems:
                return {
                    "ok": False,
                    "jobs": [],
                    "error": (
                        f"job {job.get('name')!r}: " + "; ".join(problems)
                    ),
                }
            aligned = align_to_gop(segments)
            pts = concat_pts(aligned, timebase)
            results.append(
                {
                    "name": job["name"],
                    "segments": aligned,
                    "pts": pts,
                    "duration_ms": pts[-1] if pts else 0,
                }
            )
    except SpliceError as exc:
        return {"ok": False, "jobs": [], "error": str(exc)}
    return {"ok": True, "jobs": results, "error": None}
