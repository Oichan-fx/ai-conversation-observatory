"use strict";
// 過去ログ画面（common.js を先に読み込むこと）

const el = (id) => document.getElementById(id);

function convLabel(c) {
  return c.mode === "topic" && c.topic ? c.topic : MODE_LABELS.free;
}

async function loadList() {
  const res = await fetch("/api/conversations");
  const list = await res.json();
  const ul = el("conv-list");
  ul.replaceChildren();
  el("list-empty").hidden = list.length > 0;
  for (const c of list) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = "conv-item";
    btn.dataset.id = c.id;

    const title = document.createElement("span");
    title.className = "conv-title";
    title.textContent = `#${c.id} ${convLabel(c)}`;
    const meta = document.createElement("span");
    meta.className = "conv-meta";
    meta.textContent = `${formatTime(c.created_at, true)} ／ ${MODE_LABELS[c.mode]} ／ ${c.turns}/${c.max_turns}発言`;
    const badge = document.createElement("span");
    badge.className = `badge status-${c.status}`;
    badge.textContent = STATUS_LABELS[c.status] || c.status;

    btn.append(title, meta, badge);
    btn.addEventListener("click", () => showDetail(c.id));
    li.append(btn);
    ul.append(li);
  }
}

async function showDetail(id) {
  el("detail-error").hidden = true;
  const res = await fetch(`/api/conversations/${id}`);
  if (!res.ok) {
    el("detail-error").textContent = "会話を読み込めませんでした。";
    el("detail-error").hidden = false;
    return;
  }
  const { conversation: c, messages } = await res.json();
  history.replaceState(null, "", `/history?id=${id}`);
  document.querySelectorAll(".conv-item").forEach((b) =>
    b.classList.toggle("selected", Number(b.dataset.id) === id));

  el("detail-empty").hidden = true;
  el("detail").hidden = false;
  el("detail-title").textContent = `#${c.id} ${convLabel(c)}`;
  el("export-md").href = `/api/conversations/${id}/export?format=md`;
  el("export-txt").href = `/api/conversations/${id}/export?format=txt`;

  const v = (x, unit = "") => (x == null ? "（記録なし）" : `${x}${unit}`);
  const rows = [
    ["状態", STATUS_LABELS[c.status] || c.status],
    ["開始", formatTime(c.created_at, true)],
    ["終了", c.ended_at ? formatTime(c.ended_at, true) : "（未終了）"],
    ["モード", MODE_LABELS[c.mode]],
    ["テーマ", c.topic || "（なし）"],
    ["最大発言数", v(c.max_turns)],
    ["1発言の最大文字数", v(c.max_chars, "字")],
    ["履歴として渡す発言数", v(c.history_size)],
    ["発言間隔", v(c.interval_sec, "秒")],
  ];
  const dl = el("detail-settings");
  dl.replaceChildren();
  for (const [k, val] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = val;
    dl.append(dt, dd);
  }

  el("name-a").textContent = `${c.name_a}（モデル: ${c.model_a}）`;
  el("persona-a").textContent = c.persona_a;
  el("name-b").textContent = `${c.name_b}（モデル: ${c.model_b}）`;
  el("persona-b").textContent = c.persona_b;

  const names = { A: c.name_a, B: c.name_b };
  const log = el("detail-log");
  log.replaceChildren(...messages.map((m) => renderMessage(m, names, true)));
  if (messages.length === 0) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "発言はありません。";
    log.append(p);
  }
  log.scrollTop = 0;
}

loadList().then(() => {
  const id = Number(new URLSearchParams(location.search).get("id"));
  if (id) showDetail(id);
});
