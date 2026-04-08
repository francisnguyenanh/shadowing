# -*- coding: utf-8 -*-
"""
Learning Cycle Service
Manages the 3-day shadowing methodology cycle for a video.
"""

# ── Activity definitions ──────────────────────────────────────────────────────

ACTIVITY_LABELS = {
    'listen_full':          {'vi': 'Nghe toàn video (không nhìn script)',       'icon': '👂', 'duration': 10},
    'listen_chunk':         {'vi': 'Nghe chunk này (không nhìn script)',         'icon': '👂', 'duration': 5},
    'read_transcript':      {'vi': 'Đọc transcript + gạch 10 chunks',           'icon': '📖', 'duration': 5},
    'mark_expressions':     {'vi': 'Đánh dấu 10 focus expressions (Chunk A)',   'icon': '🖊',  'duration': 5},
    'speak_summary':        {'vi': 'Nói lại ý chính video 60 giây',             'icon': '🗣',  'duration': 2},
    'shadow_1.0x':          {'vi': 'Shadowing 1.0× (có ý thức, pitch accent)',  'icon': '🎭', 'duration': 5},
    'shadow_1.25x':         {'vi': 'Shadowing 1.25× (giữ nhịp, không dừng)',   'icon': '⚡', 'duration': 5},
    'free_recall':          {'vi': 'Free Recall – nói lại không nhìn script',   'icon': '🧠', 'duration': 2},
    'free_speech':          {'vi': 'Nói tự do 5 phút dùng chunks vừa học',     'icon': '💬', 'duration': 5},
    'shadow_chunk_b':       {'vi': 'Shadowing Chunk B 1.0×',                    'icon': '🎭', 'duration': 5},
    'free_recall_b':        {'vi': 'Free Recall Chunk B',                       'icon': '🧠', 'duration': 2},
    'speak_combined':       {'vi': 'Nối A+B – nói liền mạch 3 phút',           'icon': '🔗', 'duration': 3},
    'listen_evaluate':      {'vi': 'Nghe lại toàn video – tự đánh giá % hiểu', 'icon': '🎯', 'duration': 10},
    'record_evaluate':      {'vi': 'Nói tự do 2 phút + Ghi âm đánh giá',       'icon': '🎙', 'duration': 5},
}

TIME_OF_DAY_LABELS = {
    'morning':   {'vi': '🌅 Sáng (đi bộ ~40 phút)', 'short': 'Sáng'},
    'afternoon': {'vi': '☀️ Trưa (đi bộ ~30 phút)',  'short': 'Trưa'},
    'evening':   {'vi': '🌙 Tối',                    'short': 'Tối'},
}


def generate_schedule(cycle_id: int, chunks: list) -> list:
    """Generate all session activities for a 3-day cycle (video-level, legacy).

    chunks: list of rows from DB (or dicts with 'id', 'label', etc.)
    Returns list of dicts to be inserted into session_activities.
    """
    chunk_a_id = chunks[0]['id'] if len(chunks) > 0 else None
    chunk_b_id = chunks[1]['id'] if len(chunks) > 1 else None

    raw = [
        # ── Day 1 Evening ────────────────────────────────────────────────────
        (1, 'evening', 'listen_full',      None,        1.0, 1),
        (1, 'evening', 'read_transcript',  None,        1.0, 2),
        (1, 'evening', 'mark_expressions', chunk_a_id,  1.0, 3),
        (1, 'evening', 'speak_summary',    None,        1.0, 4),
        # ── Day 2 Morning ────────────────────────────────────────────────────
        (2, 'morning', 'listen_full',      None,        1.0, 5),
        (2, 'morning', 'shadow_1.0x',      chunk_a_id,  1.0, 6),
        # ── Day 2 Afternoon ──────────────────────────────────────────────────
        (2, 'afternoon', 'shadow_1.0x',    chunk_a_id,  1.0, 7),
        (2, 'afternoon', 'free_recall',    chunk_a_id,  1.0, 8),
        # ── Day 2 Evening ────────────────────────────────────────────────────
        (2, 'evening', 'shadow_1.25x',     chunk_a_id,  1.25, 9),
        (2, 'evening', 'free_speech',      chunk_a_id,  1.0, 10),
        # ── Day 3 Morning ────────────────────────────────────────────────────
        (3, 'morning', 'shadow_chunk_b',   chunk_b_id,  1.0, 11),
        # ── Day 3 Afternoon ──────────────────────────────────────────────────
        (3, 'afternoon', 'free_recall_b',  chunk_b_id,  1.0, 12),
        (3, 'afternoon', 'speak_combined', None,        1.0, 13),
        # ── Day 3 Evening ────────────────────────────────────────────────────
        (3, 'evening', 'listen_evaluate',  None,        1.0, 14),
        (3, 'evening', 'record_evaluate',  None,        1.0, 15),
    ]

    result = []
    for (day, tod, atype, chunk_id, speed, order) in raw:
        meta = ACTIVITY_LABELS.get(atype, {})
        result.append({
            'learning_cycle_id': cycle_id,
            'activity_day':      day,
            'time_of_day':       tod,
            'activity_type':     atype,
            'chunk_id':          chunk_id,
            'speed':             speed,
            'duration_minutes':  meta.get('duration', 5),
            'activity_order':    order,
        })
    return result


