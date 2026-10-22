"""SQLiteへの会話・発言の保存。

接続は呼び出しごとに開閉する（会話スレッドとWebリクエストのスレッドが別のため）。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    ended_at     TEXT,
    mode         TEXT NOT NULL,          -- free / topic
    topic        TEXT,
    name_a       TEXT NOT NULL,
    name_b       TEXT NOT NULL,
    model_a      TEXT NOT NULL,
    model_b      TEXT NOT NULL,
    persona_a    TEXT NOT NULL,
    persona_b    TEXT NOT NULL,
    max_turns    INTEGER NOT NULL,
    status       TEXT NOT NULL,          -- running / paused / completed / stopped / error / interrupted
    max_chars    INTEGER,
    history_size INTEGER,
    interval_sec INTEGER,
    provider_a   TEXT,                   -- AIの提供会社（gemini など。空なら gemini）
    provider_b   TEXT,
    character_a  TEXT,                   -- キャラクター設定（JSON）。性格文は persona_a に入る
    character_b  TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    turn_number     INTEGER NOT NULL,
    speaker         TEXT NOT NULL,      -- A / B / system
    created_at      TEXT NOT NULL,
    content         TEXT NOT NULL,
    status          TEXT NOT NULL,      -- complete / error
    director_note   TEXT                -- その回に追加した進行役の指示の種類（カンマ区切り）
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
-- APIの呼び出し1回ごとの記録（成功・失敗とも。無料枠の使用回数の目安に使う）
CREATE TABLE IF NOT EXISTS api_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    model           TEXT NOT NULL,
    conversation_id INTEGER,
    outcome         TEXT NOT NULL,      -- ok / エラーの種類（rate_limit, daily_quota など）
    prompt_tokens   INTEGER,
    output_tokens   INTEGER,
    total_tokens    INTEGER,
    http_status     INTEGER,            -- エラー時のHTTPステータス（503・429など）
    retry_after_sec REAL,               -- 429のとき、APIが示した解除までの秒数
    reset_at        TEXT,               -- 上の秒数から計算した解除予定時刻（PCの時刻＝日本時間）
    quota_id        TEXT                -- 429のとき、当たった上限の種類
);
CREATE INDEX IF NOT EXISTS idx_api_calls_time ON api_calls(created_at);
-- 1日の上限の429で分かった、モデルごとの上限値
CREATE TABLE IF NOT EXISTS model_limits (
    model       TEXT PRIMARY KEY,
    daily_limit INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

# 古いDBに後から追加した列 {テーブル: {列名: 型}}
ADDED_COLUMNS = {
    "conversations": {
        "max_chars": "INTEGER",
        "history_size": "INTEGER",
        "interval_sec": "INTEGER",
        "provider_a": "TEXT",
        "provider_b": "TEXT",
        "character_a": "TEXT",
        "character_b": "TEXT",
    },
    "messages": {
        "director_note": "TEXT",
    },
    "api_calls": {
        "http_status": "INTEGER",
        "retry_after_sec": "REAL",
        "reset_at": "TEXT",
        "quota_id": "TEXT",
    },
}


def now():
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """テーブルを用意し、前回アプリ終了時に進行中だった会話を「中断」にする。

    戻り値: 中断扱いにした会話の数
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in ADDED_COLUMNS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        # 会話中にPowerShellを閉じた等で running / paused のまま残った会話
        cur = conn.execute(
            "UPDATE conversations SET status='interrupted', ended_at=? "
            "WHERE status IN ('running', 'paused')",
            (now(),),
        )
        return cur.rowcount


def create_conversation(mode, topic, name_a, name_b, model_a, model_b,
                        persona_a, persona_b, max_turns, max_chars, history_size,
                        interval_sec, provider_a="gemini", provider_b="gemini",
                        character_a=None, character_b=None):
    """character_a / character_b はキャラクター設定の dict（JSONにして保存する）。"""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (created_at, mode, topic, name_a, name_b, "
            "model_a, model_b, persona_a, persona_b, max_turns, status, "
            "max_chars, history_size, interval_sec, provider_a, provider_b, "
            "character_a, character_b) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)",
            (now(), mode, topic, name_a, name_b, model_a, model_b,
             persona_a, persona_b, max_turns, max_chars, history_size, interval_sec,
             provider_a, provider_b,
             json.dumps(character_a, ensure_ascii=False) if character_a else None,
             json.dumps(character_b, ensure_ascii=False) if character_b else None),
        )
        return cur.lastrowid


