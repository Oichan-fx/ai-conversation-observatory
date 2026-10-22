"""Gemini API（公式SDK google-genai）の呼び出しとエラー分類。

再試行はこのモジュールでは行わない（SDKの自動リトライも無効のまま）。
呼び出し側（conversation_manager）が回数と待ち時間を制御する。
"""
import logging
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import errors, types

import config

logger = logging.getLogger(__name__)


class GeminiError(Exception):
    """利用者向けの日本語メッセージを持つエラー。

    kind: no_api_key / invalid_model / auth / rate_limit / daily_quota / timeout /
          network / server / empty / bad_request / unknown
    （daily_quota は1日の上限。再試行しても解除まで必ず失敗するため再試行しない）
    """

    def __init__(self, kind, message, quota_limit=None, http_status=None,
                 retry_seconds=None, quota_id=None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.quota_limit = quota_limit      # daily_quota のとき、APIが示した1日の上限回数
        self.http_status = http_status      # APIが返したHTTPステータス（503・429など）
        self.retry_seconds = retry_seconds  # 429のとき、APIが示した解除（再試行可能）までの秒数
        self.quota_id = quota_id            # 429のとき、当たった上限の種類（quotaId）

    @property
    def retryable(self):
        return self.kind in config.RETRY_POLICY


def make_client():
    api_key = config.get_api_key()
    if not api_key:
        raise GeminiError(
            "no_api_key",
            "Gemini APIキーが設定されていません。README の「APIキーの設定」を参照してください。",
        )
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=config.REQUEST_TIMEOUT_SEC * 1000),
    )


@dataclass
class Generation:
    """生成結果。finish_reason は "STOP"（通常）や "MAX_TOKENS"（出力上限で途中終了）など。"""
    text: str
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None   # 思考（thinking）に使ったトークンも含む
    total_tokens: int | None = None


def generate(client, model, system_instruction, contents):
    """1発言を生成して Generation を返す。失敗時は GeminiError を送出する。

    contents: [(role, text), ...]  role は "user" または "model"
    """
    try:
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Content(role=role, parts=[types.Part.from_text(text=text)])
                for role, text in contents
            ],
            config=types.GenerateContentConfig(system_instruction=system_instruction),
        )
    except errors.APIError as e:
        raise _classify_api_error(e, model) from None
    except httpx.TimeoutException:
        raise GeminiError(
            "timeout",
            f"APIの応答がタイムアウトしました（{config.REQUEST_TIMEOUT_SEC}秒）。",
        ) from None
    except httpx.TransportError:
        raise GeminiError(
            "network", "ネットワークに接続できませんでした。インターネット接続を確認してください。"
        ) from None

    text = (response.text or "").strip()
    # 未知の値は enum ではなく文字列で返ることがあるため getattr で名前を取る
    finish_reason = None
    if response.candidates and response.candidates[0].finish_reason:
        r = response.candidates[0].finish_reason
        finish_reason = str(getattr(r, "name", r))
    if finish_reason and finish_reason != "STOP":
        # 本文は記録せず、終了理由と文字数だけを残す
        logger.warning("生成が通常以外の理由で終了しました: finish_reason=%s、モデル=%s、%d字",
                       finish_reason, model, len(text))

    if not text:
        reason = ""
        if finish_reason:
            reason = f"（終了理由: {finish_reason}）"
        elif response.prompt_feedback and response.prompt_feedback.block_reason:
            r = response.prompt_feedback.block_reason
            reason = f"（ブロック理由: {getattr(r, 'name', r)}）"
        raise GeminiError("empty", f"APIから空の返答が返されました{reason}。")

    usage = response.usage_metadata
    output_tokens = None
    if usage and (usage.candidates_token_count is not None or usage.thoughts_token_count is not None):
        output_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
    return Generation(
        text=text,
        finish_reason=finish_reason,
        prompt_tokens=usage.prompt_token_count if usage else None,
        output_tokens=output_tokens,
        total_tokens=usage.total_token_count if usage else None,
    )


