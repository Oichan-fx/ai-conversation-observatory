"""会話の進行管理。

- 会話ごとにバックグラウンドスレッドを1本動かす（同時に動かす会話は1つまで）。
- 発言は「生成 → DB保存 → 画面へ通知」の順で処理し、画面とログを一致させる。
- 発言間の待機は Condition.wait で行い、待機中の一時停止・終了を即座に受け付ける。
- 会話の設定（AI・文字数・履歴数・間隔）はDBの会話レコードから読み込む。
  そのため、エラーで止まった会話も同じ会話IDのまま続きから再開できる。
"""
import logging
import queue
import re
import threading
import time
import unicodedata
from dataclasses import dataclass

import config
import database
import director
import gemini_client
import providers
from gemini_client import GeminiError

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"completed", "stopped", "error", "interrupted"}

# .env の修正とアプリの再起動が必要なエラー（「再開」では直らない）
NEEDS_RESTART = {"no_api_key", "invalid_model", "auth"}


@dataclass
class Persona:
    key: str            # "A" / "B"
    name: str
    instruction: str
    model: str
    provider: str = "gemini"   # providers の会社ID


# ---------------------------------------------------------------------------
# 履歴の組み立て（指示書 5.1）
# ---------------------------------------------------------------------------

def build_system_instruction(me, other, max_chars, notes=()):
    """notes: その回だけ追加する進行役の指示の種類（director.NOTES のキー）"""
    return (
        f"{me.instruction}\n\n"
        "# 会話のルール\n"
        f"- あなたの名前は「{me.name}」です。会話の相手は別のAI「{other.name}」です。\n"
        f"- user として渡されるのは、進行役の説明と、相手「{other.name}」の発言です。"
        "model として渡されるのは、あなた自身の過去の発言です。\n"
        f"- 自分の発言だけを出力してください。「{me.name}：」のような名前の接頭辞は付けないでください。"
        "相手のセリフを代わりに書かないでください。\n"
        f"- 日本語で自然に話し、1回の発言は{max_chars}文字以内にしてください。\n"
        "- 毎回質問で終える必要はありません。自分の意見・体験・冗談・感想で終えてかまいません。\n"
        "- この会話に終わりはありません。話をまとめたり、締めくくったり、"
        "お礼や別れの挨拶をしたりしないでください。\n"
        "- 相手を褒めるのはほどほどにして、相手に同意するだけでなく、自分なりの見方や具体的な話を添えてください。\n"
        "- 相手の口調や方言をまねせず、自分のキャラクターの口調を最後まで保ってください。"
        + director.instructions_for(notes)
    )


def build_contents(me, other, opener, messages, history_size):
    """発言するAI（me）の視点で、APIへ渡す [(role, text), ...] を組み立てる。

    - 先頭は必ず user（導入文またはテーマ）。
    - 自分の過去発言は model、相手の発言は user。
    - user 側には見出し（【進行役】【相手名】）を付け、model 側には付けない。
    - 同じロールが連続した場合は1つにまとめる。
    """
    items = [("user", f"【進行役】\n{opener}")]
    recent = [m for m in messages if m["speaker"] in ("A", "B") and m["status"] == "complete"]
    recent = recent[-history_size:]
    for m in recent:
        if m["speaker"] == me.key:
            items.append(("model", m["content"]))
        else:
            items.append(("user", f"【{other.name}】\n{m['content']}"))

    merged = []
    for role, text in items:
        if merged and merged[-1][0] == role:
            merged[-1] = (role, merged[-1][1] + "\n\n" + text)
        else:
            merged.append((role, text))

    # 交互発言なら最後は必ず相手（user）になるが、念のため保証する
    if merged[-1][0] != "user":
        merged.append(("user", "【進行役】\n会話を続けてください。"))
    return merged


def make_opener(mode, topic):
    if mode == "topic":
        return config.TOPIC_OPENER_TEMPLATE.format(topic=topic)
    return config.FREE_TALK_OPENER


