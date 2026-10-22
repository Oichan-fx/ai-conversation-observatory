"use strict";
// メイン画面（common.js を先に読み込むこと）

const el = (id) => document.getElementById(id);
const startBtn = el("start-btn");
const pauseBtn = el("pause-btn");
const resumeBtn = el("resume-btn");
const stopBtn = el("stop-btn");
const logBox = el("log");
const startAllowed = !startBtn.disabled;  // 設定不備のときはサーバー側で無効化済み
// ログに表示する話者名。会話中はサーバーから届く会話の名前、それ以外は設定中の名前を使う
let names = { A: "AI A", B: "AI B" };

let currentId = null;
let source = null;
const shownIds = new Set();

function addMessage(m) {
  if (shownIds.has(m.id)) return;
  shownIds.add(m.id);
  logBox.append(renderMessage(m, names, false));
  logBox.scrollTop = logBox.scrollHeight;
}

function showError(text) {
  el("error").textContent = text;
  el("error").hidden = !text;
}

// ---- 進捗表示（発言数のバー・話す番・待ち時間と生成の経過時間） ----
let phase = null;        // サーバーから届いた今の段階（考え中・待機中など）
let clockOffset = null;  // サーバーの時計 − このPCの時計（ミリ秒）
let timerId = null;

const serverNow = () => Date.now() + (clockOffset || 0);

// 状態が届くのが遅れると、その分だけずれを小さく見積もるため、届いた中で最大の値を使う
function updateClockOffset(serverSec) {
  if (typeof serverSec !== "number") return;
  const sample = serverSec * 1000 - Date.now();
  clockOffset = clockOffset === null ? sample : Math.max(clockOffset, sample);
}

function formatSeconds(sec) {
  return sec >= 60 ? `${Math.floor(sec / 60)}分${String(sec % 60).padStart(2, "0")}秒` : `${sec}秒`;
}

function renderTimer() {
  const box = el("timer");
  let text = "";
  let waiting = false;
  if (phase && phase.kind === "generating") {
    const sec = Math.max(0, Math.floor((serverNow() - phase.started_at * 1000) / 1000));
    text = `生成中 ${formatSeconds(sec)}`;
  } else if (phase && (phase.kind === "waiting" || phase.kind === "retry_wait") && phase.until) {
    const sec = Math.max(0, Math.ceil((phase.until * 1000 - serverNow()) / 1000));
    text = phase.kind === "waiting"
      ? `次の発言まで あと${formatSeconds(sec)}`
      : `再試行まで あと${formatSeconds(sec)}（${phase.attempt}/${phase.max_retries}）`;
    waiting = true;
  }
  box.textContent = text;
  box.className = waiting ? "timer wait" : "timer";
  box.hidden = !text;
  // 数える必要がある段階のときだけ、0.25秒ごとに表示を更新する（サーバーへの問い合わせはしない）
  const counting = Boolean(text);
  if (counting && !timerId) timerId = setInterval(renderTimer, 250);
  if (!counting && timerId) { clearInterval(timerId); timerId = null; }
}

function renderProgress(s) {
  const bar = el("progress");
  const ratio = s.max_turns > 0 ? Math.min(1, s.turn / s.max_turns) : 0;
  el("progress-fill").style.width = `${ratio * 100}%`;
  bar.setAttribute("aria-valuemax", String(s.max_turns || 0));
  bar.setAttribute("aria-valuenow", String(s.turn || 0));
  // 話す番：進行中・一時停止中・再開できるエラー停止中のときだけ出す
  const key = s.next_speaker;
  const show = key && (s.status === "running" || s.status === "paused" ||
                       (s.status === "error" && s.resumable));
  el("next-wrap").hidden = !show;
  const who = el("next-speaker");
  who.textContent = show ? names[key] || `AI ${key}` : "";
  who.className = show ? `next-${key.toLowerCase()}` : "";
}

function applyState(s) {
  if (s.names) names = s.names;
  let label = STATUS_LABELS[s.status] || s.status;
  if (s.pause_requested) label += "（一時停止待ち）";
  el("status").textContent = label;
  el("turns").textContent = `${s.turn} / ${s.max_turns}`;
  el("activity").textContent = s.activity || "";
  updateClockOffset(s.server_now);
  phase = TERMINAL.has(s.status) ? null : s.phase || null;
  renderProgress(s);
  renderTimer();
  const active = !TERMINAL.has(s.status);
  startBtn.disabled = active || !startAllowed;
  pauseBtn.disabled = s.status !== "running" || s.pause_requested;
  resumeBtn.disabled = !(s.status === "paused" || s.pause_requested ||
                         (s.status === "error" && s.resumable && startAllowed));
  stopBtn.disabled = !active;
  if (s.status !== "error") showError("");
}