def _classify_api_error(e, model):
    # APIの応答本文はそのまま表示しない（秘密情報が混ざる可能性を避けるため）。
    # コードとステータス名だけをログ・画面に出す。
    code = e.code
    label = f"{code} {e.status or ''}".strip()
    logger.warning("Gemini API error: %s", label)
    message_lower = (e.message or "").lower()

    def error(kind, message, **kw):
        return GeminiError(kind, message, http_status=code, **kw)

    if code == 429:
        quota = _quota_info(e.details)
        if quota["per_day"]:
            limit = f"（無料枠：1日{quota['limit']}回）" if quota["limit"] else ""
            try:
                quota_limit = int(quota["limit"]) if quota["limit"] else None
            except ValueError:
                quota_limit = None
            return error(
                "daily_quota",
                f"このモデル（{model}）の1日の上限に達しました{limit}。"
                f"{_format_reset(quota['retry_seconds'])}"
                "すぐに続けたい場合は、プルダウンで残り回数のある別のモデルを選び、新しく「会話開始」してください。",
                quota_limit=quota_limit,
                retry_seconds=quota["retry_seconds"],
                quota_id=quota["quota_id"],
            )
        return error(
            "rate_limit",
            f"APIのレート制限または無料枠の上限に達しました（{label}）。"
            "発言間隔を長くするか、時間を置いてから再度お試しください。",
            retry_seconds=quota["retry_seconds"],
            quota_id=quota["quota_id"],
        )
    if code == 404 or (code == 400 and "model" in message_lower and "not found" in message_lower):
        return error(
            "invalid_model",
            f"モデル名「{model}」が見つからないか、利用できません（{label}）。"
            "プルダウンで別のモデルを選び、新しく「会話開始」してください"
            "（一覧が古い場合は「モデル一覧を更新」を押してください）。",
        )
    if code in (401, 403) or (code == 400 and "api key" in message_lower):
        return error(
            "auth",
            f"APIキーが無効か、権限がありません（{label}）。.env のAPIキーを確認してください。",
        )
    if code == 504:
        return error("timeout", f"APIの応答がタイムアウトしました（{label}）。")
    if isinstance(e, errors.ServerError):
        return error("server", f"Gemini API側で一時的なエラーが発生しました（{label}）。")
    if code == 400:
        return error("bad_request", f"APIへのリクエストが拒否されました（{label}）。")
    return error("unknown", f"APIでエラーが発生しました（{label}）。")


def _quota_info(details):
    """429の応答本文から、上限の種類（1日か）・上限値・解除までの秒数を取り出す。

    想定する形: {"error": {"details": [
        {"@type": "...QuotaFailure", "violations": [{"quotaId": "...PerDay...", "quotaValue": "20"}]},
        {"@type": "...RetryInfo", "retryDelay": "33403s"}]}}
    形が違っても例外にせず、分からない項目は None / False にする。
    """
    info = {"per_day": False, "limit": None, "retry_seconds": None, "quota_id": None}
    if isinstance(details, list) and details:
        details = details[0]
    if not isinstance(details, dict):
        return info
    error = details.get("error", details)
    items = error.get("details") if isinstance(error, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            for v in item.get("violations") or []:
                if not isinstance(v, dict):
                    continue
                quota_id = str(v.get("quotaId", ""))
                info["quota_id"] = info["quota_id"] or quota_id or None
                if "perday" in quota_id.lower():
                    info["per_day"] = True
                    info["quota_id"] = quota_id
                    info["limit"] = v.get("quotaValue") or info["limit"]
        elif kind.endswith("RetryInfo"):
            delay = str(item.get("retryDelay", ""))
            try:
                info["retry_seconds"] = float(delay.rstrip("s"))
            except ValueError:
                pass
    return info


def _format_reset(seconds):
    if seconds is None:
        return "解除の時刻は https://ai.dev/rate-limit で確認してください。"
    if seconds < 3600:
        return f"解除まで約{max(1, round(seconds / 60))}分です。"
    return f"解除まで約{round(seconds / 3600)}時間です。"


def list_generate_models(client):
    """generateContent に対応するモデル名の一覧を返す。"""
    names = []
    for m in client.models.list():
        if m.supported_actions and "generateContent" in m.supported_actions:
            names.append(m.name.removeprefix("models/"))
    return sorted(names)
