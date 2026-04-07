# -*- coding: utf-8 -*-
import re
import json
import os
import datetime
import unicodedata
from flask import Flask, render_template, request, redirect, url_for, flash, g, jsonify
from werkzeug.utils import secure_filename
import config
from database import get_db, init_db, close_db
from prompt_builder import build_prompt, build_continuation_prompt, build_chunked_prompts, build_chunk_prompt_with_transcript, build_srt_translation_prompt
from transcript_parser import parse_transcript, parse_and_merge_transcripts, save_transcript, apply_timeline_offset, parse_srt
from services.learning_cycle_service import (
    generate_schedule, generate_per_chunk_schedule, auto_split_chunks,
    get_cycle_summary, get_activity_label, get_time_label, ACTIVITY_LABELS, TIME_OF_DAY_LABELS
)

app = Flask(__name__)
app.config.from_object(config)
app.secret_key = config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = config.MAX_CONTENT_LENGTH


# ── DB lifecycle ──────────────────────────────────────────────────────────────

@app.teardown_appcontext
def teardown_db(exception):
    close_db(exception)


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from all common URL formats."""
    patterns = [
        r'(?:youtube\.com/watch\?.*v=)([A-Za-z0-9_-]{11})',
        r'(?:youtu\.be/)([A-Za-z0-9_-]{11})',
        r'(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})',
        r'(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def fetch_video_info(youtube_url: str) -> dict:
    """Try yt-dlp to get title and duration. Falls back gracefully."""
    try:
        import yt_dlp
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            return {
                'title': info.get('title', youtube_url),
                'duration': int(info.get('duration', 0) or 0),
            }
    except Exception:
        return {'title': youtube_url, 'duration': 0}


def download_audio_task(youtube_url: str, video_id: str) -> str:
    """Download audio from YouTube as MP3 192kbps.

    Output filename: static/audio/<Sanitized Title> [<video_id>].mp3
    Returns the path relative to static/ (e.g. 'audio/Title_[ID].mp3') so it can
    be stored in the DB and served via url_for('static', filename=...).
    Raises on failure so the caller can flash the error.
    """
    import os
    import yt_dlp

    audio_dir = os.path.join(app.static_folder, 'audio')
    os.makedirs(audio_dir, exist_ok=True)

    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'outtmpl': os.path.join(audio_dir, '%(title)s [%(id)s].%(ext)s'),
        'restrictfilenames': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=True)
        # prepare_filename reflects the same sanitization as restrictfilenames;
        # the postprocessor swaps the container ext to .mp3.
        raw_path = ydl.prepare_filename(info)
        mp3_path = os.path.splitext(raw_path)[0] + '.mp3'

    # Return path relative to static/ for url_for('static', filename=...)
    return 'audio/' + os.path.basename(mp3_path)


def fetch_youtube_transcript(video_id: str, youtube_url: str, language: str) -> list | None:
    """Fetch transcript from YouTube. Try youtube-transcript-api first, then yt-dlp subtitles.

    Returns list of {"start": float, "duration": float, "text": str} or None.
    """
    # Approach 1: youtube-transcript-api
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        lang_codes = (['ja', 'ja-orig'] if language == 'ja' else ['en', 'en-orig'])
        try:
            segs = YouTubeTranscriptApi.get_transcript(video_id, languages=lang_codes)
        except Exception:
            segs = YouTubeTranscriptApi.get_transcript(video_id)  # any available language
        return [{'start': s['start'], 'duration': s['duration'], 'text': s['text']} for s in segs]
    except Exception:
        pass

    # Approach 3 (fallback): yt-dlp subtitle download to temp dir
    try:
        import yt_dlp
        import os
        import tempfile
        lang_codes = (['ja', 'en'] if language == 'ja' else ['en', 'ja'])
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                'skip_download': True,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': lang_codes,
                'subtitlesformat': 'json3',
                'outtmpl': os.path.join(tmpdir, '%(id)s'),
                'quiet': True,
                'no_warnings': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([youtube_url])
            for fname in sorted(os.listdir(tmpdir)):
                if fname.endswith('.json3'):
                    fpath = os.path.join(tmpdir, fname)
                    with open(fpath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    segments = []
                    for event in data.get('events', []):
                        start_ms = event.get('tStartMs', 0)
                        dur_ms = event.get('dDurationMs', 2000)
                        text = ''.join(s.get('utf8', '') for s in event.get('segs', []))
                        text = text.strip()
                        if text and text != '\n':
                            segments.append({
                                'start': start_ms / 1000,
                                'duration': dur_ms / 1000,
                                'text': text,
                            })
                    if segments:
                        return segments
    except Exception:
        pass

    return None

@app.route('/')
def index():
    try:
        db = get_db()
        playlist_id = request.args.get('playlist', type=int)
        playlists = db.execute(
            '''SELECT p.*, COUNT(pv.id) as video_count
               FROM playlists p
               LEFT JOIN playlist_videos pv ON pv.playlist_id = p.id
               GROUP BY p.id
               ORDER BY p.name COLLATE NOCASE'''
        ).fetchall()
        if playlist_id:
            current_playlist = db.execute(
                'SELECT * FROM playlists WHERE id = ?', (playlist_id,)
            ).fetchone()
            if current_playlist is None:
                return redirect(url_for('index'))
            videos = db.execute(
                '''SELECT v.*, COUNT(s.id) as segment_count
                   FROM videos v
                   JOIN playlist_videos pv ON pv.video_id = v.id
                   LEFT JOIN segments s ON s.video_id = v.id
                   WHERE pv.playlist_id = ?
                   GROUP BY v.id
                   ORDER BY pv.added_at DESC''',
                (playlist_id,)
            ).fetchall()
        else:
            current_playlist = None
            videos = db.execute(
                '''SELECT v.*, COUNT(s.id) as segment_count
                   FROM videos v
                   LEFT JOIN segments s ON s.video_id = v.id
                   GROUP BY v.id
                   ORDER BY v.created_at DESC'''
            ).fetchall()
    except Exception as e:
        flash(f'Lỗi khi tải danh sách video: {str(e)}', 'error')
        videos = []
        playlists = []
        current_playlist = None
        playlist_id = None
    return render_template('index.html', videos=videos, playlists=playlists,
                           current_playlist=current_playlist, playlist_id=playlist_id)


@app.route('/add', methods=['GET'])
def add_video_form():
    return render_template('add_video.html')


@app.route('/add', methods=['POST'])
def add_video():
    youtube_url = request.form.get('youtube_url', '').strip()
    language = request.form.get('language', 'ja').strip()

    if not youtube_url:
        flash('Vui lòng nhập URL YouTube.', 'error')
        return render_template('add_video.html')

    video_id = extract_video_id(youtube_url)
    if not video_id:
        flash('URL YouTube không hợp lệ. Hãy kiểm tra lại định dạng URL.', 'error')
        return render_template('add_video.html')

    if language not in ('ja', 'en'):
        language = 'ja'

    try:
        info = fetch_video_info(youtube_url)
        db = get_db()
        cursor = db.execute(
            'INSERT INTO videos (youtube_url, video_id, title, language, duration) VALUES (?, ?, ?, ?, ?)',
            (youtube_url, video_id, info['title'], language, info['duration'])
        )
        db.commit()
        video_db_id = cursor.lastrowid
        # Import SRT file if provided
        srt_file = request.files.get('srt_file')
        if srt_file and srt_file.filename:
            try:
                srt_text = srt_file.read().decode('utf-8', errors='replace')
                transcript_data = parse_srt(srt_text)
                db.execute('UPDATE videos SET transcript_raw = ? WHERE id = ?',
                           (json.dumps(transcript_data, ensure_ascii=False), video_db_id))
                db.commit()
                flash(f'Đã import {len(transcript_data)} dòng từ file SRT.', 'success')
            except Exception as srt_err:
                flash(f'Lỗi khi đọc file SRT: {str(srt_err)}', 'error')
        # Download audio (MP3) — non-blocking on error
        """
        try:
            audio_file = download_audio_task(youtube_url, video_id)
            db.execute('UPDATE videos SET audio_path = ? WHERE id = ?', (audio_file, video_db_id))
            db.commit()
            flash('Video đã được thêm và audio đã tải xong!', 'success')
        except Exception as dl_err:
            flash('Video đã thêm nhưng tải audio thất bại: ' + str(dl_err), 'error')
        """
        return redirect(url_for('show_prompt', video_db_id=video_db_id))
    except Exception as e:
        flash(f'Lỗi khi thêm video: {str(e)}', 'error')
        return render_template('add_video.html')


@app.route('/prompt/<int:video_db_id>')
def show_prompt(video_db_id):
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            flash('Không tìm thấy video.', 'error')
            return redirect(url_for('index'))
        transcript_data = json.loads(video['transcript_raw']) if video['transcript_raw'] else None
        chunks = build_chunked_prompts(
            video['youtube_url'], video['language'], video['duration'] or 0,
            transcript_data=transcript_data
        )
        transcript_count = len(transcript_data) if transcript_data else 0
        srt_prompt = build_srt_translation_prompt(
            video['language'], video['title'] or '', transcript_count
        ) if transcript_count > 0 else ''
        return render_template('paste_transcript.html', video=video, chunks=chunks,
                               transcript_count=transcript_count,
                               srt_translation_prompt=srt_prompt, error=None)
    except Exception as e:
        flash(f'Lỗi: {str(e)}', 'error')
        return redirect(url_for('index'))


@app.route('/api/continuation_prompt/<int:video_db_id>', methods=['POST'])
def api_continuation_prompt(video_db_id):
    """Return a continuation prompt given last segment ID and end time."""
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            return jsonify({'error': 'Không tìm thấy video.'}), 404
        data = request.get_json(silent=True) or {}
        last_id = int(data.get('last_id', 0))
        last_end = float(data.get('last_end', 0.0))
        prompt_text = build_continuation_prompt(video['youtube_url'], video['language'], last_id, last_end)
        return jsonify({'prompt': prompt_text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/transcript/<int:video_db_id>', methods=['POST'])
def save_transcript_route(video_db_id):
    db = get_db()
    video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
    if video is None:
        flash('Không tìm thấy video.', 'error')
        return redirect(url_for('index'))

    # Support multiple JSON parts submitted as repeated fields
    raw_jsons = [t for t in request.form.getlist('transcript_json') if t.strip()]
    transcript_data = json.loads(video['transcript_raw']) if video['transcript_raw'] else None
    chunks = build_chunked_prompts(video['youtube_url'], video['language'], video['duration'] or 0,
                                   transcript_data=transcript_data)
    transcript_count = len(transcript_data) if transcript_data else 0
    srt_prompt = build_srt_translation_prompt(
        video['language'], video['title'] or '', transcript_count
    ) if transcript_count > 0 else ''

    if not raw_jsons:
        return render_template('paste_transcript.html', video=video, chunks=chunks,
                               transcript_count=transcript_count,
                               srt_translation_prompt=srt_prompt,
                               error='Vui lòng nhập ít nhất một đoạn JSON.')
    try:
        parsed = parse_and_merge_transcripts(raw_jsons)
        save_transcript(video_db_id, parsed, db)
        seg_count = len(parsed.get('segments', []))
        flash(f'Đã lưu {seg_count} segments thành công! ({len(raw_jsons)} phần JSON)', 'success')
        return redirect(url_for('player', video_db_id=video_db_id))
    except ValueError as e:
        return render_template(
            'paste_transcript.html',
            video=video,
            chunks=chunks,
            transcript_count=transcript_count,
            srt_translation_prompt=srt_prompt,
            error=str(e),
            previous_inputs=raw_jsons
        )
    except Exception as e:
        return render_template(
            'paste_transcript.html',
            video=video,
            chunks=chunks,
            transcript_count=transcript_count,
            srt_translation_prompt=srt_prompt,
            error=f'Lỗi không xác định: {str(e)}',
            previous_inputs=raw_jsons
        )


@app.route('/player/<int:video_db_id>')
def player(video_db_id):
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            flash('Không tìm thấy video.', 'error')
            return redirect(url_for('index'))
        segments = db.execute(
            'SELECT * FROM segments WHERE video_id = ? ORDER BY segment_order',
            (video_db_id,)
        ).fetchall()
        segments_list = [dict(s) for s in segments]
        chunks = [dict(c) for c in db.execute(
            'SELECT id, label, start_time, end_time, chunk_order FROM chunks WHERE video_id = ? ORDER BY chunk_order',
            (video_db_id,)
        ).fetchall()]
        return render_template('player.html', video=video, segments=segments_list,
                               segments_json=json.dumps(segments_list),
                               chunks=chunks)
    except Exception as e:
        flash(f'Lỗi khi tải player: {str(e)}', 'error')
        return redirect(url_for('index'))


@app.route('/api/segments/<int:video_db_id>')
def api_segments(video_db_id):
    try:
        db = get_db()
        segments = db.execute(
            'SELECT id, start_time as start, end_time as end, text, translation, bookmarked '
            'FROM segments WHERE video_id = ? ORDER BY segment_order',
            (video_db_id,)
        ).fetchall()
        return jsonify([dict(s) for s in segments])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/segment/<int:segment_id>', methods=['PATCH'])
def api_update_segment_time(segment_id):
    """Update segment timestamps. Supports cascade on start_time change:
    - prev segment's end_time becomes new start_time
    - all subsequent segments shift by the same delta
    """
    try:
        data = request.get_json(silent=True) or {}
        db = get_db()
        seg = db.execute('SELECT * FROM segments WHERE id = ?', (segment_id,)).fetchone()
        if seg is None:
            return jsonify({'success': False, 'error': 'Không tìm thấy segment.'}), 404

        cascade = bool(data.get('cascade', False))
        has_start = 'start_time' in data
        has_end = 'end_time' in data
        updated = []

        # Handle text content fields (text, translation, bookmarked)
        content_fields = {}
        for field in ('text', 'translation'):
            if field in data:
                content_fields[field] = str(data[field])
        if 'bookmarked' in data:
            content_fields['bookmarked'] = int(bool(data['bookmarked']))
        if content_fields:
            set_clause = ', '.join(f'{k} = ?' for k in content_fields)
            db.execute(f'UPDATE segments SET {set_clause} WHERE id = ?',
                       (*content_fields.values(), segment_id))

        if has_start:
            new_start = float(data['start_time'])
            old_start = seg['start_time']
            old_end = seg['end_time']
            # Preserve duration when only start is provided
            new_end = float(data['end_time']) if has_end else old_end + (new_start - old_start)
            if new_start < 0 or new_end <= new_start:
                return jsonify({'success': False, 'error': 'Timestamp không hợp lệ (start >= 0, start < end).'}), 400

            delta = new_start - old_start

            # Update previous segment's end → new start (snap gap closed)
            if cascade and abs(delta) > 0.001:
                prev = db.execute(
                    'SELECT * FROM segments WHERE video_id = ? AND segment_order < ? '
                    'ORDER BY segment_order DESC LIMIT 1',
                    (seg['video_id'], seg['segment_order'])
                ).fetchone()
                if prev:
                    db.execute('UPDATE segments SET end_time = ? WHERE id = ?',
                               (new_start, prev['id']))
                    updated.append({'id': prev['id'], 'start_time': prev['start_time'],
                                    'end_time': new_start})

            # Update this segment
            db.execute('UPDATE segments SET start_time = ?, end_time = ? WHERE id = ?',
                       (new_start, new_end, segment_id))
            updated.append({'id': segment_id, 'start_time': new_start, 'end_time': new_end})

            # Shift all subsequent segments by delta
            if cascade and abs(delta) > 0.001:
                subsequents = db.execute(
                    'SELECT * FROM segments WHERE video_id = ? AND segment_order > ? '
                    'ORDER BY segment_order',
                    (seg['video_id'], seg['segment_order'])
                ).fetchall()
                for s in subsequents:
                    ns = max(0.0, round(s['start_time'] + delta, 3))
                    ne = max(0.0, round(s['end_time'] + delta, 3))
                    db.execute('UPDATE segments SET start_time = ?, end_time = ? WHERE id = ?',
                               (ns, ne, s['id']))
                    updated.append({'id': s['id'], 'start_time': ns, 'end_time': ne})

        elif has_end:
            new_end = float(data['end_time'])
            if new_end <= seg['start_time']:
                return jsonify({'success': False, 'error': 'end_time phải > start_time.'}), 400
            db.execute('UPDATE segments SET end_time = ? WHERE id = ?', (new_end, segment_id))
            updated.append({'id': segment_id, 'start_time': seg['start_time'], 'end_time': new_end})
        else:
            if not content_fields:
                return jsonify({'success': False, 'error': 'Thiếu trường cần cập nhật.'}), 400

        db.commit()
        return jsonify({'success': True, 'cascade': cascade, 'updated': updated})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/video/<int:video_db_id>', methods=['DELETE'])
def api_delete_video(video_db_id):
    try:
        db = get_db()
        db.execute('DELETE FROM segments WHERE video_id = ?', (video_db_id,))
        db.execute('DELETE FROM playlist_videos WHERE video_id = ?', (video_db_id,))
        db.execute('DELETE FROM videos WHERE id = ?', (video_db_id,))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/transcript_json/<int:video_db_id>')
def api_download_transcript_json(video_db_id):
    """Download transcript as JSON with empty translations — for attaching to external AI."""
    from flask import Response
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            return jsonify({'error': 'Không tìm thấy video.'}), 404
        if not video['transcript_raw']:
            return jsonify({'error': 'Video này chưa có transcript.'}), 404

        transcript_data = json.loads(video['transcript_raw'])
        segments = []
        for i, seg in enumerate(transcript_data, start=1):
            segments.append({
                'id': i,
                'start': seg['start'],
                'end': round(seg['start'] + seg.get('duration', 2.0), 3),
                'text': seg['text'],
                'translation': None,
            })

        output = {
            'title': video['title'] or video['video_id'],
            'language': video['language'],
            'segments': segments,
        }
        content = json.dumps(output, ensure_ascii=False, indent=2)
        
        import unicodedata
        title_safe = re.sub(r'[^\w\s-]', '', video['title'] or video['video_id'])[:50].strip()
        # Loại bỏ Unicode, chỉ giữ ASCII
        title_ascii = unicodedata.normalize('NFKD', title_safe).encode('ascii', 'ignore').decode('ascii')
        filename = f'transcript_{title_ascii}_{video["video_id"]}.json'
        
        return Response(
            content,
            mimetype='application/json; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload_srt/<int:video_db_id>', methods=['POST'])
def api_upload_srt(video_db_id):
    """Upload or replace SRT transcript for an existing video."""
    db = get_db()
    video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
    if video is None:
        flash('Không tìm thấy video.', 'error')
        return redirect(url_for('index'))

    srt_file = request.files.get('srt_file')
    if not srt_file or not srt_file.filename:
        flash('Vui lòng chọn file .srt.', 'error')
        return redirect(url_for('show_prompt', video_db_id=video_db_id))

    try:
        srt_text = srt_file.read().decode('utf-8', errors='replace')
        transcript_data = parse_srt(srt_text)
        db.execute('UPDATE videos SET transcript_raw = ? WHERE id = ?',
                   (json.dumps(transcript_data, ensure_ascii=False), video_db_id))
        db.commit()
        flash(f'Đã import {len(transcript_data)} dòng transcript từ file SRT.', 'success')
    except Exception as e:
        flash(f'Lỗi khi đọc file SRT: {str(e)}', 'error')

    return redirect(url_for('show_prompt', video_db_id=video_db_id))


@app.route('/api/transcript_file/<int:video_db_id>')
def api_download_transcript_file(video_db_id):
    """Download raw YouTube transcript as a Markdown file for attaching to external AI."""
    try:
        from flask import Response
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            return jsonify({'error': 'Không tìm thấy video.'}), 404
        if not video['transcript_raw']:
            return jsonify({'error': 'Video này không có transcript từ YouTube.'}), 404

        transcript_data = json.loads(video['transcript_raw'])
        lines = [
            f'# Transcript: {video["title"] or video["video_id"]}',
            f'URL: {video["youtube_url"]}',
            f'Language: {video["language"]}',
            '',
            '## Segments',
            '',
        ]
        for seg in transcript_data:
            start = seg['start']
            end = start + seg.get('duration', 2.0)
            m_s, s_s = int(start // 60), int(start % 60)
            m_e, s_e = int(end // 60), int(end % 60)
            lines.append(f'[{m_s:02d}:{s_s:02d} → {m_e:02d}:{s_e:02d}] {seg["text"]}')

        content = '\n'.join(lines)
        title_safe = re.sub(r'[^\w\s-]', '', video['title'] or video['video_id'])[:50].strip()
        filename = f'transcript_{title_safe}_{video["video_id"]}.md'
        return Response(
            content,
            mimetype='text/markdown; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Playlist API ──────────────────────────────────────────────────────────────

@app.route('/api/playlist', methods=['POST'])
def api_create_playlist():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Tên playlist không được để trống.'}), 400
    try:
        db = get_db()
        cursor = db.execute('INSERT INTO playlists (name) VALUES (?)', (name,))
        db.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid, 'name': name})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/playlist/<int:playlist_id>', methods=['DELETE'])
def api_delete_playlist(playlist_id):
    try:
        db = get_db()
        db.execute('DELETE FROM playlist_videos WHERE playlist_id = ?', (playlist_id,))
        db.execute('DELETE FROM playlists WHERE id = ?', (playlist_id,))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/playlist/<int:playlist_id>/video', methods=['POST'])
def api_playlist_add_video(playlist_id):
    data = request.get_json(silent=True) or {}
    video_id = data.get('video_id')
    if not video_id:
        return jsonify({'success': False, 'error': 'Thiếu video_id.'}), 400
    try:
        db = get_db()
        # Verify both records exist
        if not db.execute('SELECT 1 FROM playlists WHERE id = ?', (playlist_id,)).fetchone():
            return jsonify({'success': False, 'error': 'Playlist không tồn tại.'}), 404
        if not db.execute('SELECT 1 FROM videos WHERE id = ?', (video_id,)).fetchone():
            return jsonify({'success': False, 'error': 'Video không tồn tại.'}), 404
        db.execute(
            'INSERT OR IGNORE INTO playlist_videos (playlist_id, video_id) VALUES (?, ?)',
            (playlist_id, video_id)
        )
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/playlist/<int:playlist_id>/video/<int:video_id>', methods=['DELETE'])
def api_playlist_remove_video(playlist_id, video_id):
    try:
        db = get_db()
        db.execute(
            'DELETE FROM playlist_videos WHERE playlist_id = ? AND video_id = ?',
            (playlist_id, video_id)
        )
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/video/<int:video_db_id>/playlists')
def api_video_playlists(video_db_id):
    try:
        db = get_db()
        all_playlists = db.execute(
            'SELECT * FROM playlists ORDER BY name COLLATE NOCASE'
        ).fetchall()
        member_rows = db.execute(
            'SELECT playlist_id FROM playlist_videos WHERE video_id = ?', (video_db_id,)
        ).fetchall()
        member_ids = {r['playlist_id'] for r in member_rows}
        result = [
            {'id': p['id'], 'name': p['name'], 'member': p['id'] in member_ids}
            for p in all_playlists
        ]
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/timeline_offset/<int:video_db_id>', methods=['POST'])
def api_timeline_offset(video_db_id):
    """Permanently apply a time offset (seconds) to all segments of a video."""
    try:
        data = request.get_json(silent=True)
        if not data or 'offset' not in data:
            return jsonify({'success': False, 'error': 'Thiếu tham số offset.'}), 400
        offset = float(data['offset'])
        if offset == 0:
            return jsonify({'success': True, 'rows_updated': 0, 'message': 'Offset = 0, không thay đổi.'})
        db = get_db()
        rows = apply_timeline_offset(video_db_id, offset, db)
        return jsonify({'success': True, 'rows_updated': rows, 'offset_applied': offset})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Bookmarks ─────────────────────────────────────────────────────────────────

@app.route('/bookmarks')
def bookmarks_all():
    """Show all bookmarked segments across all videos."""
    try:
        db = get_db()
        rows = db.execute(
            '''SELECT s.id, s.start_time, s.end_time, s.text, s.translation,
                      s.video_id, v.title as video_title, v.video_id as yt_video_id, v.language
               FROM segments s
               JOIN videos v ON v.id = s.video_id
               WHERE s.bookmarked = 1
               ORDER BY v.title COLLATE NOCASE, s.start_time'''
        ).fetchall()
        bookmarks = [dict(r) for r in rows]
        # Group by video
        grouped = {}
        for b in bookmarks:
            vid = b['video_id']
            if vid not in grouped:
                grouped[vid] = {
                    'video_id': vid,
                    'video_title': b['video_title'],
                    'yt_video_id': b['yt_video_id'],
                    'language': b['language'],
                    'segments': []
                }
            grouped[vid]['segments'].append(b)
        return render_template('bookmarks.html', grouped=grouped, video=None,
                               total=len(bookmarks))
    except Exception as e:
        flash(f'Lỗi: {str(e)}', 'error')
        return redirect(url_for('index'))


@app.route('/bookmarks/<int:video_db_id>')
def bookmarks_video(video_db_id):
    """Show bookmarked segments for a specific video."""
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            flash('Không tìm thấy video.', 'error')
            return redirect(url_for('index'))
        rows = db.execute(
            '''SELECT s.id, s.start_time, s.end_time, s.text, s.translation,
                      s.video_id, v.title as video_title, v.video_id as yt_video_id, v.language
               FROM segments s
               JOIN videos v ON v.id = s.video_id
               WHERE s.bookmarked = 1 AND s.video_id = ?
               ORDER BY s.start_time''',
            (video_db_id,)
        ).fetchall()
        bookmarks = [dict(r) for r in rows]
        grouped = {}
        if bookmarks:
            grouped[video_db_id] = {
                'video_id': video_db_id,
                'video_title': video['title'],
                'yt_video_id': video['video_id'],
                'language': video['language'],
                'segments': bookmarks
            }
        return render_template('bookmarks.html', grouped=grouped, video=video,
                               total=len(bookmarks))
    except Exception as e:
        flash(f'Lỗi: {str(e)}', 'error')
        return redirect(url_for('index'))


# ── Entry point ───────────────────────────────────────────────────────────────


# ── Practice count ────────────────────────────────────────────────────────────

@app.route('/api/segment/<int:segment_id>/practice', methods=['POST'])
def api_increment_practice(segment_id):
    """Increment practice_count for a segment."""
    try:
        db = get_db()
        db.execute(
            'UPDATE segments SET practice_count = COALESCE(practice_count, 0) + 1 WHERE id = ?',
            (segment_id,)
        )
        db.commit()
        row = db.execute('SELECT practice_count FROM segments WHERE id = ?', (segment_id,)).fetchone()
        return jsonify({'success': True, 'practice_count': row['practice_count'] if row else 0})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Daily goal ────────────────────────────────────────────────────────────────

@app.route('/api/daily_goal', methods=['GET'])
def api_get_daily_goal():
    """Get current daily goal and today's practiced seconds."""
    try:
        import datetime
        db = get_db()
        goal_row = db.execute('SELECT minutes_per_day FROM daily_goal WHERE id = 1').fetchone()
        goal_minutes = goal_row['minutes_per_day'] if goal_row else 15
        today = datetime.date.today().isoformat()
        session_row = db.execute(
            'SELECT SUM(seconds) as total FROM practice_sessions WHERE date = ?', (today,)
        ).fetchone()
        today_seconds = session_row['total'] or 0
        return jsonify({'goal_minutes': goal_minutes, 'today_seconds': today_seconds})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/daily_goal', methods=['POST'])
