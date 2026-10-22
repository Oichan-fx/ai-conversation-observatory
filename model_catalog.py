"""モデルの選択肢をまとめる。

- 画面を開いただけではAPIを呼ばない。保存済みの一覧（data/models_cache.json）と、
  .env の初期値・過去に使ったモデルから選択肢を作る。
- 「モデル一覧を更新」を押したときだけ、APIキーが設定済みの会社からモデル一覧を取得して保存する。
"""
import json
import logging

import config
import database
import providers
from providers import gemini

logger = logging.getLogger(__name__)


def load_cache():
    try:
        with open(config.MODELS_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(data):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.MODELS_CACHE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(config.MODELS_CACHE_PATH)


def refresh():
    """設定済みの会社からモデル一覧を取得して保存する（APIを呼ぶ）。

    戻り値: {会社ID: エラー文 または None}
    """
    cache = load_cache()
    results = {}
    for p in providers.configured():
        try:
            names = p.list_models(p.make_client())
            cache[p.id] = {"updated_at": database.now(), "models": names}
            results[p.id] = None
        except Exception as e:  # 一覧が取れなくても、保存済みの一覧はそのまま使う
            logger.warning("%s のモデル一覧を取得できませんでした: %s", p.label, type(e).__name__)
            results[p.id] = getattr(e, "message", None) or "モデル一覧を取得できませんでした。"
    save_cache(cache)
    return results


def default_refs():
    """(AI A の初期値, AI B の初期値)。.env の設定を使い、無ければ None。"""
    if not gemini.PROVIDER.is_configured():
        return None, None
    model_a, model_b = config.get_models()
    return (providers.make_ref("gemini", model_a) if model_a else None,
            providers.make_ref("gemini", model_b) if model_b else None)


def choices():
    """画面のプルダウン用。APIキーが設定済みの会社ごとに、モデル名の一覧を返す。"""
    cache = load_cache()
    used = database.used_models()
    defaults = [r for r in default_refs() if r]
    result = []
    for p in providers.configured():
        names = list(cache.get(p.id, {}).get("models", []))
        # 初期値（.env）と過去に使ったモデルは、一覧に無くても選べるようにする
        names += [providers.parse_ref(r)[1] for r in defaults if r.startswith(p.id + ":")]
        names += [m for (pid, m) in used if pid == p.id]
        result.append({
            "id": p.id,
            "label": p.label,
            "updated_at": cache.get(p.id, {}).get("updated_at"),
            "models": sorted(dict.fromkeys(names)),
        })
    return result
