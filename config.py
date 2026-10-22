"""アプリ全体の設定値。

APIキー・モデル名は .env（または環境変数）から読み込む。
最大発言数・文字数・履歴数・発言間隔は画面から変更でき、ここでは初期値と範囲を決める。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)

# --- サーバー（外部公開しないため 127.0.0.1 固定） ---
HOST = "127.0.0.1"
PORT = 8000

# --- 保存先 ---
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "conversations.db"

# --- 会話の既定値（画面の初期値）と、画面から設定できる範囲 ---
DEFAULT_MAX_TURNS = 10      # 最大発言数（無料枠は1モデル1日20回程度のことがあるため少なめ）
MAX_TURNS_LIMIT = 200
MAX_CHARS = 300             # 1発言の最大文字数
MAX_CHARS_RANGE = (50, 1000)
HISTORY_SIZE = 8            # 次のAIへ渡す直近発言数
HISTORY_SIZE_RANGE = (1, 20)
TURN_INTERVAL_SEC = 5       # 発言と発言の間の待機秒数
TURN_INTERVAL_RANGE = (1, 60)

# --- モデルの選択肢 ---
MODELS_CACHE_PATH = DATA_DIR / "models_cache.json"   # 「モデル一覧を更新」で取得した一覧の保存先
# 名前にこれらを含むモデルは、会話に向かないため選択肢に出さない（小文字で比較）
MODEL_EXCLUDE_WORDS = [
    "preview", "tts", "image", "audio", "live", "embedding", "transcribe", "robotics",
    "computer-use", "deep-research", "lyria", "imagen", "veo", "aqa", "nano-banana",
    "antigravity",
    "gemma",   # Gemini API の Gemma はシステム指示に対応していないことがあるため
]

# --- 使用回数の目安（画面表示用） ---
USAGE_WINDOW_HOURS = 24     # 直近何時間の呼び出しを数えるか
DAILY_LIMIT_DEFAULT = 20    # 1モデルあたりの1日の上限の目安（AI Studio の表示に合わせて変更可）
DAILY_LIMITS = {}           # モデルごとに変える場合 例: {"gemini-flash-lite-latest": 20}
# ※ 実際に「1日の上限」の429が返ったときは、その上限値がDBに記録され、こちらより優先される

# --- エクスポート ---
EXPORT_DIR = BASE_DIR / "exports"

# --- API呼び出し ---
REQUEST_TIMEOUT_SEC = 120   # 1回のAPIリクエストのタイムアウト（Gemini側にも同じ秒数が期限として渡り、超えると504）
# 一時的なエラーの再試行： エラーの種類 → (再試行の回数（初回を含まない）, 再試行までに待つ秒数)
# ここに無い種類（1日の上限・APIキー・モデル名のエラーなど）は再試行しない
RETRY_POLICY = {
    "rate_limit": (2, 30),  # 1分あたりの上限（429）
    "server": (1, 60),      # Gemini側の一時的なエラー（503など5xx。504はtimeout扱い）
    "timeout": (1, 60),     # タイムアウト（504 DEADLINE_EXCEEDED を含む。混雑時はすぐ再試行しても失敗しやすい）
    "network": (2, 5),
    "empty": (2, 5),        # 空の返答・直前と同じ発言
}

# --- 入力サイズ上限 ---
TOPIC_MAX_LEN = 500
PERSONA_MAX_LEN = 1500      # キャラクター設定から作った性格文の上限
NAME_MAX_LEN = 30
CHARACTER_ITEM_MAX_LEN = 40     # チェックボックスの1項目の長さ
CHARACTER_FREE_MAX_LEN = 200    # 各項目の自由記入欄
CHARACTER_EXTRA_MAX_LEN = 1000  # 「追加の指示」欄

# --- AIのキャラクター ---
CHARACTER_OPTIONS_PATH = BASE_DIR / "character_options.json"   # 選択肢の一覧（編集してよい）
DEFAULT_PRESETS_PATH = BASE_DIR / "presets_default.json"       # 初期プリセット（Gitで管理）
USER_PRESETS_PATH = DATA_DIR / "presets.json"                  # 自分で保存したプリセット
DEFAULT_PRESET_A = "default-curious"   # 初めて使うときの AI A（presets_default.json の id）
DEFAULT_PRESET_B = "default-cynic"

# --- 会話の導入文 ---
FREE_TALK_OPENER = (
    "初対面の2体のAIです。お互いに自己紹介し、その後は自由に雑談してください。"
    "同じ話題に固執しすぎず、自然に話を広げてください（無理に話題を変える必要はありません）。"
)
TOPIC_OPENER_TEMPLATE = (
    "テーマ：{topic}\n"
    "このテーマをきっかけに会話を始めてください。議論に限定せず、"
    "自然な雑談・質問・冗談・脱線も歓迎です。"
)


def get_api_key():
    """APIキーを返す（未設定なら None）。値そのものは画面やログに出さないこと。"""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    return key or None


def get_models():
    """(AI Aのモデル名, AI Bのモデル名) を返す。未設定の項目は None。"""
    common = os.environ.get("GEMINI_MODEL", "").strip() or None
    model_a = os.environ.get("GEMINI_MODEL_A", "").strip() or common
    model_b = os.environ.get("GEMINI_MODEL_B", "").strip() or common
    return model_a, model_b
