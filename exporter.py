"""会話ログを Markdown / テキストに書き出す。"""
import re

import config
import director

MODE_LABELS = {"free": "フリートーク", "topic": "テーマ指定"}
STATUS_LABELS = {
    "running": "会話中",
    "paused": "一時停止中",
    "completed": "完了（最大発言数に到達）",
    "stopped": "終了（終了ボタン）",
    "error": "エラーで停止",
    "interrupted": "中断（アプリ終了時に進行中だった）",
}

# Windowsのファイル名に使えない文字と制御文字
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                   *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename_part(text, max_len=40):
    """ファイル名の一部として安全な文字列にする（使えない文字は「_」に置き換える）。"""
    text = _INVALID_CHARS.sub("_", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"_[_ ]*_|_ | _", "_", text)   # 置き換えた「_」が並ぶのを1つにまとめる
    text = text[:max_len].rstrip(" .")       # Windowsは末尾の空白・ピリオドを許さない
    if text.split(".")[0].upper() in _RESERVED_NAMES:
        text = "_" + text
    return text


def make_filename(conv, ext):
    stamp = conv["created_at"].replace("-", "").replace(":", "").replace("T", "-")
    label = conv["topic"] if conv["mode"] == "topic" and conv["topic"] else MODE_LABELS["free"]
    label = sanitize_filename_part(label) or "会話"
    return f"{stamp}_会話{conv['id']}_{label}.{ext}"


def _speaker_name(conv, speaker):
    return {"A": conv["name_a"], "B": conv["name_b"]}.get(speaker, "システム")


def _settings(conv):
    def v(x, unit=""):
        return f"{x}{unit}" if x is not None else "（記録なし）"
    return [
        ("開始日時", conv["created_at"].replace("T", " ")),
        ("終了日時", conv["ended_at"].replace("T", " ") if conv["ended_at"] else "（未終了）"),
        ("状態", STATUS_LABELS.get(conv["status"], conv["status"])),
        ("モード", MODE_LABELS.get(conv["mode"], conv["mode"])),
        ("テーマ", conv["topic"] or "（なし）"),
        ("最大発言数", v(conv["max_turns"])),
        ("1発言の最大文字数", v(conv["max_chars"], "字")),
        ("履歴として渡す発言数", v(conv["history_size"])),
        ("発言間隔", v(conv["interval_sec"], "秒")),
    ]


def to_markdown(conv, messages):
    lines = [f"# AI会話観察室 会話ログ #{conv['id']}", ""]
    lines += [f"- **{k}**: {val}" for k, val in _settings(conv)]
    lines.append("")
    for key in ("a", "b"):
        lines += [
            f"## {conv['name_' + key]}（モデル: {conv['model_' + key]}）",
            "",
            *[f"> {line}" for line in conv["persona_" + key].splitlines()],
            "",
        ]
    lines += ["## 会話", ""]
    for m in messages:
        time = m["created_at"].replace("T", " ")
        if m["speaker"] == "system":
            lines.append(f"### ⚠ システム（{time}）")
        else:
            lines.append(f"### #{m['turn_number']} {_speaker_name(conv, m['speaker'])}（{time}）")
        lines += ["", m["content"], ""]
        if notes := director.labels(m.get("director_note")):
            lines += [f"<small>進行役：{'、'.join(notes)}</small>", ""]
    return "\n".join(lines)


def to_text(conv, messages):
    lines = [f"AI会話観察室 会話ログ #{conv['id']}", "=" * 40]
    lines += [f"{k}: {val}" for k, val in _settings(conv)]
    for key in ("a", "b"):
        lines += ["", f"[{conv['name_' + key]}] モデル: {conv['model_' + key]}",
                  conv["persona_" + key]]
    lines += ["", "=" * 40, ""]
    for m in messages:
        time = m["created_at"].replace("T", " ")
        if m["speaker"] == "system":
            lines.append(f"[システム] {time}")
        else:
            lines.append(f"#{m['turn_number']} {_speaker_name(conv, m['speaker'])}  {time}")
        lines.append(m["content"])
        if notes := director.labels(m.get("director_note")):
            lines.append(f"  （進行役：{'、'.join(notes)}）")
        lines.append("")
    return "\n".join(lines)


def export(conv, messages, fmt):
    """exports フォルダにUTF-8で保存し、保存先のパスを返す。fmt は "md" または "txt"。"""
    body = to_markdown(conv, messages) if fmt == "md" else to_text(conv, messages)
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.EXPORT_DIR / make_filename(conv, fmt)
    # Windowsのメモ帳でも読みやすいよう、改行はCRLFで保存する
    path.write_text(body + "\n", encoding="utf-8", newline="\r\n")
    return path
