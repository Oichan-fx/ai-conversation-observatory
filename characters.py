"""AIのキャラクター設定：入力の確認、性格文（システム指示の性格部分）の組み立て、プリセットの保存。

キャラクターの形（画面・プリセット・DBで共通）:
  {
    "name": "ハル",
    "selections": {"personality": ["好奇心旺盛"], "age": ["20代"], ...},   # チェックボックスで選んだもの
    "free": {"personality": "自由記入の文", ...},                          # 各項目の自由記入欄
    "extra": "追加の指示（自由記入）"
  }
会話の質のルール（質問で終えない・まとめない等）は、ここで作る性格文とは別に
conversation_manager.build_system_instruction が必ず付け足す。
"""
import json
import logging
import re
import unicodedata
import uuid

import config
import database

logger = logging.getLogger(__name__)

# 名前として使えない言葉（会話の履歴やログの見出しと紛らわしいため）
RESERVED_NAMES = {"進行役", "システム"}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class CharacterError(ValueError):
    """利用者向けの日本語メッセージを持つ入力エラー。"""


# ---------------------------------------------------------------------------
# 選択肢・プリセットの読み込み
# ---------------------------------------------------------------------------

def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        logger.exception("%s を読み込めませんでした", path.name)
        return default


def load_options():
    """character_options.json の項目一覧。読むたびにファイルから読み込む（編集をすぐ反映するため）。"""
    data = _read_json(config.CHARACTER_OPTIONS_PATH, {})
    fields = []
    for f in data.get("fields", []) if isinstance(data, dict) else []:
        if not isinstance(f, dict) or not f.get("key"):
            continue
        rnd = f.get("random") or [1, 1]
        fields.append({
            "key": str(f["key"]),
            "label": str(f.get("label") or f["key"]),
            "max_select": f.get("max_select"),
            "random": [int(rnd[0]), int(rnd[-1])],
            "free_text": bool(f.get("free_text", True)) and f["key"] != "name",
            "options": [str(o) for o in f.get("options", []) if str(o).strip()],
        })
    return fields


def default_presets():
    data = _read_json(config.DEFAULT_PRESETS_PATH, {})
    items = data.get("presets", []) if isinstance(data, dict) else []
    return [{"id": p["id"], "name": p["name"], "character": p["character"], "default": True}
            for p in items if isinstance(p, dict) and {"id", "name", "character"} <= set(p)]


def user_presets():
    data = _read_json(config.USER_PRESETS_PATH, {})
    items = data.get("presets", []) if isinstance(data, dict) else []
    return [{**p, "default": False} for p in items
            if isinstance(p, dict) and {"id", "name", "character"} <= set(p)]


def get_preset(preset_id):
    for p in default_presets() + user_presets():
        if p["id"] == preset_id:
            return p
    return None


def _ensure_user_presets_readable():
    """presets.json が壊れているときに上書きして中身を失わないよう、保存・削除の前に確かめる。"""
    path = config.USER_PRESETS_PATH
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
    except (OSError, ValueError):
        raise CharacterError(f"{path} を読み込めないため、保存・削除できません。"
                             "ファイルの中身を確認するか、別の場所に移してから再度お試しください。") from None


def _write_user_presets(items):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.USER_PRESETS_PATH.with_suffix(".tmp")
    payload = {"presets": [{k: v for k, v in p.items() if k != "default"} for p in items]}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(config.USER_PRESETS_PATH)


def save_preset(name, character):
    """自分のプリセットとして保存する。同じ名前があれば上書きする。戻り値: 保存したプリセット"""
    name = clean_text(name, 40, "プリセット名")
    if not name:
        raise CharacterError("プリセット名を入力してください。")
    if any(p["name"] == name for p in default_presets()):
        raise CharacterError("初期プリセットと同じ名前では保存できません。別の名前にしてください。")
    character = validate(character)
    _ensure_user_presets_readable()
    items = user_presets()
    for p in items:
        if p["name"] == name:
            p["character"] = character
            p["updated_at"] = database.now()
            saved = p
            break
    else:
        saved = {"id": "user-" + uuid.uuid4().hex[:12], "name": name, "character": character,
                 "updated_at": database.now()}
        items.append(saved)
    _write_user_presets(items)
    return {**saved, "default": False}


def delete_preset(preset_id):
    if any(p["id"] == preset_id for p in default_presets()):
        raise CharacterError("初期プリセットは削除できません。")
    _ensure_user_presets_readable()
    items = user_presets()
    remaining = [p for p in items if p["id"] != preset_id]
    if len(remaining) == len(items):
        raise CharacterError("プリセットが見つかりません。")
    _write_user_presets(remaining)


