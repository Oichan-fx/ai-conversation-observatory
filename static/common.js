"use strict";
// メイン画面と過去ログ画面で共通の表示処理

const STATUS_LABELS = {
  running: "会話中",
  paused: "一時停止中",
  completed: "完了（最大発言数に到達）",
  stopped: "終了",
  error: "エラーで停止",
  interrupted: "中断（アプリ終了時に進行中だった）",
};
const TERMINAL = new Set(["completed", "stopped", "error", "interrupted"]);
const MODE_LABELS = { free: "フリートーク", topic: "テーマ指定" };

// "2026-10-14T12:34:56" → "12:34:56"（withDate なら "2026-10-14 12:34:56"）
function formatTime(iso, withDate) {
  if (!iso) return "";
  return withDate ? iso.replace("T", " ") : (iso.split("T")[1] || iso);
}

// 発言1件の表示要素を作る。本文は textContent で入れる（AIの出力をHTMLとして解釈させない）
function renderMessage(m, names, withDate) {
  const item = document.createElement("article");
  item.className = `msg speaker-${m.speaker.toLowerCase()}` + (m.status === "error" ? " msg-error" : "");
  const head = document.createElement("div");
  head.className = "msg-head";
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = m.speaker === "system" ? "システム" : `#${m.turn_number} ${names[m.speaker]}`;
  const time = document.createElement("time");
  time.textContent = formatTime(m.created_at, withDate);
  head.append(who, time);
  const body = document.createElement("p");
  body.className = "msg-body";
  body.textContent = m.content;
  item.append(head, body);
  const notes = noteLabels(m.director_note);
  if (notes.length) {
    const note = document.createElement("p");
    note.className = "msg-note";
    note.textContent = `進行役：${notes.join("、")}`;
    item.append(note);
  }
  return item;
}

// 進行役の指示の種類（"question,wrapup" など）→ 表示用の文言。文言は director.py が元
function noteLabels(noteStr) {
  const labels = window.NOTE_LABELS || {};
  return (noteStr || "").split(",").filter((n) => labels[n]).map((n) => labels[n]);
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `エラーが発生しました（${res.status}）`);
  return data;
}