def last_characters():
    """最後に始めた会話のキャラクター設定 (A, B)。記録が無ければ (None, None)。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT character_a, character_b FROM conversations "
            "WHERE character_a IS NOT NULL AND character_b IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None, None
    try:
        return json.loads(row["character_a"]), json.loads(row["character_b"])
    except ValueError:
        return None, None


def used_models():
    """過去の会話で使った (会社, モデル名) の一覧。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(provider_a, 'gemini'), model_a FROM conversations "
            "UNION SELECT COALESCE(provider_b, 'gemini'), model_b FROM conversations"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]


def set_status(conversation_id, status, ended=False):
    """状態を更新する。ended=True なら終了日時を記録し、False なら終了日時を消す。"""
    with connect() as conn:
        conn.execute(
            "UPDATE conversations SET status=?, ended_at=? WHERE id=?",
            (status, now() if ended else None, conversation_id),
        )


def add_message(conversation_id, turn_number, speaker, content, status="complete",
                director_note=None):
    """発言を保存し、保存した行を dict で返す。"""
    created_at = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, turn_number, speaker, created_at, "
            "content, status, director_note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, turn_number, speaker, created_at, content, status, director_note),
        )
        return {
            "id": cur.lastrowid,
            "conversation_id": conversation_id,
            "turn_number": turn_number,
            "speaker": speaker,
            "created_at": created_at,
            "content": content,
            "status": status,
            "director_note": director_note,
        }


def get_conversation(conversation_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        return dict(row) if row else None


def list_conversations():
    """過去の会話一覧（新しい順）。発言数（AIの発言のみ）付き。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.created_at, c.ended_at, c.mode, c.topic, c.status, "
            "c.max_turns, c.name_a, c.name_b, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id "
            " AND m.speaker IN ('A','B') AND m.status='complete') AS turns "
            "FROM conversations c ORDER BY c.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_messages(conversation_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# API使用回数（目安）
# ---------------------------------------------------------------------------

def add_api_call(model, conversation_id, outcome, prompt_tokens=None,
                 output_tokens=None, total_tokens=None, http_status=None,
                 retry_after_sec=None, quota_id=None):
    """API呼び出しを1件記録する。retry_after_sec があれば解除予定時刻も計算して残す。"""
    created = datetime.now()
    reset_at = None
    if retry_after_sec is not None:
        reset_at = (created + timedelta(seconds=retry_after_sec)).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            "INSERT INTO api_calls (created_at, model, conversation_id, outcome, "
            "prompt_tokens, output_tokens, total_tokens, http_status, retry_after_sec, "
            "reset_at, quota_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (created.isoformat(timespec="seconds"), model, conversation_id, outcome,
             prompt_tokens, output_tokens, total_tokens, http_status, retry_after_sec,
             reset_at, quota_id),
        )
        return reset_at


def latest_daily_reset(model):
    """そのモデルで最後に記録された「1日の上限」の解除予定時刻（まだ先のものだけ）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT reset_at FROM api_calls WHERE model=? AND outcome='daily_quota' "
            "AND reset_at IS NOT NULL AND reset_at > ? ORDER BY id DESC LIMIT 1",
            (model, now()),
        ).fetchone()
        return row["reset_at"] if row else None


def usage_since(hours):
    """直近 hours 時間のモデルごとの呼び出し回数・トークン数・最も古い呼び出し日時。"""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute(
            "SELECT model, COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens, "
            "MIN(created_at) AS oldest FROM api_calls WHERE created_at >= ? "
            "GROUP BY model ORDER BY model",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_model_limit(model, daily_limit):
    with connect() as conn:
        conn.execute(
            "INSERT INTO model_limits (model, daily_limit, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(model) DO UPDATE SET daily_limit=excluded.daily_limit, "
            "updated_at=excluded.updated_at",
            (model, daily_limit, now()),
        )


def get_model_limits():
    with connect() as conn:
        return {r["model"]: r["daily_limit"]
                for r in conn.execute("SELECT model, daily_limit FROM model_limits")}


def nth_oldest_call_since(model, hours, n):
    """直近 hours 時間のうち、model の n 番目に古い呼び出し日時（1始まり）。"""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            "SELECT created_at FROM api_calls WHERE model=? AND created_at >= ? "
            "ORDER BY created_at, id LIMIT 1 OFFSET ?",
            (model, since, n - 1),
        ).fetchone()
        return row["created_at"] if row else None
