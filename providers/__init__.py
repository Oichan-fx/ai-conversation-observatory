"""AIの提供会社ごとの部品をまとめる。

会社を追加するときは、このフォルダに部品（例: claude.py）を作り、下の PROVIDERS に加える。
部品には次を用意する:
  id / label / env_key        … 識別子・画面の表示名・APIキーの環境変数名
  is_configured()             … APIキーが設定済みか
  make_client()               … APIクライアントを作る（キー未設定なら GeminiError(no_api_key)）
  generate(client, model, system_instruction, contents) → gemini_client.Generation
  list_models(client)         … 会話に使えるモデル名の一覧（APIを呼ぶ）
画面のモデルの選択肢には、APIキーが設定済みの会社のモデルだけが並ぶ。
"""
import re

from . import gemini

PROVIDERS = [gemini.PROVIDER]
_BY_ID = {p.id: p for p in PROVIDERS}

MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._\-/]{1,100}$")


def get(provider_id):
    return _BY_ID.get(provider_id)


def configured():
    """APIキーが設定済みの会社の一覧。"""
    return [p for p in PROVIDERS if p.is_configured()]


def make_ref(provider_id, model):
    """画面・APIでモデルを指す文字列（例: "gemini:gemini-flash-latest"）。"""
    return f"{provider_id}:{model}"


def parse_ref(ref):
    """"gemini:モデル名" → (会社, モデル名)。不正なら None。

    会社の部分が無い古い形式（モデル名だけ）は Gemini とみなす。
    """
    if not isinstance(ref, str):
        return None
    provider_id, sep, model = ref.partition(":")
    if not sep:
        provider_id, model = gemini.PROVIDER.id, ref
    provider = get(provider_id)
    if not provider or not MODEL_NAME_PATTERN.match(model):
        return None
    return provider, model
