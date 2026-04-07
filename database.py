import sqlite3
import click
from flask import current_app, g
import warnings

# Suppress the sqlite3 TIMESTAMP/DATE deprecation warnings (Python 3.12+)
warnings.filterwarnings('ignore', category=DeprecationWarning, module='sqlite3')

# Custom converters for sqlite3 (to replace deprecated defaults)
def _timestamp_converter(val):
    """Convert SQLite TIMESTAMP to string."""
    if isinstance(val, bytes):
        return val.decode('utf-8')
    return val

def _date_converter(val):
    """Convert SQLite DATE to string."""
    if isinstance(val, bytes):
        return val.decode('utf-8')
    return val

sqlite3.register_converter('TIMESTAMP', _timestamp_converter)
sqlite3.register_converter('DATE', _date_converter)


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(
            current_app.config['DATABASE'],
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            youtube_url TEXT NOT NULL,
            video_id TEXT NOT NULL,
            title TEXT,
            language TEXT,
            duration INTEGER,
            audio_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER REFERENCES videos(id),
            segment_order INTEGER,
            start_time REAL,
            end_time REAL,
            text TEXT,
            translation TEXT,
            bookmarked INTEGER DEFAULT 0
        )
    ''')
    # Migrations for existing databases
    for _sql in [
        'ALTER TABLE segments ADD COLUMN bookmarked INTEGER DEFAULT 0',
        'ALTER TABLE segments ADD COLUMN practice_count INTEGER DEFAULT 0',
        'ALTER TABLE videos ADD COLUMN audio_path TEXT',
        'ALTER TABLE videos ADD COLUMN transcript_raw TEXT',
    ]:
        try:
            db.execute(_sql)
            db.commit()
        except Exception:
            pass  # column already exists
    db.execute('''
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS playlist_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL,
            video_id INTEGER NOT NULL,
            position INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(playlist_id, video_id)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS daily_goal (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            minutes_per_day INTEGER DEFAULT 15
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS practice_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            seconds INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Ensure default daily_goal row exists
    db.execute('INSERT OR IGNORE INTO daily_goal (id, minutes_per_day) VALUES (1, 15)')

    # ── Learning Cycle tables ─────────────────────────────────────────────────
    # Migration: recreate learning_cycles with chunk_id support if needed
    try:
        db.execute('SELECT chunk_id FROM learning_cycles LIMIT 1')
    except Exception:
        # chunk_id column missing – migrate to new schema
        db.execute('''
            CREATE TABLE IF NOT EXISTS learning_cycles_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER NOT NULL,
                chunk_id INTEGER,
                status TEXT DEFAULT 'day1',
                comprehension_day1 INTEGER DEFAULT 0,
                comprehension_day3 INTEGER DEFAULT 0,
                started_at DATE,
                day2_started_at DATE,
                day3_started_at DATE,
                completed_at DATE,
                notes TEXT,
                FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE,
                FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
            )
        ''')
        # Copy existing rows (chunk_id defaults to NULL)
        try:
            db.execute('''
                INSERT INTO learning_cycles_v2
                    (id, video_id, chunk_id, status, comprehension_day1,
                     comprehension_day3, started_at, day2_started_at,
                     day3_started_at, completed_at, notes)
                SELECT id, video_id, NULL, status, comprehension_day1,
                       comprehension_day3, started_at, day2_started_at,
                       day3_started_at, completed_at, notes
                FROM learning_cycles
            ''')
        except Exception:
            pass
        db.execute('DROP TABLE IF EXISTS learning_cycles')
        db.execute('ALTER TABLE learning_cycles_v2 RENAME TO learning_cycles')
        db.commit()

    db.execute('''
        CREATE TABLE IF NOT EXISTS learning_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            chunk_id INTEGER,
            status TEXT DEFAULT 'day1',
            comprehension_day1 INTEGER DEFAULT 0,
            comprehension_day3 INTEGER DEFAULT 0,
            started_at DATE,
            day2_started_at DATE,
            day3_started_at DATE,
            completed_at DATE,
            notes TEXT,
            FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE,
            FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            chunk_order INTEGER NOT NULL DEFAULT 0,
            label TEXT,
            start_time REAL NOT NULL DEFAULT 0,
            end_time REAL NOT NULL DEFAULT 0,
            focus_expressions TEXT DEFAULT '[]',
            notes TEXT,
            FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS session_activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            learning_cycle_id INTEGER NOT NULL,
            activity_day INTEGER NOT NULL,
            time_of_day TEXT NOT NULL DEFAULT 'evening',
            activity_type TEXT NOT NULL,
            chunk_id INTEGER,
            speed REAL DEFAULT 1.0,
            duration_minutes INTEGER DEFAULT 5,
            activity_order INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            completed_at TIMESTAMP,
            FOREIGN KEY(learning_cycle_id) REFERENCES learning_cycles(id) ON DELETE CASCADE,
            FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE SET NULL
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS audio_recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            activity_id INTEGER,
            activity_type TEXT NOT NULL DEFAULT 'free_recall',
            filename TEXT NOT NULL,
            duration_seconds INTEGER DEFAULT 0,
            self_notes TEXT,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE,
            FOREIGN KEY(activity_id) REFERENCES session_activities(id) ON DELETE SET NULL
        )
    ''')

    # ── Learning Sources ──────────────────────────────────────────────────────
    db.execute('''
        CREATE TABLE IF NOT EXISTS learning_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phase TEXT NOT NULL DEFAULT 'N2',
            channel_name TEXT NOT NULL,
            link TEXT,
            topic TEXT,
            level TEXT,
            reason TEXT,
            position INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Seed initial data only if table is empty
    if db.execute('SELECT COUNT(*) FROM learning_sources').fetchone()[0] == 0:
        _seed = [
            # phase, channel_name, link, topic, level, reason, position
            ('N2', 'あかね的日本語教室', 'youtube.com/@akanenihongo',
             'Văn hóa, đời sống, hội thoại', 'N2-N1',
             'Phát âm chuẩn, tốc độ vừa. Tốt nhất để lấy chunks văn nói tự nhiên. Có thể bật auto-caption làm transcript.', 1),
            ('N2', 'Nihongo con Teppei', 'youtube.com + Podcast',
             'Đủ chủ đề, nói tự nhiên không script', 'N2-N1',
             'Tiếng Nhật "thô" không chỉnh sửa. Lý tưởng để tai quen connected speech thật. Bắt đầu bằng "for Beginners".', 2),
            ('N2', '日本語の森 (Nihongo no Mori)', 'youtube.com/@nihongonomori',
             'Ngữ pháp N2/N1 bằng tiếng Nhật hoàn toàn', 'N2→N1',
             'Vừa học ngữ pháp vừa luyện nghe. Giọng giảng rõ ràng, tốc độ có kiểm soát. Transcript khá đầy đủ.', 3),
            ('N2', 'Speak Japanese Naturally (Naoko)', 'youtube.com/@naokostudio',
             'Đời sống, văn hóa 5-10 phút/video', 'N2',
             'Video ngắn lý tưởng cho shadowing. Có transcript đầy đủ. Chủ đề đa dạng, không nhàm.', 4),
            ('N2', 'ゆる言語学ラジオ', 'youtube.com/@yurugengo',
             'Ngôn ngữ học, tư duy, cuộc sống', 'Cuối N2 → N1',
             'Hai người nói chuyện tự nhiên, nhiều expression thú vị. Giúp hiểu cách người Nhật tư duy và diễn đạt.', 5),
            ('N1', 'NHK News Web & NHK World JP', 'nhk.or.jp/news  nhk.or.jp/nhkworld',
             'Tin tức thời sự (chuẩn vàng phát âm)', 'N1',
             'Giọng news anchor chuẩn nhất Nhật Bản. Bắt đầu bằng NHK Web Easy (có furigana), sau lên NHK News chính.', 1),
            ('N1', 'Rebuild.fm', 'rebuild.fm (podcast + YT)',
             'Tech, startup, tools (IT thuần túy)', 'N1 - IT',
             '★ Phù hợp nhất với ngữ cảnh IT Sales. 2 kỹ sư JP nói về tech news tự nhiên. Từ vựng IT thực tế 100%.', 2),
            ('N1', 'Maruko Tech Life', 'youtube.com/@marukotechlife',
             'Review sản phẩm tech, giải thích IT concept', 'N1 - IT',
             'Từ vựng IT tự nhiên theo kiểu engineer nói chuyện, không học thuật. Rất sát với daily conversation IT office.', 3),
            ('N1', 'ひろゆき (Hiroyuki)', 'youtube.com/@hiroyuki',
             'Đủ chủ đề – nói rất nhanh, nhiều quan điểm', 'N1 nâng cao',
             'Tốc độ cao, nhiều expression biểu đạt quan điểm. Dùng giai đoạn cuối để luyện theo kịp tư duy người Nhật.', 4),
            ('N1', '営業ロールプレイ (search term)', '「営業 ロールプレイ」「IT商談 提案」trên YouTube',
             'Role-play sales JP, Presentation, demo', 'N1 - Sale',
             '★ Quan trọng nhất cho Presale/BRSE. Shadow các video này = vừa luyện tiếng vừa luyện kỹ năng nghề nghiệp.', 5),
        ]
        db.executemany(
            'INSERT INTO learning_sources (phase, channel_name, link, topic, level, reason, position) VALUES (?,?,?,?,?,?,?)',
            _seed
        )

    db.commit()
