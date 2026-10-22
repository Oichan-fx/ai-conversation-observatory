"""会話ログの傾向を集計する（APIは呼ばない。DBは読み取り専用で開く）。

使い方:
  python analyze_logs.py                      … すべての会話
  python analyze_logs.py --from 2026-10-17    … この日時以降に始まった会話だけ
  python analyze_logs.py --to 2026-10-16T23:59 … この日時以前に始まった会話だけ
改善の前後を比べるときは、--to と --from で期間を分けて2回実行する。
"""
import argparse
import sqlite3
import sys
from collections import Counter

import config
import director


def load(args):
    if not config.DB_PATH.exists():
        sys.exit(f"DBが見つかりません: {config.DB_PATH}")
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    where, params = [], []
    if args.date_from:
        where.append("created_at >= ?"); params.append(args.date_from)
    if args.date_to:
        where.append("created_at <= ?"); params.append(args.date_to)
    sql = "SELECT * FROM conversations" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
    convs = [dict(r) for r in conn.execute(sql, params)]
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    note_col = "director_note" if "director_note" in cols else "NULL AS director_note"
    for c in convs:
        c["messages"] = [dict(r) for r in conn.execute(
            f"SELECT speaker, turn_number, content, {note_col} FROM messages "
            "WHERE conversation_id=? AND speaker IN ('A','B') AND status='complete' ORDER BY id",
            (c["id"],))]
    conn.close()
    return [c for c in convs if c["messages"]]


def pct(n, d):
    return f"{n}/{d}（{n / d:.0%}）" if d else "-"


def main():
    p = argparse.ArgumentParser(description="会話ログの傾向を集計します（APIは呼びません）")
    p.add_argument("--from", dest="date_from", help="この日時以降に始まった会話（例: 2026-10-17）")
    p.add_argument("--to", dest="date_to", help="この日時以前に始まった会話（例: 2026-10-16T23:59）")
    args = p.parse_args()
    convs = load(args)
    if not convs:
        print("対象の会話がありません。")
        return

    total = {"A": [0, 0], "B": [0, 0]}          # [質問で終わる数, 発言数]
    wrap = [0, 0, 0, 0]                          # 前半の該当発言数, 前半の発言数, 後半…, 後半…
    opening = {"A": 0, "B": 0}                   # 褒め・同意の言葉から書き出した発言数
    notes = Counter()
    print(f"{'会話':>4}  {'発言':>4}  {'A 質問終わり':>12}  {'B 質問終わり':>12}  "
          f"{'A 褒め書き出し':>12}  {'B 褒め書き出し':>12}  {'褒め/まとめ 前半→後半':>18}  進行役の指示")
    for c in convs:
        ms = c["messages"]
        row = {}
        for sp in ("A", "B"):
            own = [m for m in ms if m["speaker"] == sp]
            q = sum(director.ends_with_question(m["content"]) for m in own)
            total[sp][0] += q; total[sp][1] += len(own)
            row[sp] = pct(q, len(own))
            o = sum(director.opens_with_praise(m["content"]) for m in own)
            opening[sp] += o
            row["open" + sp] = pct(o, len(own))
        half = len(ms) // 2
        first = sum(director.has_wrapup_words(m["content"]) for m in ms[:half])
        second = sum(director.has_wrapup_words(m["content"]) for m in ms[half:])
        wrap[0] += first; wrap[1] += half; wrap[2] += second; wrap[3] += len(ms) - half
        conv_notes = Counter(n for m in ms for n in director.split_notes(m["director_note"]))
        notes.update(conv_notes)
        note_text = "、".join(f"{director.NOTES[n][0]}×{k}" for n, k in conv_notes.items()) or "-"
        print(f"#{c['id']:>3}  {len(ms):>4}  {row['A']:>12}  {row['B']:>12}  "
              f"{row['openA']:>12}  {row['openB']:>12}  "
              f"{f'{first}/{half} → {second}/{len(ms) - half}':>18}  {note_text}")

    print("\n=== 合計 ===")
    print(f"会話数: {len(convs)}、発言数: {sum(len(c['messages']) for c in convs)}")
    print(f"質問で終わる発言: A {pct(*total['A'])}、B {pct(*total['B'])}")
    print(f"褒め・同意の言葉から書き出した発言: A {pct(opening['A'], total['A'][1])}、"
          f"B {pct(opening['B'], total['B'][1])}")
    print(f"褒め・まとめの言葉を含む発言: 前半 {pct(wrap[0], wrap[1])} → 後半 {pct(wrap[2], wrap[3])}")
    if notes:
        print("進行役の指示: " + "、".join(f"{director.NOTES[n][0]} {k}回" for n, k in notes.items()))
    else:
        print("進行役の指示: なし")
    print("\n※ 簡単な文字列の判定による目安です。会話数が少ないと、たまたまの差が大きく出ます。")


if __name__ == "__main__":
    main()