function connect(id) {
  if (source) source.close();
  currentId = id;
  clockOffset = null;  // 接続し直したら、時計のずれも測り直す
  source = new EventSource(`/api/conversations/${id}/events`);
  source.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "snapshot") {
      ev.messages.forEach(addMessage);
    } else if (ev.type === "message") {
      addMessage(ev.message);
    } else if (ev.type === "status") {
      applyState(ev);
      if (TERMINAL.has(ev.status)) {
        source.close();
        source = null;
      }
    } else if (ev.type === "error") {
      showError(ev.message);
    } else if (ev.type === "usage") {
      loadUsage();
      loadModels();  // プルダウンの残り回数も更新する（APIは呼ばない）
    }
  };
  source.onerror = () => {
    // サーバー停止などで接続が切れた場合（EventSource は自動で再接続を試みる）
    el("activity").textContent = "サーバーとの接続が切れました。再接続を試みています…";
  };
}

function readNumber(id) {
  return Number(el(id).value);
}

// ---- 使用回数（目安） ----
let usage = null;

function formatTokens(n) {
  return n >= 10000 ? `${(n / 10000).toFixed(1)}万` : n.toLocaleString();
}

function renderUsage() {
  const table = el("usage-table");
  table.replaceChildren();
  for (const u of usage.models) {
    const tr = document.createElement("tr");
    const who = [u.model === usage.model_a ? "A" : "", u.model === usage.model_b ? "B" : ""]
      .filter(Boolean).join("・");
    const name = document.createElement("td");
    name.textContent = who ? `${u.model}（${who}）` : u.model;
    const count = document.createElement("td");
    count.className = "num" + (u.remaining === 0 ? " over" : u.remaining <= 4 ? " low" : "");
    count.textContent = `${u.calls} / ${u.limit}回`;
    const extra = document.createElement("td");
    extra.className = "hint";
    extra.textContent = u.api_reset_at
      ? `1日の上限に達しました（APIの解除予定 ${formatTime(u.api_reset_at, true)}）`
      : u.next_free_at
        ? `上限の目安に到達（${formatTime(u.next_free_at, true)}ごろから空き）`
        : `残り${u.remaining}回・${formatTokens(u.tokens)}トークン`;
    tr.append(name, count, extra);
    table.append(tr);
  }
  updateUsageWarning();
}

// 開始前の注意：この設定で各モデルを何回使うかと、残りの目安を比べる（止めはしない）
function updateUsageWarning() {
  const box = el("usage-warning");
  if (!usage) return;
  const turns = Math.max(0, readNumber("max-turns") || 0);
  const need = {};
  need[usage.model_a] = (need[usage.model_a] || 0) + Math.ceil(turns / 2);
  need[usage.model_b] = (need[usage.model_b] || 0) + Math.floor(turns / 2);
  const lines = [];
  for (const u of usage.models) {
    const n = need[u.model] || 0;
    if (n > u.remaining) {
      lines.push(`この設定では ${u.model} を${n}回使いますが、残りの目安は${u.remaining}回です。`);
    }
  }
  if (lines.length) lines.push("途中で1日の上限に達する可能性があります（再試行があるとさらに使います）。");
  box.textContent = lines.join("\n");
  box.hidden = lines.length === 0;
}

async function loadUsage() {
  try {
    const q = new URLSearchParams({ a: el("model-a").value || "", b: el("model-b").value || "" });
    const res = await fetch(`/api/usage?${q}`);
    if (!res.ok) return;
    usage = await res.json();
    renderUsage();
  } catch (_e) {
    // 使用回数は目安なので、取得できなくても会話の操作には影響させない
  }
}

el("max-turns").addEventListener("input", updateUsageWarning);

// ---- モデルの選択（APIキーが設定済みの会社のモデルだけ） ----
const MODEL_KEYS = { a: "model-a", b: "model-b" };

function storageGet(key) {
  try { return localStorage.getItem(key); } catch (_e) { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_e) { /* 保存できなくても動作には影響しない */ }
}

