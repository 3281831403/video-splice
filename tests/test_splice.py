"""Red tests for the video splice exercise.

Every test calls at least one public stub in ``edit.splice``. All PTS values
are in milliseconds, and fixtures use the inline segment shape documented in
``edit/splice.py``.
"""

import pytest

from edit.splice import (
    SpliceError,
    align_to_gop,
    concat_pts,
    detect_gaps,
    insert_transition,
    retime_audio,
    splice_batch,
    split_at,
    validate,
)


def _total_duration(items):
    """Sum the timeline duration of segments and transition descriptors."""
    return sum(item["out"] - item["in"] for item in items)


def test_align_to_gop_snaps_cuts_to_keyframes():
    segments = [
        {
            "in": 1300,
            "out": 4700,
            "gop": 25,
            "keyframes": [0, 1000, 2000, 3000, 4000, 5000],
        }
    ]

    aligned = align_to_gop(segments)

    assert len(aligned) == 1
    assert aligned[0]["in"] in segments[0]["keyframes"]
    assert aligned[0]["out"] in segments[0]["keyframes"]
    assert abs(aligned[0]["in"] - segments[0]["in"]) <= 1000
    assert abs(aligned[0]["out"] - segments[0]["out"]) <= 1000
    assert aligned[0]["out"] > aligned[0]["in"]


def test_concat_pts_closes_33ms_hole():
    segments = [
        {"in": 0, "out": 1000, "gop": 30, "keyframes": [0, 1000]},
        {"in": 1033, "out": 2033, "gop": 30, "keyframes": [1033, 2033]},
    ]

    pts = concat_pts(segments, 30)

    assert segments[1]["in"] - segments[0]["out"] == 33
    assert pts == [1000, 2000]
    assert pts[1] - pts[0] == 1000


def test_retime_audio_has_no_cumulative_drift():
    segments = [
        {
            "in": i * 33,
            "out": (i + 1) * 33,
            "gop": 30,
            "keyframes": [i * 33, (i + 1) * 33],
        }
        for i in range(3000)
    ]

    counts = retime_audio(segments, 44100)

    assert len(counts) == 3000
    assert sum(counts) * 1000 == 3000 * 33 * 44100

    running = 0
    for i, count in enumerate(counts, start=1):
        running += count
        assert abs(running * 1000 - i * 33 * 44100) <= 1000


def test_detect_gaps_skips_invalid_segments():
    segments = [
        {"in": 0, "out": 1000, "gop": 25, "keyframes": [0, 1000]},
        {"in": 5000, "out": 5000, "gop": 25, "keyframes": [5000]},
        {"in": 1100, "out": 2100, "gop": 25, "keyframes": [1100, 2100]},
    ]

    gaps = detect_gaps(segments, 50)

    assert gaps == [(1000, 1100, 100)]


def test_insert_transition_keeps_total_duration():
    segments = [
        {"in": 0, "out": 1000, "gop": 25, "keyframes": [0, 1000]},
        {"in": 1000, "out": 2000, "gop": 25, "keyframes": [1000, 2000]},
    ]

    result = insert_transition(segments, 0, 200)

    assert len(result) == 3
    assert result[1]["kind"] == "transition"
    assert _total_duration(result) == 2000
    assert segments[0]["out"] - result[0]["out"] == 100
    assert result[2]["in"] - segments[1]["in"] == 100
    assert segments[0]["out"] == 1000
    assert segments[1]["in"] == 1000


def test_split_at_uses_nearest_frame_boundary():
    segment = {
        "in": 0,
        "out": 3000,
        "gop": 25,
        "keyframes": [0, 1000, 2000, 3000],
    }

    left, right = split_at(segment, 1510)

    assert left["out"] == right["in"]
    assert abs(left["out"] - 1520) <= 1
    assert left["in"] == 0
    assert right["out"] == 3000


def test_validate_rejects_zero_duration():
    issues = validate(
        [{"in": 1000, "out": 1000, "gop": 25, "keyframes": [1000]}]
    )

    assert issues
    assert any("zero duration" in issue for issue in issues)


def test_validate_rejects_negative_pts_and_out_of_range_keyframes():
    issues = validate(
        [{"in": -100, "out": 1000, "gop": 25, "keyframes": [-100, 0, 1200]}]
    )

    assert issues
    assert any("negative pts" in issue for issue in issues)
    assert any("keyframe out of range" in issue for issue in issues)


def test_splice_batch_rolls_back_on_invalid_segment():
    jobs = [
        {
            "name": "good",
            "segments": [{"in": 0, "out": 1000, "gop": 25, "keyframes": [0, 1000]}],
        },
        {
            "name": "bad",
            "segments": [{"in": 0, "out": 0, "gop": 25, "keyframes": [0]}],
        },
    ]

    result = splice_batch(jobs, 25)

    assert result["ok"] is False
    assert result["jobs"] == []
    assert result["error"]


def test_timebase_29_97_rounds_1000_frames_without_float_drift():
    # 1000 frames at 30000/1001 fps is 33366.66... ms, i.e. 33367 ms.
    segments = [
        {"in": 0, "out": 33366, "gop": 30, "keyframes": [0, 1001]}
    ]

    pts = concat_pts(segments, (30000, 1001))

    assert pts == [33367]


def test_single_segment_concat_keeps_duration():
    segments = [
        {"in": 1000, "out": 2000, "gop": 25, "keyframes": [1000, 2000]}
    ]

    pts = concat_pts(segments, 25)

    assert pts == [1000]


def test_empty_inputs_return_empty():
    assert align_to_gop([]) == []
    assert concat_pts([], 25) == []
    assert retime_audio([], 44100) == []
    assert detect_gaps([], 100) == []
    assert validate([]) == []
    assert splice_batch([], 25) == {"ok": True, "jobs": [], "error": None}


def test_negative_max_gap_raises_splice_error():
    with pytest.raises(SpliceError):
        detect_gaps([], -1)


def test_transition_duration_tolerance_is_one_ms():
    segments = [
        {"in": 0, "out": 1000, "gop": 25, "keyframes": [0, 1000]},
        {"in": 1000, "out": 2000, "gop": 25, "keyframes": [1000, 2000]},
    ]

    result = insert_transition(segments, 0, 33)

    assert abs(_total_duration(result) - 2000) <= 1
    assert abs((result[1]["out"] - result[1]["in"]) - 33) <= 1


def test_batch_success_preserves_job_order():
    jobs = [
        {
            "name": "a",
            "segments": [{"in": 0, "out": 1000, "gop": 25, "keyframes": [0, 1000]}],
        },
        {
            "name": "b",
            "segments": [{"in": 1000, "out": 2000, "gop": 25, "keyframes": [1000, 2000]}],
        },
        {
            "name": "c",
            "segments": [{"in": 2000, "out": 3000, "gop": 25, "keyframes": [2000, 3000]}],
        },
    ]

    result = splice_batch(jobs, 25)

    assert result["ok"] is True
    assert [job["name"] for job in result["jobs"]] == ["a", "b", "c"]
