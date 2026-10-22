"""Google Gemini の部品。実際の呼び出しとエラー分類は gemini_client.py にある。"""
import config
import gemini_client


class GeminiProvider:
    id = "gemini"
    label = "Google Gemini"
    env_key = "GEMINI_API_KEY"

    def is_configured(self):
        return bool(config.get_api_key())

    def make_client(self):
        return gemini_client.make_client()

    def generate(self, client, model, system_instruction, contents):
        # 呼び出すたびに gemini_client から取り出す（テストで差し替えられるようにするため）
        return gemini_client.generate(client, model, system_instruction, contents)

    def list_models(self, client):
        return [m for m in gemini_client.list_generate_models(client) if is_chat_model(m)]


def is_chat_model(name):
    """会話に使えるモデルか（音声・画像・プレビュー版などを除く）。条件は config で変えられる。"""
    lower = name.lower()
    return not any(word in lower for word in config.MODEL_EXCLUDE_WORDS)


PROVIDER = GeminiProvider()
