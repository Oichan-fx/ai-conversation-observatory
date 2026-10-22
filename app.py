"""AI会話観察室 — Flaskアプリ本体。

起動: python app.py  →  ブラウザで http://127.0.0.1:8000 を開く
"""
import json
import logging
import queue
from datetime import datetime, timedelta

from flask import Flask, Response, jsonify, render_template, request, send_file

import characters
import config
import database
import director
import exporter
import model_catalog
import providers
from conversation_manager import (TERMINAL_STATUSES, ConversationManager, Persona,
                                  build_system_instruction)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
manager = ConversationManager()
NOTE_LABELS = {key: label for key, (label, _text) in director.NOTES.items()}


def setup_problems():
    """会話を始められない設定上の問題を、コード（no_api_key / no_model）のリストで返す。"""
    if not providers.configured():
        return ["no_api_key"]
    if not any(p["models"] for p in model_catalog.choices()):
        return ["no_model"]
    return []


@app.get("/")
def index():
    default_a, default_b = model_catalog.default_refs()
    return render_template(
        "index.html",
        problems=setup_problems(),
        default_model_a=default_a,
        default_model_b=default_b,
        default_max_turns=config.DEFAULT_MAX_TURNS,
        max_turns_limit=config.MAX_TURNS_LIMIT,
        max_chars=config.MAX_CHARS,
        max_chars_range=config.MAX_CHARS_RANGE,
        history_size=config.HISTORY_SIZE,
        history_range=config.HISTORY_SIZE_RANGE,
        interval=config.TURN_INTERVAL_SEC,
        interval_range=config.TURN_INTERVAL_RANGE,
        topic_max_len=config.TOPIC_MAX_LEN,
        db_path=str(config.DB_PATH),
        usage_hours=config.USAGE_WINDOW_HOURS,
        note_labels=NOTE_LABELS,
    )


@app.get("/history")
def history():
    return render_template("history.html", export_dir=str(config.EXPORT_DIR),
                           note_labels=NOTE_LABELS)


# ---------------------------------------------------------------------------
# 会話の操作
# ---------------------------------------------------------------------------

@app.get("/api/current")
def current():
    """進行中（または直前）の会話の状態。ページ再読み込み時の再接続に使う。"""
    r = manager.current
    return jsonify(r.state() if r else None)


def _int_in_range(data, key, default, low, high, label):
    """data[key] を整数として取り出し、範囲外ならエラー文を返す。戻り値: (値, エラー文)"""
    try:
        value = int(data.get(key, default))
    except (TypeError, ValueError):
        return None, f"{label}は数値で入力してください。"
    if not low <= value <= high:
        return None, f"{label}は{low}〜{high}で入力してください。"
    return value, None


@app.post("/api/conversations")
def start_conversation():
    if setup_problems():
        return jsonify(error="APIキーまたはモデル名が設定されていません。画面上部の案内を確認してください。"), 400

    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode not in ("free", "topic"):
        return jsonify(error="会話モードが不正です。"), 400
    topic = (data.get("topic") or "").strip()
    if len(topic) > config.TOPIC_MAX_LEN:
        return jsonify(error=f"テーマは{config.TOPIC_MAX_LEN}文字以内で入力してください。"), 400
    if mode == "topic" and not topic:
        return jsonify(error="テーマ指定モードでは、テーマを入力してください。"), 400
    if mode == "free":
        topic = ""

    values = {}
    for key, default, (low, high), label in [
        ("max_turns", config.DEFAULT_MAX_TURNS, (1, config.MAX_TURNS_LIMIT), "最大発言数"),
        ("max_chars", config.MAX_CHARS, config.MAX_CHARS_RANGE, "1発言の最大文字数"),
        ("history_size", config.HISTORY_SIZE, config.HISTORY_SIZE_RANGE, "履歴として渡す発言数"),
        ("interval_sec", config.TURN_INTERVAL_SEC, config.TURN_INTERVAL_RANGE, "発言間隔"),
    ]:
        values[key], error = _int_in_range(data, key, default, low, high, label)
        if error:
            return jsonify(error=error), 400

    models = {}
    for key, default in zip(("a", "b"), model_catalog.default_refs()):
        parsed = providers.parse_ref(data.get(f"model_{key}") or default)
        if not parsed:
            return jsonify(error=f"AI {key.upper()} のモデルを選んでください。"), 400
        provider, model = parsed
        if not provider.is_configured():
            return jsonify(error=f"{provider.label} のAPIキーが設定されていません。"), 400
        models[key] = (provider.id, model)

    chars = {}
    for key, default in zip(("a", "b"), initial_characters()):
        try:
            chars[key] = characters.validate(data.get(f"character_{key}") or default)
        except characters.CharacterError as e:
            return jsonify(error=f"AI {key.upper()} のキャラクター設定：{e}"), 400
    name_error = characters.check_names(chars["a"]["name"], chars["b"]["name"])
    if name_error:
        return jsonify(error=name_error), 400

    persona_a = Persona("A", chars["a"]["name"], characters.build_persona(chars["a"]),
                        models["a"][1], models["a"][0])
    persona_b = Persona("B", chars["b"]["name"], characters.build_persona(chars["b"]),
                        models["b"][1], models["b"][0])
    try:
        runner = manager.start(mode, topic or None, persona_a, persona_b, **values,
                               character_a=chars["a"], character_b=chars["b"])
    except RuntimeError as e:
        return jsonify(error=str(e)), 409
    except Exception:
        logger.exception("会話の開始に失敗しました")
        return jsonify(error="会話を開始できませんでした（ログ保存先のエラーの可能性があります）。"), 500
    return jsonify(runner.state())


