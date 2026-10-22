"""停止の前に、進行中（会話中・一時停止中）の会話をDBから読み取って表示する。

DBは読み取り専用で開く。アプリの画面・API、Gemini API は使わない。
stop.ps1 から呼ばれる。進行中の会話が無ければ何も表示しない。
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "conversations.db"
STATUS_LABELS = {"running": "会話中", "paused": "一時停止中"}


def main():
    if not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=3)
        rows = conn.execute(
            "SELECT c.id, c.name_a, c.name_b, c.status, c.max_turns, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id "
            " AND m.speaker IN ('A', 'B') AND m.status = 'complete') "
            "FROM conversations c WHERE c.status IN ('running', 'paused') ORDER BY c.id"
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        print(f"（会話の状態を読み取れませんでした: {e}）")
        return
    for cid, name_a, name_b, status, max_turns, turns in rows:
        print(f"会話{cid}（{name_a}×{name_b}）{STATUS_LABELS[status]}　{turns} / {max_turns} 発言")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