function renderModelChoices(data) {
  const updated = data.providers.map((p) => p.updated_at ? `${p.label}: ${formatTime(p.updated_at, true)}` : null)
    .filter(Boolean);
  el("models-updated").textContent = updated.length ? `一覧の最終更新 ${updated.join("、")}` : "一覧はまだ取得していません";
  for (const [key, id] of Object.entries(MODEL_KEYS)) {
    const select = el(id);
    // 選び直さないかぎり、前回選んだモデル → .env の初期値 の順で使う
    const wanted = select.value || storageGet(`model_${key}`) || data[`default_${key}`];
    select.replaceChildren();
    for (const p of data.providers) {
      const group = document.createElement("optgroup");
      group.label = p.label;
      for (const m of p.models) {
        const opt = document.createElement("option");
        opt.value = m.ref;
        opt.textContent = `${m.name}（残り${m.remaining}回）`;
        group.append(opt);
      }
      select.append(group);
    }
    const values = [...select.options].map((o) => o.value);
    select.value = values.includes(wanted) ? wanted : (values.includes(data[`default_${key}`]) ? data[`default_${key}`] : values[0] || "");
  }
}

async function loadModels() {
  try {
    const res = await fetch("/api/models");
    if (res.ok) renderModelChoices(await res.json());
  } catch (_e) {
    // 取得できなくても、次の操作で再読み込みする
  }
}

for (const [key, id] of Object.entries(MODEL_KEYS)) {
  el(id).addEventListener("change", () => {
    storageSet(`model_${key}`, el(id).value);
    loadUsage();
  });
}

el("refresh-models").addEventListener("click", async () => {
  const btn = el("refresh-models");
  btn.disabled = true;
  el("models-updated").textContent = "モデル一覧を取得しています…";
  try {
    renderModelChoices(await postJSON("/api/models/refresh"));
    loadUsage();
  } catch (err) {
    showError(`モデル一覧を更新できませんでした：${err.message}`);
    loadModels();
  } finally {
    btn.disabled = false;
  }
});

startBtn.addEventListener("click", async () => {
  showError("");
  const mode = document.querySelector("input[name=mode]:checked").value;
  const topic = el("topic").value.trim();
  if (mode === "topic" && !topic) {
    showError("テーマ指定モードでは、テーマを入力してください。");
    return;
  }
  const conflict = Characters.conflict();
  if (conflict) {
    showError(conflict);
    return;
  }
  startBtn.disabled = true;
  try {
    names = Characters.names();
    const state = await postJSON("/api/conversations", {
      mode,
      topic,
      max_turns: readNumber("max-turns"),
      max_chars: readNumber("max-chars"),
      history_size: readNumber("history-size"),
      interval_sec: readNumber("interval-sec"),
      model_a: el("model-a").value,
      model_b: el("model-b").value,
      character_a: Characters.get("a"),
      character_b: Characters.get("b"),
    });
    logBox.replaceChildren();
    shownIds.clear();
    applyState(state);
    connect(state.conversation_id);
  } catch (err) {
    showError(err.message);
    startBtn.disabled = !startAllowed;
  }
});

async function control(action, btn) {
  if (currentId == null) return;
  btn.disabled = true;
  try {
    const state = await postJSON(`/api/conversations/${currentId}/${action}`);
    applyState(state);
    // エラー停止からの再開では新しい実行になるため、接続し直す
    if (action === "resume" && !source) connect(currentId);
  } catch (err) {
    showError(err.message);
    btn.disabled = false;
  }
}

pauseBtn.addEventListener("click", () => control("pause", pauseBtn));
resumeBtn.addEventListener("click", () => control("resume", resumeBtn));
stopBtn.addEventListener("click", () => control("stop", stopBtn));

document.querySelectorAll("input[name=mode]").forEach((r) =>
  r.addEventListener("change", () => {
    el("topic-required").textContent =
      r.value === "topic" && r.checked ? "（必須）" : "（フリートークでは任意・使用しません）";
  })
);

Characters.init().catch(() => showError("キャラクター設定を読み込めませんでした。画面を再読み込みしてください。"));
loadModels().then(loadUsage);

// ページを再読み込みした場合、進行中の会話（または直前の会話）に再接続する
fetch("/api/current")
  .then((res) => res.json())
  .then((s) => { if (s) { applyState(s); connect(s.conversation_id); } })
  .catch(() => {});