# ---------------------------------------------------------------------------
# 生成結果の後処理
# ---------------------------------------------------------------------------

def _label_pattern(names):
    alts = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True) if n)
    # 例:「B：」「AI A:」「【AI B】」「[AI A]：」「**AI A**：」
    return (
        r"(?:\*\*)?\s*(?:【\s*(?:" + alts + r")\s*】|[\[［]\s*(?:" + alts + r")\s*[\]］]"
        r"|(?:" + alts + r"))\s*(?:\*\*)?\s*[：:]?"
    )


SENTENCE_END = "。！？!?"
_OPEN_TO_CLOSE = {"「": "」", "『": "』", "（": "）", "(": ")", "【": "】"}
_CLOSE = set(_OPEN_TO_CLOSE.values())

# 後処理の記録用（サーバーログに出す名前）
POSTPROCESS_LABELS = {
    "prefix_removed": "先頭の話者名を除去",
    "other_speaker_cut": "相手の名前の行以降を切り捨て",
    "max_tokens": "出力トークン上限で途中終了",
    "length_cut": "最大文字数で切り詰め",
    "sentence_trimmed": "途中で終わった文を除去",
    "no_sentence_end": "文末が見つからず省略記号で終了",
}


def sentence_end_positions(text):
    """括弧の外で文が終わる位置（その文字の直後の添字）の一覧を返す。

    - 「。」「！」「？」が続く場合（例:「！？」）は最後の1文字の後を文末とする。
    - 括弧の中の「？」などは文末としない（例: あなたが「今日の天気は？」と聞いたら）。
    - ただし「〜！」のように括弧で閉じて、その後が空白・改行・終わりなら文末とする。
    """
    positions = []
    depth = 0
    for i, ch in enumerate(text):
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch in _OPEN_TO_CLOSE:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
            if depth == 0 and i > 0 and text[i - 1] in SENTENCE_END and (not nxt or nxt.isspace()):
                positions.append(i + 1)
        # 注意: 空文字列は常に `in` が真になるため、末尾（nxt == ""）は別に判定する
        elif ch in SENTENCE_END and depth == 0 and (not nxt or nxt not in SENTENCE_END):
            positions.append(i + 1)
    return positions


def trim_to_sentence_end(text):
    """最後の完結した文までで切る。文末が1つも無ければ None。"""
    text = text.rstrip()
    positions = sentence_end_positions(text)
    if not positions:
        return None
    return text[:positions[-1]]


def clean_output(text, me, other):
    """先頭の話者名の接頭辞を除き、相手のセリフを書き始めた箇所以降を切り捨てる。

    戻り値: (処理後の本文, 行った処理の一覧)
    """
    events = []
    names = [me.name, other.name, me.key, other.key, f"AI {me.key}", f"AI {other.key}"]
    label = _label_pattern(names)

    # 先頭の接頭辞（「AI A：」「【AI A】」など）を除去。コロンも括弧も無い単語は除去しない。
    prefix = re.compile(r"^\s*(?:" + label + r")\s*", re.UNICODE)
    for _ in range(2):
        m = prefix.match(text)
        if not m or not re.search(r"[：:】\]］]", m.group(0)):
            break
        text = text[m.end():]
        events.append("prefix_removed")

    # 途中の行で相手の名前ラベルが始まっていたら、そこから先は相手のセリフとみなして切る
    other_names = [other.name, other.key, f"AI {other.key}"]
    other_label = re.compile(
        r"^\s*(?:\*\*)?\s*(?:【\s*(?:" + "|".join(re.escape(n) for n in other_names) + r")\s*】"
        r"|(?:" + "|".join(re.escape(n) for n in other_names) + r")\s*(?:\*\*)?\s*[：:])",
        re.MULTILINE,
    )
    m = other_label.search(text)
    if m and m.start() > 0:
        text = text[:m.start()]
        events.append("other_speaker_cut")

    return text.strip(), events