def generate_per_chunk_schedule(cycle_id: int, chunk_id: int) -> list:
    """Generate a 14-activity 3-day schedule focused entirely on one chunk.

    Day 1 – Intake & Comprehension (~22 min)
    Day 2 – Pronunciation & Rhythm (~37 min across 3 sessions)
    Day 3 – Integration & Output (~22 min)
    """
    raw = [
        # ── Day 1 Evening ────────────────────────────────────────────────────
        (1, 'evening', 'listen_chunk',    chunk_id, 1.0,  1),
        (1, 'evening', 'read_transcript', chunk_id, 1.0,  2),
        (1, 'evening', 'mark_expressions',chunk_id, 1.0,  3),
        (1, 'evening', 'speak_summary',   chunk_id, 1.0,  4),
        # ── Day 2 Morning ────────────────────────────────────────────────────
        (2, 'morning', 'listen_chunk',    chunk_id, 1.0,  5),
        (2, 'morning', 'shadow_1.0x',     chunk_id, 1.0,  6),
        # ── Day 2 Afternoon ──────────────────────────────────────────────────
        (2, 'afternoon', 'shadow_1.0x',   chunk_id, 1.0,  7),
        (2, 'afternoon', 'free_recall',   chunk_id, 1.0,  8),
        # ── Day 2 Evening ────────────────────────────────────────────────────
        (2, 'evening', 'shadow_1.25x',    chunk_id, 1.25, 9),
        (2, 'evening', 'free_speech',     chunk_id, 1.0,  10),
        # ── Day 3 Morning ────────────────────────────────────────────────────
        (3, 'morning', 'shadow_1.25x',    chunk_id, 1.25, 11),
        (3, 'morning', 'free_recall',     chunk_id, 1.0,  12),
        # ── Day 3 Evening ────────────────────────────────────────────────────
        (3, 'evening', 'listen_evaluate', chunk_id, 1.0,  13),
        (3, 'evening', 'record_evaluate', chunk_id, 1.0,  14),
    ]

    result = []
    for (day, tod, atype, cid, speed, order) in raw:
        meta = ACTIVITY_LABELS.get(atype, {})
        result.append({
            'learning_cycle_id': cycle_id,
            'activity_day':      day,
            'time_of_day':       tod,
            'activity_type':     atype,
            'chunk_id':          cid,
            'speed':             speed,
            'duration_minutes':  meta.get('duration', 5),
            'activity_order':    order,
        })
    return result


    chunk_a_id = chunks[0]['id'] if len(chunks) > 0 else None
    chunk_b_id = chunks[1]['id'] if len(chunks) > 1 else None

    raw = [
        # ── Day 1 Evening ────────────────────────────────────────────────────
        (1, 'evening', 'listen_full',      None,        1.0, 1),
        (1, 'evening', 'read_transcript',  None,        1.0, 2),
        (1, 'evening', 'mark_expressions', chunk_a_id,  1.0, 3),
        (1, 'evening', 'speak_summary',    None,        1.0, 4),
        # ── Day 2 Morning ────────────────────────────────────────────────────
        (2, 'morning', 'listen_full',      None,        1.0, 5),
        (2, 'morning', 'shadow_1.0x',      chunk_a_id,  1.0, 6),
        # ── Day 2 Afternoon ──────────────────────────────────────────────────
        (2, 'afternoon', 'shadow_1.0x',    chunk_a_id,  1.0, 7),
        (2, 'afternoon', 'free_recall',    chunk_a_id,  1.0, 8),
        # ── Day 2 Evening ────────────────────────────────────────────────────
        (2, 'evening', 'shadow_1.25x',     chunk_a_id,  1.25, 9),
        (2, 'evening', 'free_speech',      chunk_a_id,  1.0, 10),
        # ── Day 3 Morning ────────────────────────────────────────────────────
        (3, 'morning', 'shadow_chunk_b',   chunk_b_id,  1.0, 11),
        # ── Day 3 Afternoon ──────────────────────────────────────────────────
        (3, 'afternoon', 'free_recall_b',  chunk_b_id,  1.0, 12),
        (3, 'afternoon', 'speak_combined', None,        1.0, 13),
        # ── Day 3 Evening ────────────────────────────────────────────────────
        (3, 'evening', 'listen_evaluate',  None,        1.0, 14),
        (3, 'evening', 'record_evaluate',  None,        1.0, 15),
    ]

    result = []
    for (day, tod, atype, chunk_id, speed, order) in raw:
        meta = ACTIVITY_LABELS.get(atype, {})
        result.append({
            'learning_cycle_id': cycle_id,
            'activity_day':      day,
            'time_of_day':       tod,
            'activity_type':     atype,
            'chunk_id':          chunk_id,
            'speed':             speed,
            'duration_minutes':  meta.get('duration', 5),
            'activity_order':    order,
        })
    return result


