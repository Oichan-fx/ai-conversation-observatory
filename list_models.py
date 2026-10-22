"""利用可能なGeminiモデル名を一覧表示する。

使い方: python list_models.py
表示された名前のうち1つを .env の GEMINI_MODEL= に書く。
"""
import sys

import gemini_client
from gemini_client import GeminiError


def main():
    try:
        client = gemini_client.make_client()
        names = gemini_client.list_generate_models(client)
    except GeminiError as e:
        print(e.message)
        return 1
    except Exception as e:
        # 例外の詳細には環境情報が含まれ得るため、種類だけを表示する
        print(f"モデル一覧を取得できませんでした（{type(e).__name__}）。"
              "APIキーとインターネット接続を確認してください。")
        return 1

    if not names:
        print("文章生成に使えるモデルが見つかりませんでした。")
        return 1
    print("文章生成（generateContent）に使えるモデル:")
    for name in names:
        print(f"  {name}")
    print()
    print("※ 名前に「flash」を含むモデルは、無料枠で使いやすい傾向があります。")
    print("  無料枠の対象かどうかは https://ai.google.dev/gemini-api/docs/pricing で確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