# ---------------------------------------------------------------------------
# 入力の確認と性格文の組み立て
# ---------------------------------------------------------------------------

def clean_text(value, max_len, label):
    text = _CONTROL_CHARS.sub(" ", str(value or "")).strip()
    if len(text) > max_len:
        raise CharacterError(f"{label}は{max_len}文字以内にしてください。")
    return text


def _clean_multiline(value, max_len, label):
    text = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", str(value or "")).strip()   # 改行は残す
    if len(text) > max_len:
        raise CharacterError(f"{label}は{max_len}文字以内にしてください。")
    return text


def validate(character):
    """画面から来たキャラクター設定を確認し、きれいにした dict を返す。不正なら CharacterError。"""
    if not isinstance(character, dict):
        raise CharacterError("キャラクターの設定が正しくありません。")
    fields = {f["key"]: f for f in load_options()}

    name = clean_text(character.get("name"), config.NAME_MAX_LEN, "名前")
    if not name:
        raise CharacterError("名前を入力してください。")
    if name in RESERVED_NAMES:
        raise CharacterError(f"「{name}」は名前に使えません。")

    selections = {}
    raw_sel = character.get("selections") if isinstance(character.get("selections"), dict) else {}
    for key, values in raw_sel.items():
        if key not in fields or key == "name" or not isinstance(values, list):
            continue
        label = fields[key]["label"]
        cleaned = list(dict.fromkeys(
            v for v in (clean_text(x, config.CHARACTER_ITEM_MAX_LEN, label) for x in values[:30]) if v))
        max_select = fields[key]["max_select"]
        if max_select and len(cleaned) > max_select:
            raise CharacterError(f"{label}は{max_select}つまで選べます。")
        if cleaned:
            selections[key] = cleaned

    free = {}
    raw_free = character.get("free") if isinstance(character.get("free"), dict) else {}
    for key, value in raw_free.items():
        if key in fields and key != "name":
            text = _clean_multiline(value, config.CHARACTER_FREE_MAX_LEN, f"{fields[key]['label']}の自由記入")
            if text:
                free[key] = text

    extra = _clean_multiline(character.get("extra"), config.CHARACTER_EXTRA_MAX_LEN, "追加の指示")
    result = {"name": name, "selections": selections, "free": free, "extra": extra}
    if len(build_persona(result)) > config.PERSONA_MAX_LEN:
        raise CharacterError(f"キャラクター設定が長すぎます（{config.PERSONA_MAX_LEN}文字まで）。"
                             "選ぶ項目か自由記入を減らしてください。")
    return result


def build_persona(character):
    """キャラクター設定から性格文を作る。名前と会話のルールはここには入れない
    （build_system_instruction が「あなたの名前は〜」と会話のルールを必ず付け足す）。"""
    lines = []
    for f in load_options():
        if f["key"] == "name":
            continue
        values = list(character.get("selections", {}).get(f["key"], []))
        free = character.get("free", {}).get(f["key"], "")
        if free:
            values.append(free)
        if values:
            lines.append(f"- {f['label']}：{'、'.join(values)}")
    parts = []
    if lines:
        parts.append("# あなたのキャラクター\n" + "\n".join(lines)
                     + "\nこの人物になりきって、自然に会話してください。")
    if character.get("extra"):
        parts.append(character["extra"])
    return "\n\n".join(parts) or "あなたは会話を楽しむAIです。"


# ---------------------------------------------------------------------------
# 名前の確認（A と B の組み合わせ）
# ---------------------------------------------------------------------------

def normalize_name(name):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name or "")).casefold()


def check_names(name_a, name_b):
    """A と B の名前の組み合わせを確認する。問題があれば日本語のメッセージ、無ければ None。

    - 同じ名前（全角半角・大文字小文字・空白の違いは同じとみなす）は使えない。
      話者の見分けがつかなくなり、名前の接頭辞の除去や相手の行の切り捨てが正しく動かないため。
    - 相手の呼び名（A なら「B」「AI B」）を自分の名前にはできない。
    """
    a, b = normalize_name(name_a), normalize_name(name_b)
    if a == b:
        return f"AI A と AI B に同じ名前（{name_a}）は使えません。どちらかの名前を変えてください。"
    if a in {"b", "aib"}:
        return f"AI A の名前に「{name_a}」は使えません（AI B の呼び名と紛らわしいため）。"
    if b in {"a", "aia"}:
        return f"AI B の名前に「{name_b}」は使えません（AI A の呼び名と紛らわしいため）。"
    return None