def auto_split_chunks(video_id: int, segments: list, target_minutes: float = 4.0) -> list:
    """Split transcript segments into ~3-5 minute chunks.

    Returns list of dicts: {video_id, chunk_order, label, start_time, end_time}.
    """
    if not segments:
        return []

    target_secs = target_minutes * 60
    chunks = []
    chunk_order = 0
    chunk_start = segments[0]['start_time']
    chunk_label_idx = 0
    labels = ['Chunk A', 'Chunk B', 'Chunk C', 'Chunk D', 'Chunk E']

    prev_end = segments[0]['end_time']
    split_indices = []

    for i, seg in enumerate(segments):
        seg_end = seg['end_time']
        elapsed = seg_end - chunk_start
        if elapsed >= target_secs and i < len(segments) - 1:
            split_indices.append(i)
            chunk_start = seg_end

    # Build chunks from split indices
    boundary_segs = [0] + [idx + 1 for idx in split_indices] + [len(segments)]
    for i in range(len(boundary_segs) - 1):
        s_idx = boundary_segs[i]
        e_idx = boundary_segs[i + 1] - 1
        start = segments[s_idx]['start_time']
        end = segments[e_idx]['end_time']
        label = labels[i] if i < len(labels) else f'Chunk {i + 1}'
        chunks.append({
            'video_id':    video_id,
            'chunk_order': i,
            'label':       label,
            'start_time':  round(start, 3),
            'end_time':    round(end, 3),
        })

    return chunks


def get_cycle_summary(cycle, activities: list) -> dict:
    """Compute progress summary for a cycle row."""
    by_day = {1: [], 2: [], 3: []}
    for a in activities:
        d = a['activity_day'] if isinstance(a, dict) else a[0]
        completed = a['completed'] if isinstance(a, dict) else a[-1]
        by_day.setdefault(d, []).append(completed)

    day_progress = {}
    for d, comps in by_day.items():
        total = len(comps)
        done = sum(1 for c in comps if c)
        day_progress[d] = {'total': total, 'done': done,
                           'pct': round(done / total * 100) if total else 0}
    return day_progress


def get_activity_label(activity_type: str) -> dict:
    return ACTIVITY_LABELS.get(activity_type, {'vi': activity_type, 'icon': '▶', 'duration': 5})


def get_time_label(time_of_day: str) -> dict:
    return TIME_OF_DAY_LABELS.get(time_of_day, {'vi': time_of_day, 'short': time_of_day})