def _running_or_404(conversation_id):
    runner = manager.get(conversation_id)
    if not runner:
        return None, (jsonify(error="この会話は進行していません。"), 404)
    return runner, None


@app.post("/api/conversations/<int:conversation_id>/pause")
def pause_conversation(conversation_id):
    runner, err = _running_or_404(conversation_id)
    if err:
        return err
    runner.pause()
    return jsonify(runner.state())


@app.post("/api/conversations/<int:conversation_id>/resume")
def resume_conversation(conversation_id):
    if setup_problems():
        return jsonify(error="APIキーまたはモデル名が設定されていません。"), 400
    try:
        runner = manager.resume(conversation_id)
    except RuntimeError as e:
        return jsonify(error=str(e)), 409
    return jsonify(runner.state())


@app.post("/api/conversations/<int:conversation_id>/stop")
def stop_conversation(conversation_id):
    runner, err = _running_or_404(conversation_id)
    if err:
        return err
    runner.stop()
    return jsonify(runner.state())


@app.get("/api/conversations/<int:conversation_id>/events")
def conversation_events(conversation_id):
    """SSE: 最初に保存済みの全発言と現在の状態を送り、その後は更新を逐次送る。"""
    runner, err = _running_or_404(conversation_id)
    if err:
        return err

    def stream():
        q = runner.subscribe()  # 取りこぼしを防ぐため、スナップショットより先に購読する
        try:
            yield _sse({"type": "snapshot", "messages": database.get_messages(conversation_id)})
            yield _sse(runner.state())
            if runner.status in TERMINAL_STATUSES:
                return
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)
                if event.get("type") == "status" and event["status"] in TERMINAL_STATUSES:
                    return
        finally:
            runner.unsubscribe(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# キャラクター設定・プリセット
# ---------------------------------------------------------------------------

def initial_characters():
    """画面を開いたときの A・B のキャラクター：前回の会話の設定、無ければ初期プリセット。"""
    last_a, last_b = database.last_characters()
    if last_a and last_b:
        return last_a, last_b
    preset_a = characters.get_preset(config.DEFAULT_PRESET_A)
    preset_b = characters.get_preset(config.DEFAULT_PRESET_B)
    return preset_a["character"], preset_b["character"]


@app.get("/api/characters")
def character_setup():
    """キャラクター設定パネルに必要なもの：選択肢・プリセット・A/B の初期値。"""
    char_a, char_b = initial_characters()
    return jsonify(
        fields=characters.load_options(),
        presets=characters.default_presets() + characters.user_presets(),
        character_a=char_a,
        character_b=char_b,
    )


@app.post("/api/characters/preview")
def character_preview():
    """パネルのプレビュー：キャラクター設定から作った性格文と、必ず付け足される会話のルール。"""
    data = request.get_json(silent=True) or {}
    key = "B" if data.get("key") == "B" else "A"
    try:
        char = characters.validate(data.get("character"))
    except characters.CharacterError as e:
        return jsonify(error=str(e)), 400
    other_name = characters.clean_text(data.get("other_name"), config.NAME_MAX_LEN, "相手の名前") or "相手"
    persona = characters.build_persona(char)
    me = Persona(key, char["name"], persona, "")
    other = Persona("B" if key == "A" else "A", other_name, "", "")
    max_chars, _error = _int_in_range(data, "max_chars", config.MAX_CHARS, *config.MAX_CHARS_RANGE, "文字数")
    full = build_system_instruction(me, other, max_chars or config.MAX_CHARS)
    return jsonify(persona=persona, rules=full[len(persona):].lstrip("\n"),
                   name_error=characters.check_names(*((char["name"], other_name) if key == "A"
                                                       else (other_name, char["name"]))))


@app.get("/api/presets")
def list_presets():
    return jsonify(presets=characters.default_presets() + characters.user_presets())


@app.post("/api/presets")
def save_preset():
    data = request.get_json(silent=True) or {}
    try:
        saved = characters.save_preset(data.get("name"), data.get("character"))
    except characters.CharacterError as e:
        return jsonify(error=str(e)), 400
    return jsonify(saved=saved, presets=characters.default_presets() + characters.user_presets())


@app.delete("/api/presets/<preset_id>")
def delete_preset(preset_id):
    try:
        characters.delete_preset(preset_id)
    except characters.CharacterError as e:
        return jsonify(error=str(e)), 400
    return jsonify(presets=characters.default_presets() + characters.user_presets())


# ---------------------------------------------------------------------------
# 使用回数（目安）
# ---------------------------------------------------------------------------

def _daily_limit(model, recorded_limits):
    return recorded_limits.get(model) or config.DAILY_LIMITS.get(model) or config.DAILY_LIMIT_DEFAULT


@app.get("/api/usage")
def usage():
    """直近24時間のモデルごとの使用回数（このアプリの記録から数えた目安）。

    ?a=gemini:モデル名&b=... で、画面で選んでいるモデルを指定する（使用0回でも表示するため）。
    """
    default_a, default_b = model_catalog.default_refs()
    refs = [request.args.get("a") or default_a, request.args.get("b") or default_b]
    names = [parsed[1] if (parsed := providers.parse_ref(r)) else None for r in refs]
    rows = {r["model"]: r for r in database.usage_since(config.USAGE_WINDOW_HOURS)}
    models = [m for m in dict.fromkeys([*names, *rows]) if m]
    return jsonify(window_hours=config.USAGE_WINDOW_HOURS, model_a=names[0], model_b=names[1],
                   models=_usage_items(models))


@app.get("/api/models")
def model_choices():
    """モデルの選択肢（APIキーが設定済みの会社のみ）と、各モデルの残り回数の目安。APIは呼ばない。"""
    groups = model_catalog.choices()
    names = [m for g in groups for m in g["models"]]
    usage_by_model = {u["model"]: u for u in _usage_items(names)}
    for g in groups:
        g["models"] = [
            {"ref": providers.make_ref(g["id"], m), "name": m,
             "remaining": usage_by_model[m]["remaining"], "limit": usage_by_model[m]["limit"]}
            for m in g["models"]
        ]
    default_a, default_b = model_catalog.default_refs()
    return jsonify(providers=groups, default_a=default_a, default_b=default_b)


@app.post("/api/models/refresh")
def refresh_models():
    """「モデル一覧を更新」：設定済みの会社からモデル一覧を取得して保存する（APIを呼ぶ）。"""
    errors = {pid: err for pid, err in model_catalog.refresh().items() if err}
    if errors:
        return jsonify(error=" ".join(errors.values())), 502
    return model_choices()


def _usage_items(models):
    hours = config.USAGE_WINDOW_HOURS
    rows = {r["model"]: r for r in database.usage_since(hours)}
    limits = database.get_model_limits()
    result = []
    for model in models:
        row = rows.get(model, {"calls": 0, "tokens": 0, "oldest": None})
        limit = _daily_limit(model, limits)
        item = {
            "model": model,
            "calls": row["calls"],
            "tokens": row["tokens"],
            "limit": limit,
            "remaining": max(0, limit - row["calls"]),
            "next_free_at": None,
            # 1日の上限の429でAPIが示した解除予定時刻（まだ先のものだけ。数え方には使わない）
            "api_reset_at": database.latest_daily_reset(model),
        }
        # 直近24時間で数えるため、古い呼び出しが24時間を過ぎると枠が空く（目安）。
        # 上限を超えている分も含めて、残りが1回になる時刻を求める。
        if row["calls"] >= limit:
            nth = database.nth_oldest_call_since(model, hours, row["calls"] - limit + 1)
            if nth:
                free_at = datetime.fromisoformat(nth) + timedelta(hours=hours)
                item["next_free_at"] = free_at.isoformat(timespec="minutes")
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# 過去ログ
# ---------------------------------------------------------------------------

@app.get("/api/conversations")
def list_conversations():
    return jsonify(database.list_conversations())


@app.get("/api/conversations/<int:conversation_id>")
def conversation_detail(conversation_id):
    conv = database.get_conversation(conversation_id)
    if not conv:
        return jsonify(error="会話が見つかりません。"), 404
    return jsonify(conversation=conv, messages=database.get_messages(conversation_id))


@app.get("/api/conversations/<int:conversation_id>/export")
def export_conversation(conversation_id):
    fmt = request.args.get("format", "md")
    if fmt not in ("md", "txt"):
        return jsonify(error="形式は md または txt を指定してください。"), 400
    conv = database.get_conversation(conversation_id)
    if not conv:
        return jsonify(error="会話が見つかりません。"), 404
    try:
        path = exporter.export(conv, database.get_messages(conversation_id), fmt)
    except OSError:
        logger.exception("エクスポートの保存に失敗しました")
        return jsonify(error="エクスポートファイルを保存できませんでした。"), 500
    mimetype = "text/markdown" if fmt == "md" else "text/plain"
    return send_file(path, as_attachment=True, download_name=path.name,
                     mimetype=f"{mimetype}; charset=utf-8")


@app.errorhandler(500)
def internal_error(_e):
    return jsonify(error="サーバー内部でエラーが発生しました。"), 500


if __name__ == "__main__":
    interrupted = database.init_db()
    if interrupted:
        print(f"前回の終了時に進行中だった会話 {interrupted} 件を「中断」として記録しました。")
    print(f"AI会話観察室を起動します: http://{config.HOST}:{config.PORT}")
    print("停止するには、このウィンドウで Ctrl + C を押してください。")
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)