def limit_length(text, max_chars):
    """最大文字数を超えた場合、上限内で最後に完結した文までで切る。

    文末が1つも無ければ省略記号で切る。戻り値: (処理後の本文, 行った処理の一覧)
    """
    if len(text) <= max_chars:
        return text, []
    trimmed = trim_to_sentence_end(text[:max_chars])
    if trimmed:
        return trimmed, ["length_cut"]
    return text[:max_chars - 1] + "…", ["length_cut", "no_sentence_end"]


def postprocess(raw, me, other, max_chars, finish_reason=None):
    """生成結果を発言として整える。戻り値: (本文, 行った処理の一覧)"""
    text, events = clean_output(raw, me, other)
    if finish_reason == "MAX_TOKENS":
        events.append("max_tokens")
    # 相手のセリフの切り捨てや出力上限で文が途中で終わっていたら、最後の完結した文までにする
    if ("other_speaker_cut" in events or "max_tokens" in events) and text:
        trimmed = trim_to_sentence_end(text)
        if trimmed is None:
            events.append("no_sentence_end")
        elif trimmed != text:
            text = trimmed
            events.append("sentence_trimmed")
    text, more = limit_length(text, max_chars)
    return text, events + more


def _normalize(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


# ---------------------------------------------------------------------------
# 会話の実行
# ---------------------------------------------------------------------------

class _Stopped(Exception):
    """終了操作を受けたことを、ループの奥から外へ伝えるための例外。"""


class ConversationRunner:
    def __init__(self, conversation_id):
        conv = database.get_conversation(conversation_id)
        self.id = conversation_id
        self.opener = make_opener(conv["mode"], conv["topic"])
        self.personas = {
            "A": Persona("A", conv["name_a"], conv["persona_a"], conv["model_a"],
                         conv.get("provider_a") or "gemini"),
            "B": Persona("B", conv["name_b"], conv["persona_b"], conv["model_b"],
                         conv.get("provider_b") or "gemini"),
        }
        self._clients = {}   # 会社ID → APIクライアント
        self.max_turns = conv["max_turns"]
        self.max_chars = conv["max_chars"] or config.MAX_CHARS
        self.history_size = conv["history_size"] or config.HISTORY_SIZE
        self.interval = conv["interval_sec"] or config.TURN_INTERVAL_SEC
        # 保存済みのAI発言数（再開時は続きの番号から始める）
        self.turn = sum(
            1 for m in database.get_messages(conversation_id)
            if m["speaker"] in ("A", "B") and m["status"] == "complete"
        )

        self.status = "running"
        self.activity = ""          # 「AI Aが考えています」等の表示用
        # 進捗表示用の今の段階（考え中・待機中など）。時刻はUNIX秒で、画面側が秒数を数える
        self.phase = None
        self.resumable = False      # エラー停止後に「再開」で続けられるか
        self._stop = False
        self._pause = False
        self._cond = threading.Condition()
        self._subscribers = []
        self._sub_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    # --- 画面への通知（SSE） ---
    def subscribe(self):
        q = queue.Queue()
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _emit(self, event):
        with self._sub_lock:
            for q in self._subscribers:
                q.put(event)

    def state(self):
        return {
            "type": "status",
            "conversation_id": self.id,
            "status": self.status,
            "activity": self.activity,
            "turn": self.turn,
            "max_turns": self.max_turns,
            "pause_requested": self._pause and self.status == "running",
            "resumable": self.resumable,
            "names": {"A": self.personas["A"].name, "B": self.personas["B"].name},
            "next_speaker": self.next_speaker_key(),
            "phase": self.phase,
            "server_now": time.time(),  # 画面側でPCの時計とのずれを補正するため
        }

    def _set(self, status=None, activity=None, phase=False):
        """状態を更新して画面へ送る。phase は辞書か None（False のときは変えない）。"""
        # status を最後に変える（status を見て待っている側が、古い phase を読まないように）
        if phase is not False:
            self.phase = phase
        if activity is not None:
            self.activity = activity
        if status is not None:
            self.status = status
        self._emit(self.state())

    def _phase(self, kind, speaker=None, seconds=None, attempt=None, max_retries=None):
        """進捗表示用の段階。seconds を渡すと、その秒数後を待ち終わりの予定時刻にする。"""
        now = time.time()
        return {
            "kind": kind,              # generating / waiting / retry_wait / paused
            "speaker": speaker,
            "started_at": now,
            "until": now + seconds if seconds is not None else None,
            "attempt": attempt,
            "max_retries": max_retries,
        }

    def is_pause_requested(self):
        with self._cond:
            return self._pause

    def next_speaker_key(self):
        """次に発言するAI（"A"/"B"）。最後まで話し終えたら None。"""
        if self.turn >= self.max_turns:
            return None
        return "A" if (self.turn + 1) % 2 == 1 else "B"

    def next_speaker_name(self):
        return self.personas[self.next_speaker_key() or "A"].name

    # --- 操作（Webリクエストのスレッドから呼ばれる） ---
    def start(self):
        self._thread.start()

    def pause(self):
        with self._cond:
            if self.status != "running" or self._pause:
                return
            self._pause = True
            self._cond.notify_all()
        if self.phase and self.phase["kind"] in ("generating", "retry_wait"):
            self._set(activity="一時停止を受け付けました。生成中の発言が完成したら、保存してから停止します。")
        else:
            self._emit(self.state())

    def resume(self):
        with self._cond:
            if not self._pause:
                return
            self._pause = False
            self._cond.notify_all()
            cancelled_before_pause = self.status == "running"
        if cancelled_before_pause:
            # 生成中に一時停止→実際に止まる前に再開した場合
            self._set(activity="一時停止を取り消しました。会話を続けます。")

    def stop(self):
        with self._cond:
            if self.status in TERMINAL_STATUSES:
                return
            self._stop = True
            self._cond.notify_all()
        self._set(activity="終了処理中です（生成中の発言があれば、その応答を待ってから破棄します。次の発言は開始しません）")

    # --- 待機・チェックポイント（会話スレッド内） ---
    def _wait(self, seconds):
        """最大 seconds 秒待つ。終了なら _Stopped、一時停止なら True、時間切れなら False。"""
        with self._cond:
            self._cond.wait_for(lambda: self._stop or self._pause, timeout=seconds)
            if self._stop:
                raise _Stopped
            return self._pause

    def _checkpoint(self):
        """終了なら _Stopped を送出。一時停止中なら再開（または終了）まで待つ。"""
        with self._cond:
            if self._stop:
                raise _Stopped
            if not self._pause:
                return
        database.set_status(self.id, "paused")
        self._set(status="paused",
                  activity=f"一時停止中です。「再開」を押すと{self.next_speaker_name()}の発言から続けます。",
                  phase=self._phase("paused", self.next_speaker_key()))
        with self._cond:
            self._cond.wait_for(lambda: self._stop or not self._pause)
            if self._stop:
                raise _Stopped
        database.set_status(self.id, "running")
        self._set(status="running", activity="再開しました。", phase=None)

    # --- 本体 ---
    def _run(self):
        try:
            # 会話を始める前に、両方のAIのAPIクライアントを用意する（キー未設定ならここで止まる）
            for persona in self.personas.values():
                self._client_for(persona)
            while self.turn < self.max_turns:
                self._checkpoint()
                turn = self.turn + 1
                key = "A" if turn % 2 == 1 else "B"
                me = self.personas[key]
                other = self.personas["B" if key == "A" else "A"]
                self._set(activity=f"{me.name}が考えています…", phase=self._phase("generating", key))

                text, notes = self._generate_with_retry(me, other)
                with self._cond:
                    if self._stop:
                        raise _Stopped  # 終了操作済み（生成された発言は保存しない）

                msg = database.add_message(self.id, turn, key, text,
                                           director_note=",".join(notes) or None)
                self.turn = turn
                self.phase = None
                self._emit({"type": "message", "message": msg})

                if self.turn < self.max_turns:
                    self._checkpoint()  # 生成中に一時停止された場合はここで止まる
                    self._set(activity=f"次は{self.next_speaker_name()}の発言です。",
                              phase=self._phase("waiting", self.next_speaker_key(), self.interval))
                    self._wait(self.interval)  # 一時停止されたら次のループ先頭で止まる

            database.set_status(self.id, "completed", ended=True)
            self._set(status="completed", activity="", phase=None)
        except _Stopped:
            database.set_status(self.id, "stopped", ended=True)
            self._set(status="stopped", activity="", phase=None)
        except GeminiError as e:
            self._fail(e.message, resumable=e.kind not in NEEDS_RESTART, kind=e.kind)
        except Exception:
            logger.exception("会話 %s で予期しないエラー", self.id)
            self._fail("予期しないエラーが発生しました。サーバーのコンソール表示を確認してください。",
                       resumable=True)

    def _generate_with_retry(self, me, other):
        """生成に成功したら (テキスト, 進行役の指示の種類) を返す。

        終了操作なら _Stopped、失敗なら GeminiError。
        """
        messages = database.get_messages(self.id)
        contents = build_contents(me, other, self.opener, messages, self.history_size)
        notes = director.detect_notes(me.key, messages)
        if notes:
            logger.info("会話%d #%d %s: 進行役の指示 [%s]", self.id, self.turn + 1, me.key,
                        "、".join(director.NOTES[n][0] for n in notes))
        system_instruction = build_system_instruction(me, other, self.max_chars, notes)
        recent_texts = {
            _normalize(m["content"]) for m in messages[-2:] if m["speaker"] in ("A", "B")
        }

        attempts = {}  # エラーの種類ごとの再試行回数（上限・待ち時間は config.RETRY_POLICY）
        while True:
            try:
                gen = self._call_api(me, system_instruction, contents)
                raw, finish_reason = gen.text, gen.finish_reason
                text, events = postprocess(raw, me, other, self.max_chars, finish_reason)
                if events:
                    # 本文は記録せず、何をしたかと文字数だけを残す
                    logger.info(
                        "会話%d #%d %s: 後処理 [%s]（生成%d字 → 保存%d字、上限%d字、finish_reason=%s）",
                        self.id, self.turn + 1, me.key,
                        "、".join(POSTPROCESS_LABELS[e] for e in events),
                        len(raw), len(text), self.max_chars, finish_reason,
                    )
                if not text:
                    raise GeminiError("empty", "APIの返答に、発言として使える本文がありませんでした。")
                if _normalize(text) in recent_texts:
                    raise GeminiError("empty", "直前と同じ発言が返されました。")
                return text, notes
            except GeminiError as e:
                if not e.retryable:
                    raise
                max_retries, wait = config.RETRY_POLICY[e.kind]
                attempt = attempts.get(e.kind, 0)
                if attempt >= max_retries:
                    raise
                attempt = attempts[e.kind] = attempt + 1
                logger.info("再試行 %d/%d（%s）: %d秒待機", attempt, max_retries, e.kind, wait)
                self._set(activity=f"{e.message} {wait}秒後に再試行します（{attempt}/{max_retries}）",
                          phase=self._phase("retry_wait", me.key, wait, attempt, max_retries))
                if self._wait(wait):
                    self._checkpoint()  # 再試行の待機中に一時停止された場合
                self._set(activity=f"{me.name}が考えています…（再試行 {attempt}/{max_retries}）",
                          phase=self._phase("generating", me.key, attempt=attempt,
                                            max_retries=max_retries))

    def _client_for(self, persona):
        provider = providers.get(persona.provider)
        if provider is None:
            raise GeminiError("invalid_model", f"AIの提供会社「{persona.provider}」に対応していません。"
                                               "プルダウンで別のモデルを選び、新しく「会話開始」してください。")
        if provider.id not in self._clients:
            self._clients[provider.id] = provider.make_client()
        return provider, self._clients[provider.id]

    def _call_api(self, me, system_instruction, contents):
        """APIを1回呼び、成功・失敗にかかわらず使用回数として記録する。"""
        provider, client = self._client_for(me)
        model = me.model
        try:
            gen = provider.generate(client, model, system_instruction, contents)
        except GeminiError as e:
            reset_at = self._record_call(model, e.kind, http_status=e.http_status,
                                         retry_after_sec=e.retry_seconds, quota_id=e.quota_id)
            if e.kind == "daily_quota":
                logger.warning("1日の上限: モデル=%s、解除まで%s秒、解除予定=%s、quotaId=%s",
                               model, e.retry_seconds, reset_at, e.quota_id)
                if e.quota_limit:
                    try:
                        database.set_model_limit(model, e.quota_limit)
                    except Exception:
                        logger.exception("上限値の記録に失敗しました")
            raise
        self._record_call(model, "ok", prompt_tokens=gen.prompt_tokens,
                          output_tokens=gen.output_tokens, total_tokens=gen.total_tokens)
        return gen

    def _record_call(self, model, outcome, **fields):
        """使用回数を記録し、解除予定時刻（分かる場合）を返す。

        記録は目安のため、失敗しても会話は止めない。
        """
        reset_at = None
        try:
            reset_at = database.add_api_call(model, self.id, outcome, **fields)
        except Exception:
            logger.exception("API使用回数の記録に失敗しました")
        self._emit({"type": "usage"})
        return reset_at

    def _fail(self, message, resumable, kind=None):
        if resumable:
            message += " 原因が解消したら「再開」を押すと、この会話の続きから再試行します。"
        elif kind != "invalid_model":
            # モデル名のエラーは、プルダウンで選び直す案内がエラー文に入っている
            message += " .env を修正してアプリを再起動し、新しく会話を開始してください。"
        self.resumable = resumable
        try:
            msg = database.add_message(self.id, self.turn, "system", message, status="error")
            self._emit({"type": "message", "message": msg})
            database.set_status(self.id, "error", ended=True)
        except Exception:
            logger.exception("エラー内容の保存に失敗しました")
            message += "（このエラーの記録をログに保存できませんでした）"
        # 画面は終了系の状態を受け取ると接続を閉じるため、エラー文を先に送る
        self._emit({"type": "error", "message": message})
        self._set(status="error", activity="", phase=None)


class ConversationManager:
    """同時に1つの会話だけを動かす。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.current = None

    def _ensure_idle(self):
        if self.current and self.current.status not in TERMINAL_STATUSES:
            raise RuntimeError("すでに会話が進行中です。終了してから開始してください。")

    def start(self, mode, topic, persona_a, persona_b, max_turns,
              max_chars, history_size, interval_sec, character_a=None, character_b=None):
        with self._lock:
            self._ensure_idle()
            conv_id = database.create_conversation(
                mode, topic, persona_a.name, persona_b.name, persona_a.model,
                persona_b.model, persona_a.instruction, persona_b.instruction, max_turns,
                max_chars, history_size, interval_sec,
                provider_a=persona_a.provider, provider_b=persona_b.provider,
                character_a=character_a, character_b=character_b,
            )
            return self._launch(conv_id)

    def resume(self, conversation_id):
        """一時停止中なら再開し、エラー停止中なら同じ会話の続きから新しく実行する。"""
        with self._lock:
            r = self.get(conversation_id)
            if not r:
                raise RuntimeError("この会話は再開できません。")
            if r.status == "paused" or r.is_pause_requested():
                r.resume()
                return r
            if r.status == "error" and r.resumable:
                database.set_status(conversation_id, "running")
                return self._launch(conversation_id)
            raise RuntimeError("この会話は再開できる状態ではありません。")

    def _launch(self, conv_id):
        runner = ConversationRunner(conv_id)
        self.current = runner
        runner.start()
        return runner

    def get(self, conversation_id):
        r = self.current
        return r if r and r.id == conversation_id else None