def api_set_daily_goal():
    """Set daily practice goal in minutes."""
    try:
        data = request.get_json(silent=True) or {}
        minutes = int(data.get('minutes', 15))
        if minutes < 1 or minutes > 480:
            return jsonify({'error': 'Mục tiêu phải từ 1–480 phút.'}), 400
        db = get_db()
        db.execute('UPDATE daily_goal SET minutes_per_day = ? WHERE id = 1', (minutes,))
        db.commit()
        return jsonify({'success': True, 'goal_minutes': minutes})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/daily_goal/log', methods=['POST'])
def api_log_practice_session():
    """Log practiced seconds for today."""
    try:
        import datetime
        data = request.get_json(silent=True) or {}
        seconds = int(data.get('seconds', 0))
        if seconds <= 0:
            return jsonify({'success': False}), 400
        db = get_db()
        today = datetime.date.today().isoformat()
        db.execute(
            'INSERT INTO practice_sessions (date, seconds) VALUES (?, ?)', (today, seconds)
        )
        db.commit()
        session_row = db.execute(
            'SELECT SUM(seconds) as total FROM practice_sessions WHERE date = ?', (today,)
        ).fetchone()
        return jsonify({'success': True, 'today_seconds': session_row['total'] or 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 3-Day Learning Cycle ──────────────────────────────────────────────────────

@app.route('/cycle/<int:video_db_id>')
def cycle_dashboard(video_db_id):
    """Show video-level overview: all chunks with their individual cycle status."""
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            flash('Không tìm thấy video.', 'error')
            return redirect(url_for('index'))

        # Load chunks for this video
        chunks_rows = db.execute(
            'SELECT * FROM chunks WHERE video_id = ? ORDER BY chunk_order', (video_db_id,)
        ).fetchall()
        chunks = [dict(c) for c in chunks_rows]
        for c in chunks:
            try:
                c['focus_expressions'] = json.loads(c['focus_expressions'] or '[]')
            except Exception:
                c['focus_expressions'] = []

        # Load all cycles belonging to this video (one per chunk)
        cycles_rows = db.execute(
            '''SELECT lc.*,
                      COUNT(sa.id) as total_acts,
                      SUM(sa.completed) as done_acts
               FROM learning_cycles lc
               LEFT JOIN session_activities sa ON sa.learning_cycle_id = lc.id
               WHERE lc.video_id = ?
               GROUP BY lc.id
               ORDER BY lc.chunk_id ASC''',
            (video_db_id,)
        ).fetchall()
        cycles_by_chunk = {r['chunk_id']: dict(r) for r in cycles_rows}

        # Attach cycle info to each chunk
        for c in chunks:
            cyc = cycles_by_chunk.get(c['id'])
            if cyc:
                total = cyc['total_acts'] or 0
                done = cyc['done_acts'] or 0
                cyc['pct'] = round(done / total * 100) if total else 0
                c['cycle'] = cyc
            else:
                c['cycle'] = None

        total_chunks = len(chunks)
        completed_chunks = sum(1 for c in chunks if c['cycle'] and c['cycle']['status'] == 'completed')
        active_chunks = sum(1 for c in chunks if c['cycle'] and c['cycle']['status'] in ('day1', 'day2', 'day3'))

        return render_template(
            'cycle_dashboard.html',
            video=video, chunks=chunks,
            total_chunks=total_chunks,
            completed_chunks=completed_chunks,
            active_chunks=active_chunks,
        )
    except Exception as e:
        flash(f'Lỗi: {str(e)}', 'error')
        return redirect(url_for('index'))


@app.route('/cycle/<int:video_db_id>/chunk/<int:chunk_id>')
def chunk_cycle_detail(video_db_id, chunk_id):
    """Show the 3-day cycle detail for one specific chunk."""
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            flash('Không tìm thấy video.', 'error')
            return redirect(url_for('index'))

        chunk = db.execute('SELECT * FROM chunks WHERE id = ? AND video_id = ?',
                           (chunk_id, video_db_id)).fetchone()
        if chunk is None:
            flash('Không tìm thấy chunk.', 'error')
            return redirect(url_for('cycle_dashboard', video_db_id=video_db_id))
        chunk = dict(chunk)
        try:
            chunk['focus_expressions'] = json.loads(chunk['focus_expressions'] or '[]')
        except Exception:
            chunk['focus_expressions'] = []

        cycle = db.execute(
            'SELECT * FROM learning_cycles WHERE video_id = ? AND chunk_id = ?',
            (video_db_id, chunk_id)
        ).fetchone()

        activities = []
        day_progress = {1: {'total': 0, 'done': 0, 'pct': 0, 'activities': []},
                        2: {'total': 0, 'done': 0, 'pct': 0, 'activities': []},
                        3: {'total': 0, 'done': 0, 'pct': 0, 'activities': []}}
        recordings = []
        if cycle:
            acts_rows = db.execute(
                '''SELECT sa.*, c.label as chunk_label, c.start_time as chunk_start, c.end_time as chunk_end
                   FROM session_activities sa
                   LEFT JOIN chunks c ON c.id = sa.chunk_id
                   WHERE sa.learning_cycle_id = ?
                   ORDER BY sa.activity_order''',
                (cycle['id'],)
            ).fetchall()
            activities = [dict(a) for a in acts_rows]
            for a in activities:
                a['meta'] = get_activity_label(a['activity_type'])
                a['time_meta'] = get_time_label(a['time_of_day'])
            by_day = {1: [], 2: [], 3: []}
            for a in activities:
                by_day[a['activity_day']].append(a)
            for d in [1, 2, 3]:
                total = len(by_day[d])
                done = sum(1 for a in by_day[d] if a['completed'])
                day_progress[d] = {
                    'total': total, 'done': done,
                    'pct': round(done / total * 100) if total else 0,
                    'activities': by_day[d],
                }
            recordings = [dict(r) for r in db.execute(
                'SELECT * FROM audio_recordings WHERE video_id = ? ORDER BY recorded_at DESC',
                (video_db_id,)
            ).fetchall()]

        # Transcript segments for this chunk's time range
        chunk_segments = [dict(s) for s in db.execute(
            '''SELECT id, start_time, end_time, text, translation
               FROM segments
               WHERE video_id = ? AND start_time >= ? AND start_time < ?
               ORDER BY start_time''',
            (video_db_id, chunk['start_time'], chunk['end_time'])
        ).fetchall()]

        # Prev/next chunks for navigation
        all_chunks = db.execute(
            'SELECT id, label FROM chunks WHERE video_id = ? ORDER BY chunk_order',
            (video_db_id,)
        ).fetchall()
        chunk_ids = [r['id'] for r in all_chunks]
        cur_idx = chunk_ids.index(chunk_id) if chunk_id in chunk_ids else -1
        prev_chunk_id = chunk_ids[cur_idx - 1] if cur_idx > 0 else None
        next_chunk_id = chunk_ids[cur_idx + 1] if cur_idx < len(chunk_ids) - 1 else None

        return render_template(
            'chunk_cycle.html',
            video=video, chunk=chunk, cycle=cycle,
            day_progress=day_progress, activities=activities,
            recordings=recordings, chunk_segments=chunk_segments,
            activity_labels=ACTIVITY_LABELS, time_labels=TIME_OF_DAY_LABELS,
            prev_chunk_id=prev_chunk_id, next_chunk_id=next_chunk_id,
        )
    except Exception as e:
        flash(f'Lỗi: {str(e)}', 'error')
        return redirect(url_for('cycle_dashboard', video_db_id=video_db_id))




@app.route('/learning-path')
def learning_path():
    """Show all videos with their cycle status."""
    try:
        db = get_db()
        rows = db.execute(
            '''SELECT v.*, lc.id as cycle_id, lc.status as cycle_status,
                      lc.comprehension_day3, lc.started_at, lc.completed_at,
                      COUNT(DISTINCT sa.id) as total_activities,
                      SUM(sa.completed) as done_activities
               FROM videos v
               LEFT JOIN learning_cycles lc ON lc.video_id = v.id
               LEFT JOIN session_activities sa ON sa.learning_cycle_id = lc.id
               GROUP BY v.id
               ORDER BY lc.started_at DESC NULLS LAST, v.created_at DESC'''
        ).fetchall()
        videos = [dict(r) for r in rows]
        # Stats
        total_completed = sum(1 for v in videos if v.get('cycle_status') == 'completed')
        in_progress = sum(1 for v in videos if v.get('cycle_status') in ('day1', 'day2', 'day3'))
        avg_comp = 0
        completed_with_score = [v for v in videos if v.get('comprehension_day3', 0)]
        if completed_with_score:
            avg_comp = round(sum(v['comprehension_day3'] for v in completed_with_score) / len(completed_with_score))
        return render_template(
            'learning_path.html',
            videos=videos, total_completed=total_completed,
            in_progress=in_progress, avg_comp=avg_comp
        )
    except Exception as e:
        flash(f'Lỗi: {str(e)}', 'error')
        return redirect(url_for('index'))


@app.route('/review')
def weekly_review():
    """Weekly review dashboard."""
    try:
        db = get_db()
        # Last 7 days practice
        today = datetime.date.today()
        week_ago = (today - datetime.timedelta(days=6)).isoformat()
        sessions = db.execute(
            '''SELECT date, SUM(seconds) as total_seconds
               FROM practice_sessions WHERE date >= ?
               GROUP BY date ORDER BY date''',
            (week_ago,)
        ).fetchall()
        daily_practice = {r['date']: r['total_seconds'] for r in sessions}
        # Fill missing days
        days_data = []
        for i in range(6, -1, -1):
            day = (today - datetime.timedelta(days=i)).isoformat()
            days_data.append({'date': day, 'seconds': daily_practice.get(day, 0)})
        total_week_seconds = sum(d['seconds'] for d in days_data)

        # Completed cycles in last 30 days
        thirty_ago = (today - datetime.timedelta(days=30)).isoformat()
        completed_cycles = db.execute(
            '''SELECT lc.*, v.title, v.video_id as yt_id, v.language
               FROM learning_cycles lc
               JOIN videos v ON v.id = lc.video_id
               WHERE lc.status = 'completed' AND lc.completed_at >= ?
               ORDER BY lc.completed_at DESC''',
            (thirty_ago,)
        ).fetchall()

        # Recent recordings
        recordings = db.execute(
            '''SELECT ar.*, v.title as video_title, v.video_id as yt_id
               FROM audio_recordings ar
               JOIN videos v ON v.id = ar.video_id
               ORDER BY ar.recorded_at DESC LIMIT 20''',
        ).fetchall()

        # Active cycles
        active_cycles = db.execute(
            '''SELECT lc.*, v.title, v.video_id as yt_id,
                      COUNT(sa.id) as total_acts,
                      SUM(sa.completed) as done_acts
               FROM learning_cycles lc
               JOIN videos v ON v.id = lc.video_id
               LEFT JOIN session_activities sa ON sa.learning_cycle_id = lc.id
               WHERE lc.status IN ('day1', 'day2', 'day3')
               GROUP BY lc.id
               ORDER BY lc.started_at DESC''',
        ).fetchall()

        return render_template(
            'weekly_review.html',
            days_data=days_data, total_week_seconds=total_week_seconds,
            completed_cycles=[dict(c) for c in completed_cycles],
            recordings=[dict(r) for r in recordings],
            active_cycles=[dict(c) for c in active_cycles]
        )
    except Exception as e:
        flash(f'Lỗi: {str(e)}', 'error')
        return redirect(url_for('index'))


# ── Cycle API ─────────────────────────────────────────────────────────────────

@app.route('/api/video/<int:video_db_id>/split-chunks', methods=['POST'])
def api_split_chunks(video_db_id):
    """Auto-split a video into ~4-minute chunks and create one learning_cycle per chunk."""
    try:
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            return jsonify({'error': 'Video không tồn tại.'}), 404

        # Delete all existing chunks and their cycles for this video
        old_chunk_ids = [r['id'] for r in db.execute(
            'SELECT id FROM chunks WHERE video_id = ?', (video_db_id,)
        ).fetchall()]
        if old_chunk_ids:
            db.execute(
                f'DELETE FROM learning_cycles WHERE chunk_id IN ({",".join("?" * len(old_chunk_ids))})',
                old_chunk_ids
            )
            db.execute('DELETE FROM chunks WHERE video_id = ?', (video_db_id,))

        # Also delete legacy video-level cycles (chunk_id IS NULL)
        db.execute(
            'DELETE FROM learning_cycles WHERE video_id = ? AND chunk_id IS NULL', (video_db_id,)
        )

        # Build chunks from transcript segments
        segs = db.execute(
            'SELECT start_time, end_time FROM segments WHERE video_id = ? ORDER BY segment_order',
            (video_db_id,)
        ).fetchall()
        chunk_dicts = auto_split_chunks(video_db_id, [dict(s) for s in segs])

        # Fallback: split by duration
        if not chunk_dicts and video['duration'] and video['duration'] > 0:
            dur = video['duration']
            target = 4 * 60  # 4 minutes
            n = max(2, round(dur / target))
            step = dur / n
            labels = ['Chunk A', 'Chunk B', 'Chunk C', 'Chunk D', 'Chunk E',
                      'Chunk F', 'Chunk G', 'Chunk H']
            chunk_dicts = [
                {
                    'video_id': video_db_id,
                    'chunk_order': i,
                    'label': labels[i] if i < len(labels) else f'Chunk {i + 1}',
                    'start_time': round(i * step, 3),
                    'end_time': round(min((i + 1) * step, dur), 3),
                }
                for i in range(n)
            ]
        elif not chunk_dicts:
            chunk_dicts = [
                {'video_id': video_db_id, 'chunk_order': 0,
                 'label': 'Chunk A', 'start_time': 0, 'end_time': 0},
            ]

        today = datetime.date.today().isoformat()
        created = []
        for c in chunk_dicts:
            cur = db.execute(
                'INSERT INTO chunks (video_id, chunk_order, label, start_time, end_time) VALUES (?, ?, ?, ?, ?)',
                (c['video_id'], c['chunk_order'], c['label'], c['start_time'], c['end_time'])
            )
            chunk_db_id = cur.lastrowid

            # Create a learning_cycle for this chunk
            cyc = db.execute(
                '''INSERT INTO learning_cycles (video_id, chunk_id, status, started_at)
                   VALUES (?, ?, 'not_started', ?)''',
                (video_db_id, chunk_db_id, today)
            )
            created.append({'chunk_id': chunk_db_id, 'cycle_id': cyc.lastrowid,
                            'label': c['label'],
                            'start_time': c['start_time'], 'end_time': c['end_time']})
        db.commit()
        return jsonify({'success': True, 'chunks': created})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/chunk/<int:chunk_id>/cycle/start', methods=['POST'])
def api_chunk_cycle_start(chunk_id):
    """Start (or restart) the 3-day cycle for a specific chunk."""
    try:
        db = get_db()
        chunk = db.execute('SELECT * FROM chunks WHERE id = ?', (chunk_id,)).fetchone()
        if chunk is None:
            return jsonify({'error': 'Chunk không tồn tại.'}), 404

        cycle = db.execute(
            'SELECT * FROM learning_cycles WHERE chunk_id = ?', (chunk_id,)
        ).fetchone()
        if cycle is None:
            return jsonify({'error': 'Không tìm thấy cycle cho chunk này.'}), 404

        # Reset activities
        db.execute('DELETE FROM session_activities WHERE learning_cycle_id = ?', (cycle['id'],))

        today = datetime.date.today().isoformat()
        db.execute(
            '''UPDATE learning_cycles SET status = 'day1', started_at = ?,
               day2_started_at = NULL, day3_started_at = NULL, completed_at = NULL,
               comprehension_day1 = 0, comprehension_day3 = 0
               WHERE id = ?''',
            (today, cycle['id'])
        )

        # Generate per-chunk schedule
        activities = generate_per_chunk_schedule(cycle['id'], chunk_id)
        for act in activities:
            db.execute(
                '''INSERT INTO session_activities
                   (learning_cycle_id, activity_day, time_of_day, activity_type,
                    chunk_id, speed, duration_minutes, activity_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (act['learning_cycle_id'], act['activity_day'], act['time_of_day'],
                 act['activity_type'], act['chunk_id'], act['speed'],
                 act['duration_minutes'], act['activity_order'])
            )
        db.commit()
        return jsonify({'success': True, 'cycle_id': cycle['id'], 'chunk_id': chunk_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cycle-by-id/<int:cycle_id>/advance', methods=['POST'])
def api_cycle_by_id_advance(cycle_id):
    """Advance a specific cycle (by cycle id) to the next day."""
    try:
        db = get_db()
        cycle = db.execute('SELECT * FROM learning_cycles WHERE id = ?', (cycle_id,)).fetchone()
        if cycle is None:
            return jsonify({'error': 'Không tìm thấy cycle.'}), 404
        today = datetime.date.today().isoformat()
        next_map = {'day1': ('day2', 'day2_started_at'),
                    'day2': ('day3', 'day3_started_at'),
                    'day3': ('completed', 'completed_at')}
        current = cycle['status']
        if current not in next_map:
            return jsonify({'error': 'Cycle đã completed hoặc chưa bắt đầu.'}), 400
        new_status, date_col = next_map[current]
        db.execute(
            f'UPDATE learning_cycles SET status = ?, {date_col} = ? WHERE id = ?',
            (new_status, today, cycle_id)
        )
        db.commit()
        return jsonify({'success': True, 'new_status': new_status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cycle-by-id/<int:cycle_id>/comprehension', methods=['POST'])
def api_cycle_by_id_comprehension(cycle_id):
    """Save comprehension % for a specific cycle by cycle id."""
    try:
        data = request.get_json(silent=True) or {}
        pct = int(data.get('pct', 0))
        day = int(data.get('day', 1))
        if not (0 <= pct <= 100) or day not in (1, 3):
            return jsonify({'error': 'Tham số không hợp lệ.'}), 400
        db = get_db()
        col = f'comprehension_day{day}'
        db.execute(f'UPDATE learning_cycles SET {col} = ? WHERE id = ?', (pct, cycle_id))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cycle/<int:video_db_id>/start', methods=['POST'])
def api_cycle_start(video_db_id):
    """Legacy: split video + start all chunk cycles at once."""
    try:
        # Re-use split-chunks logic then start all cycles
        from flask import current_app
        with current_app.test_request_context():
            pass  # just to validate context
        db = get_db()
        video = db.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,)).fetchone()
        if video is None:
            return jsonify({'error': 'Video không tồn tại.'}), 404

        # Delete old data
        old_chunk_ids = [r['id'] for r in db.execute(
            'SELECT id FROM chunks WHERE video_id = ?', (video_db_id,)
        ).fetchall()]
        if old_chunk_ids:
            db.execute(
                f'DELETE FROM learning_cycles WHERE chunk_id IN ({",".join("?" * len(old_chunk_ids))})',
                old_chunk_ids
            )
            db.execute('DELETE FROM chunks WHERE video_id = ?', (video_db_id,))
        db.execute('DELETE FROM learning_cycles WHERE video_id = ? AND chunk_id IS NULL', (video_db_id,))

        segs = db.execute(
            'SELECT start_time, end_time FROM segments WHERE video_id = ? ORDER BY segment_order',
            (video_db_id,)
        ).fetchall()
        chunk_dicts = auto_split_chunks(video_db_id, [dict(s) for s in segs])
        if not chunk_dicts and video['duration'] and video['duration'] > 0:
            dur = video['duration']
            target = 4 * 60
            n = max(2, round(dur / target))
            step = dur / n
            labels = ['Chunk A', 'Chunk B', 'Chunk C', 'Chunk D', 'Chunk E',
                      'Chunk F', 'Chunk G', 'Chunk H']
            chunk_dicts = [
                {'video_id': video_db_id, 'chunk_order': i,
                 'label': labels[i] if i < len(labels) else f'Chunk {i + 1}',
                 'start_time': round(i * step, 3),
                 'end_time': round(min((i + 1) * step, dur), 3)}
                for i in range(n)
            ]
        elif not chunk_dicts:
            chunk_dicts = [
                {'video_id': video_db_id, 'chunk_order': 0,
                 'label': 'Chunk A', 'start_time': 0, 'end_time': 0}
            ]

        today = datetime.date.today().isoformat()
        total_acts = 0
        for c in chunk_dicts:
            cur = db.execute(
                'INSERT INTO chunks (video_id, chunk_order, label, start_time, end_time) VALUES (?, ?, ?, ?, ?)',
                (c['video_id'], c['chunk_order'], c['label'], c['start_time'], c['end_time'])
            )
            chunk_db_id = cur.lastrowid
            cyc = db.execute(
                '''INSERT INTO learning_cycles (video_id, chunk_id, status, started_at)
                   VALUES (?, ?, 'day1', ?)''',
                (video_db_id, chunk_db_id, today)
            )
            cycle_id = cyc.lastrowid
            acts = generate_per_chunk_schedule(cycle_id, chunk_db_id)
            for act in acts:
                db.execute(
                    '''INSERT INTO session_activities
                       (learning_cycle_id, activity_day, time_of_day, activity_type,
                        chunk_id, speed, duration_minutes, activity_order)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (act['learning_cycle_id'], act['activity_day'], act['time_of_day'],
                     act['activity_type'], act['chunk_id'], act['speed'],
                     act['duration_minutes'], act['activity_order'])
                )
                total_acts += 1
        db.commit()
        return jsonify({'success': True, 'chunks': len(chunk_dicts), 'activities': total_acts})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cycle/<int:video_db_id>/status')
def api_cycle_status(video_db_id):
    try:
        db = get_db()
        cycles = db.execute(
            '''SELECT lc.*, COUNT(sa.id) as total_acts, SUM(sa.completed) as done_acts
               FROM learning_cycles lc
               LEFT JOIN session_activities sa ON sa.learning_cycle_id = lc.id
               WHERE lc.video_id = ?
               GROUP BY lc.id''',
            (video_db_id,)
        ).fetchall()
        if not cycles:
            return jsonify({'status': 'none'})
        summary = []
        for c in cycles:
            total = c['total_acts'] or 0
            done = c['done_acts'] or 0
            summary.append({
                'cycle_id': c['id'],
                'chunk_id': c['chunk_id'],
                'status': c['status'],
                'pct': round(done / total * 100) if total else 0,
            })
        return jsonify({'cycles': summary})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cycle/<int:video_db_id>/advance', methods=['POST'])
def api_cycle_advance(video_db_id):
    """Advance the first in-progress cycle for a video (legacy compat)."""
    try:
        db = get_db()
        cycle = db.execute(
            '''SELECT * FROM learning_cycles WHERE video_id = ? AND status IN ('day1','day2','day3')
               ORDER BY id LIMIT 1''',
            (video_db_id,)
        ).fetchone()
        if cycle is None:
            return jsonify({'error': 'Không có cycle đang hoạt động.'}), 404
        today = datetime.date.today().isoformat()
        next_map = {'day1': ('day2', 'day2_started_at'), 'day2': ('day3', 'day3_started_at'),
                    'day3': ('completed', 'completed_at')}
        new_status, date_col = next_map[cycle['status']]
        db.execute(
            f'UPDATE learning_cycles SET status = ?, {date_col} = ? WHERE id = ?',
            (new_status, today, cycle['id'])
        )
        db.commit()
        return jsonify({'success': True, 'new_status': new_status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cycle/<int:video_db_id>/comprehension', methods=['POST'])
def api_cycle_comprehension(video_db_id):
    """Save comprehension % estimate (legacy – targets first cycle)."""
    try:
        data = request.get_json(silent=True) or {}
        pct = int(data.get('pct', 0))
        day = int(data.get('day', 1))
        if not (0 <= pct <= 100) or day not in (1, 3):
            return jsonify({'error': 'Tham số không hợp lệ.'}), 400
        db = get_db()
        col = f'comprehension_day{day}'
        db.execute(
            f'UPDATE learning_cycles SET {col} = ? WHERE video_id = ? ORDER BY id LIMIT 1',
            (pct, video_db_id)
        )
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500




# ── Chunks API ────────────────────────────────────────────────────────────────

@app.route('/api/chunks/<int:video_db_id>')
def api_get_chunks(video_db_id):
    try:
        db = get_db()
        rows = db.execute(
            'SELECT * FROM chunks WHERE video_id = ? ORDER BY chunk_order', (video_db_id,)
        ).fetchall()
        chunks = []
        for r in rows:
            c = dict(r)
            try:
                c['focus_expressions'] = json.loads(c['focus_expressions'] or '[]')
            except Exception:
                c['focus_expressions'] = []
            chunks.append(c)
        return jsonify(chunks)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/chunks/<int:chunk_id>/expressions', methods=['PATCH'])
def api_update_chunk_expressions(chunk_id):
    """Save focus expressions for a chunk."""
    try:
        data = request.get_json(silent=True) or {}
        expressions = data.get('expressions', [])
        if not isinstance(expressions, list):
            return jsonify({'error': 'expressions phải là mảng.'}), 400
        # Sanitize
        expressions = [str(e).strip() for e in expressions if str(e).strip()][:15]
        db = get_db()
        db.execute(
            'UPDATE chunks SET focus_expressions = ? WHERE id = ?',
            (json.dumps(expressions, ensure_ascii=False), chunk_id)
        )
        db.commit()
        return jsonify({'success': True, 'count': len(expressions)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Session Activity API ──────────────────────────────────────────────────────

@app.route('/api/activity/<int:activity_id>/complete', methods=['POST'])
def api_complete_activity(activity_id):
    """Toggle activity completion."""
    try:
        data = request.get_json(silent=True) or {}
        completed = int(bool(data.get('completed', True)))
        db = get_db()
        act = db.execute('SELECT * FROM session_activities WHERE id = ?', (activity_id,)).fetchone()
        if act is None:
            return jsonify({'error': 'Activity không tồn tại.'}), 404
        now = datetime.datetime.now().isoformat() if completed else None
        db.execute(
            'UPDATE session_activities SET completed = ?, completed_at = ? WHERE id = ?',
            (completed, now, activity_id)
        )
        db.commit()
        return jsonify({'success': True, 'completed': completed})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Recording API ─────────────────────────────────────────────────────────────

ALLOWED_AUDIO = {'webm', 'mp3', 'wav', 'ogg', 'm4a', 'opus'}

def _allowed_audio(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_AUDIO


@app.route('/api/recording/upload', methods=['POST'])
def api_upload_recording():
    """Upload a free-recall or free-speech audio recording."""
    try:
        video_id = request.form.get('video_id', type=int)
        activity_id = request.form.get('activity_id', type=int)
        activity_type = request.form.get('activity_type', 'free_recall')
        duration = request.form.get('duration', 0, type=int)
        notes = (request.form.get('notes') or '').strip()[:500]

        if not video_id:
            return jsonify({'error': 'Thiếu video_id.'}), 400

        audio_file = request.files.get('audio')
        if not audio_file or not audio_file.filename:
            return jsonify({'error': 'Không có file audio.'}), 400
        if not _allowed_audio(audio_file.filename):
            return jsonify({'error': 'Định dạng file không được hỗ trợ.'}), 400

        # Build a safe filename
        ext = audio_file.filename.rsplit('.', 1)[-1].lower()
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'rec_{video_id}_{activity_type}_{ts}.{ext}'

        recordings_dir = os.path.join(app.static_folder, 'recordings')
        os.makedirs(recordings_dir, exist_ok=True)
        audio_file.save(os.path.join(recordings_dir, filename))

        db = get_db()
        cur = db.execute(
            '''INSERT INTO audio_recordings (video_id, activity_id, activity_type, filename, duration_seconds, self_notes)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (video_id, activity_id, activity_type, filename, duration, notes or None)
        )
        db.commit()
        return jsonify({'success': True, 'id': cur.lastrowid, 'filename': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/recording/<int:recording_id>', methods=['DELETE'])
def api_delete_recording(recording_id):
    try:
        db = get_db()
        row = db.execute('SELECT * FROM audio_recordings WHERE id = ?', (recording_id,)).fetchone()
        if row is None:
            return jsonify({'error': 'Không tìm thấy recording.'}), 404
        filepath = os.path.join(app.static_folder, 'recordings', row['filename'])
        if os.path.exists(filepath):
            os.remove(filepath)
        db.execute('DELETE FROM audio_recordings WHERE id = ?', (recording_id,))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/recording/<int:recording_id>/notes', methods=['PATCH'])
def api_update_recording_notes(recording_id):
    try:
        data = request.get_json(silent=True) or {}
        notes = (data.get('notes') or '').strip()[:500]
        db = get_db()
        db.execute('UPDATE audio_recordings SET self_notes = ? WHERE id = ?', (notes, recording_id))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Learning Sources ──────────────────────────────────────────────────────────

@app.route('/sources')
def learning_sources():
    """Learning sources management page."""
    try:
        db = get_db()
        rows = db.execute(
            'SELECT * FROM learning_sources ORDER BY phase, position, id'
        ).fetchall()
        sources_n2 = [dict(r) for r in rows if r['phase'] == 'N2']
        sources_n1 = [dict(r) for r in rows if r['phase'] == 'N1']
        return render_template('sources.html', sources_n2=sources_n2, sources_n1=sources_n1)
    except Exception as e:
        flash(f'Lỗi: {str(e)}', 'error')
        return redirect(url_for('index'))


@app.route('/api/source', methods=['POST'])
def api_create_source():
    data = request.get_json(silent=True) or {}
    phase = data.get('phase', 'N2').strip()
    channel_name = (data.get('channel_name') or '').strip()
    if not channel_name:
        return jsonify({'error': 'Thiếu tên kênh'}), 400
    if phase not in ('N2', 'N1'):
        phase = 'N2'
    try:
        db = get_db()
        max_pos = db.execute(
            'SELECT MAX(position) FROM learning_sources WHERE phase = ?', (phase,)
        ).fetchone()[0] or 0
        cursor = db.execute(
            '''INSERT INTO learning_sources (phase, channel_name, link, topic, level, reason, position)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (phase, channel_name, data.get('link', ''), data.get('topic', ''),
             data.get('level', ''), data.get('reason', ''), max_pos + 1)
        )
        db.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/source/<int:source_id>', methods=['PATCH'])
def api_update_source(source_id):
    data = request.get_json(silent=True) or {}
    allowed = ('phase', 'channel_name', 'link', 'topic', 'level', 'reason', 'position')
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'Không có dữ liệu cập nhật'}), 400
    if 'phase' in updates and updates['phase'] not in ('N2', 'N1'):
        return jsonify({'error': 'phase phải là N2 hoặc N1'}), 400
    try:
        db = get_db()
        set_clause = ', '.join(f'{k} = ?' for k in updates)
        db.execute(
            f'UPDATE learning_sources SET {set_clause} WHERE id = ?',
            list(updates.values()) + [source_id]
        )
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/source/<int:source_id>', methods=['DELETE'])
def api_delete_source(source_id):
    try:
        db = get_db()
        db.execute('DELETE FROM learning_sources WHERE id = ?', (source_id,))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    with app.app_context():
        init_db()
    app.run(debug=True, host='0.0.0.0', port=5015)

