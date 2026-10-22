"""会話の質を保つための「進行役のひとこと」。

直近の発言から気になる傾向を見つけたら、次に話すAIのその回のシステム指示に
短い指示を追加する（会話の履歴そのものには入れない）。
APIの呼び出し回数は増やさない。判定は簡単なもので、完全に防げるわけではない。
"""
import re
import unicodedata
from difflib import SequenceMatcher

# 指示の種類 → (画面・エクスポートの印, システム指示に追加する文)
NOTES = {
    "question": (
        "質問で終えないよう指示",
        "今回は質問で終えないでください。自分の考え・体験・冗談などで締めてください。",
    ),
    "wrapup": (
        "まとめに向かわないよう指示",
        "会話をまとめたり、締めくくったり、相手を褒めたりせずに、"
        "新しい具体例や意外な話題、自分なりの別の見方を出してください。",
    ),
    "repeat": (
        "繰り返しを避けるよう指示",
        "最近の発言と似た内容になっています。今回は違う角度・違う話題で話してください。",
    ),
    "opening": (
        "褒め言葉から入らないよう指示",
        "相手を褒める・同意する言葉から入らず、自分の話から始めてください。",
    ),
}

# 相手を褒めるときによく出る言葉（丁寧語のほか、くだけた言い方・関西弁も）
PRAISE_WORDS = (
    r"素晴らしい|すばらしい|さすが(?!に)|(?<!過去)最高(?!気温|裁)|素敵|天才|名案|ナイス|"   # 「さすがに」「過去最高」「最高気温」「最高裁」は除く
    r"(?<!もの)すご[いっ]|すげ[えー]|(?<!物)凄い|"   # 「ものすごい」「物凄い」は程度の意味が多いので除く
    r"深い(です|ね|な|わ|やん|じゃん)|鋭い(です|ね|な|わ|やん|じゃん)|"
    r"ええな|ええ響き|ええやん|いいね|いいじゃん|"
    r"和む|しびれる|痺れる|神すぎ|神やん|神じゃん"
)
# 褒め合い・まとめに向かうときによく出る言葉
WRAPUP_PATTERN = re.compile(
    PRAISE_WORDS + r"|"
    r"楽しかった|楽しい(時間|会話|ひととき)|ありがとう|感謝|"
    r"まとめ(?!て)|締めくく|"   # 「まとめて」（一度にの意味・「まとめてた」）は除く
    r"結論|有意義|おっしゃる通り|その通り(です|ですね)|共感し|勉強にな"
)
# 発言の書き出しが「褒め・同意」かどうか（褒め言葉に加え、同意のあいづちと面白がる反応も含める）
# 「あはは」「ふふ」などの笑いだけの書き出しは、キャラクターらしい自然な反応なので含めない
OPENING_PATTERN = re.compile(
    PRAISE_WORDS + r"|"
    r"たしかに|確かに|わかる|分かる|わかります|分かります|それな|ほんまそれ|ほんとそれ|まさに|"
    r"なるほど|その通り|そのとおり|おっしゃる通り|同感|共感|激しく同意|"
    r"せやねん|せやな|それそれ|"
    r"ウケる|ウケた|おもろ|面白|笑える|うまいこと|上手いこと|言い得て妙"
)
OPENING_CHARS = 30           # 書き出しとして見る長さ（最初の1文がこれより長ければここまで）
SIMILARITY_THRESHOLD = 0.6   # これ以上似ていたら「繰り返し」とみなす（0〜1）
SIMILARITY_MIN_CHARS = 20    # これより短い発言は、たまたま似やすいので比べない
COOLDOWN_TURNS = 2           # 同じ指示を、そのAIの直近何回分の発言で使っていたら出さないか
# 指示ごとに間隔を変える場合（書き出しの指示は、そのAIの直前の発言で使っていなければまた出す）
COOLDOWN_BY_NOTE = {"opening": 1}


def ends_with_question(text):
    return bool(re.search(r"[？?][」』）)♪☆★笑wｗ\s]*$", text.strip()))


def has_wrapup_words(text):
    return bool(WRAPUP_PATTERN.search(text))


def opening_of(text):
    """発言の書き出し（最初の1文。長ければ OPENING_CHARS 文字まで）。"""
    first = re.split(r"[。！？!?\n]", text.strip(), maxsplit=1)[0]
    return first[:OPENING_CHARS]


def opens_with_praise(text):
    return bool(OPENING_PATTERN.search(opening_of(text)))


def _normalize(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def similarity(a, b):
    a, b = _normalize(a), _normalize(b)
    if min(len(a), len(b)) < SIMILARITY_MIN_CHARS:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def detect_notes(speaker, messages):
    """次に speaker（"A"/"B"）が話すときに追加する指示の種類のリストを返す。

    messages: これまでの発言（DBの行の dict）。system や error の行は無視する。
    """
    talk = [m for m in messages if m["speaker"] in ("A", "B") and m["status"] == "complete"]
    own = [m for m in talk if m["speaker"] == speaker]
    notes = []

    # 1. 自分の直前2回の発言がどちらも質問で終わっている
    if len(own) >= 2 and all(ends_with_question(m["content"]) for m in own[-2:]):
        notes.append("question")

    # 2. 直近3発言のうち2つ以上に、褒め言葉・まとめの言葉がある
    if len(talk) >= 3 and sum(has_wrapup_words(m["content"]) for m in talk[-3:]) >= 2:
        notes.append("wrapup")

    # 3. 自分の直前2回の発言が、どちらも褒め・同意の言葉から書き出している
    #    （2. は直近3発言を見るため、1つおきに話す本人には届きにくい。こちらは本人を見る）
    if len(own) >= 2 and all(opens_with_praise(m["content"]) for m in own[-2:]):
        notes.append("opening")

    # 4. 自分の直前の発言が、自分のその前の発言か、
    #    そのとき返事をした相手の発言（自分の直前の発言のすぐ前）とよく似ている
    if own:
        last_index = max(i for i, m in enumerate(talk) if m["speaker"] == speaker)
        last = talk[last_index]["content"]
        others = [m["content"] for m in own[-2:-1]]
        if last_index > 0 and talk[last_index - 1]["speaker"] != speaker:
            others.append(talk[last_index - 1]["content"])
        if any(similarity(last, o) >= SIMILARITY_THRESHOLD for o in others):
            notes.append("repeat")

    # 同じ指示を続けて出さない（そのAIの直近の発言で使っていたら見送る）
    def used_recently(note):
        turns = COOLDOWN_BY_NOTE.get(note, COOLDOWN_TURNS)
        return any(note in split_notes(m.get("director_note")) for m in own[-turns:])
    return [n for n in notes if not used_recently(n)]


def instructions_for(notes):
    """システム指示に追加する文。指示が無ければ空文字。"""
    if not notes:
        return ""
    lines = "\n".join(f"- {NOTES[n][1]}" for n in notes)
    return f"\n\n# 進行役からの今回の指示\n{lines}"


def split_notes(note_str):
    return [n for n in (note_str or "").split(",") if n in NOTES]


def labels(note_str):
    """DBに保存した指示の種類 → 画面・エクスポート用の印のリスト。"""
    return [NOTES[n][0] for n in split_notes(note_str)]
