"use strict";
// キャラクター設定パネル（common.js の後、app.js の前に読み込む）
// 選択肢は character_options.json、初期プリセットは presets_default.json、
// 自分で保存したプリセットは data/presets.json（サーバー側）にある。

const Characters = (() => {
  const $ = (id) => document.getElementById(id);
  const STORAGE_KEY = "characters_v1";
  const KEYS = ["a", "b"];

  let fields = [];
  let presets = [];
  const chars = { a: null, b: null };   // 今使っている設定
  let editing = null;                     // パネルで編集中の "a" / "b"
  let draft = null;                       // パネルで編集中の設定（「この設定を使う」まで反映しない）
  let previewTimer = null;
  let lastPreview = null;
  const listeners = [];

  const clone = (o) => JSON.parse(JSON.stringify(o));
  const other = (key) => (key === "a" ? "b" : "a");

  function normalizeCharacter(c) {
    return {
      name: (c && c.name) || "",
      selections: { ...((c && c.selections) || {}) },
      free: { ...((c && c.free) || {}) },
      extra: (c && c.extra) || "",
    };
  }

  // ---- メイン画面のカード ----
  function summary(c) {
    const parts = [];
    for (const f of fields) {
      if (f.key === "name") continue;
      const values = [...(c.selections[f.key] || [])];
      if (c.free[f.key]) values.push(c.free[f.key]);
      if (values.length) parts.push(`${f.label}：${values.join("、")}`);
    }
    if (c.extra) parts.push(c.extra);
    const text = parts.join(" ／ ") || "（設定なし）";
    return text.length > 120 ? `${text.slice(0, 120)}…` : text;
  }

  function renderCards() {
    for (const key of KEYS) {
      const c = chars[key];
      if (!c) continue;
      $(`card-name-${key}`).textContent = c.name;
      $(`card-summary-${key}`).textContent = summary(c);
    }
    const warn = nameConflict(chars.a, chars.b);
    $("name-warning").textContent = warn || "";
    $("name-warning").hidden = !warn;
    listeners.forEach((fn) => fn());
  }

  // サーバー側の characters.check_names と同じ考え方（最終的な確認はサーバーで行う）
  function normName(n) {
    return (n || "").normalize("NFKC").replace(/\s+/g, "").toLowerCase();
  }
  function nameConflict(a, b) {
    if (!a || !b) return null;
    const na = normName(a.name), nb = normName(b.name);
    if (na === nb) return `AI A と AI B に同じ名前（${a.name}）は使えません。どちらかの名前を変えてください。`;
    if (na === "b" || na === "aib") return `AI A の名前に「${a.name}」は使えません（AI B の呼び名と紛らわしいため）。`;
    if (nb === "a" || nb === "aia") return `AI B の名前に「${b.name}」は使えません（AI A の呼び名と紛らわしいため）。`;
    return null;
  }

  function save() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(chars)); } catch (_e) { /* 保存できなくても動く */ }
  }
  function loadSaved() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (saved && saved.a && saved.b && saved.a.name && saved.b.name) return saved;
    } catch (_e) { /* 読めなければ使わない */ }
    return null;
  }

  // ---- パネル ----
  function randomInt(min, max) {
    return min + Math.floor(Math.random() * (max - min + 1));
  }
  function pickRandom(list, n) {
    const copy = [...list];
    for (let i = copy.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy.slice(0, n);
  }

  function randomize(field) {
    if (field.key === "name") {
      const taken = normName(chars[other(editing)]?.name);
      const candidates = field.options.filter((o) => normName(o) !== taken);
      if (candidates.length) draft.name = pickRandom(candidates, 1)[0];
      return;
    }
    let [min, max] = field.random;
    if (field.max_select) max = Math.min(max, field.max_select);
    const n = Math.min(field.options.length, randomInt(Math.min(min, max), max));
    draft.selections[field.key] = pickRandom(field.options, n);
  }

  function renderFields() {
    const box = $("char-fields");
    box.replaceChildren();
    for (const f of fields) {
      const row = document.createElement("div");
      row.className = "char-field";

      const head = document.createElement("div");
      head.className = "char-field-head";
      const label = document.createElement("span");
      label.className = "char-field-label";
      label.textContent = f.label + (f.max_select === 1 && f.key !== "name" ? "（1つ）" : "");
      const dice = document.createElement("button");
      dice.type = "button";
      dice.className = "small dice";
      dice.textContent = "🎲";
      dice.title = `${f.label}をランダムに選ぶ`;
      dice.addEventListener("click", () => { randomize(f); renderFields(); schedulePreview(); });
      head.append(label, dice);
      row.append(head);

      if (f.key === "name") {
        const input = document.createElement("input");
        input.type = "text";
        input.className = "char-name";
        input.maxLength = 30;
        input.value = draft.name;
        input.placeholder = "名前を入力するか 🎲 で選ぶ";
        input.addEventListener("input", () => { draft.name = input.value; schedulePreview(); });
        row.append(input);
        box.append(row);
        continue;
      }

      const selected = draft.selections[f.key] || [];
      // 選択肢の一覧から消えた項目も、選んでいれば残して表示する
      const options = [...f.options, ...selected.filter((s) => !f.options.includes(s))];
      const group = document.createElement("div");
      group.className = "char-options";
      for (const opt of options) {
        const lab = document.createElement("label");
        lab.className = "chip";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = selected.includes(opt);
        cb.addEventListener("change", () => {
          let list = draft.selections[f.key] || [];
          if (cb.checked) {
            list = f.max_select === 1 ? [opt] : [...list, opt];
          } else {
            list = list.filter((v) => v !== opt);
          }
          if (f.max_select && list.length > f.max_select) {
            cb.checked = false;
            showError(`${f.label}は${f.max_select}つまで選べます。`);
            return;
          }
          draft.selections[f.key] = list;
          if (f.max_select === 1) renderFields();   // 1つだけの項目は他のチェックを外す
          schedulePreview();
        });
        lab.append(cb, document.createTextNode(opt));
        group.append(lab);
      }
      row.append(group);

      if (f.free_text) {
        const free = document.createElement("input");
        free.type = "text";
        free.className = "char-free";
        free.maxLength = 200;
        free.placeholder = "自由記入";
        free.value = draft.free[f.key] || "";
        free.addEventListener("input", () => { draft.free[f.key] = free.value; schedulePreview(); });
        row.append(free);
      }
      box.append(row);
    }
    $("char-extra").value = draft.extra;
  }

  function showError(text) {
    $("char-error").textContent = text || "";
    $("char-error").hidden = !text;
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(updatePreview, 250);
  }

  async function updatePreview() {
    if (!editing || !draft) return;          // パネルが閉じた後に予約分が動いた場合
    const requestedFor = draft;
    const res = await fetch("/api/characters/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: editing.toUpperCase(),
        character: draft,
        other_name: chars[other(editing)].name,
        max_chars: Number(document.getElementById("max-chars")?.value) || undefined,
      }),
    }).catch(() => null);
    if (!res) return;
    const data = await res.json().catch(() => ({}));
    if (draft !== requestedFor) return;      // 待っている間に閉じた・別の設定に切り替わった
    lastPreview = { ok: res.ok, ...data };
    if (!res.ok) {
      showError(data.error || "プレビューを作れませんでした。");
      return;
    }
    $("preview-persona").textContent = data.persona;
    $("preview-rules").textContent = data.rules;
    showError(data.name_error || "");
  }

  function renderPresets(selectId) {
    const select = $("preset-select");
    select.replaceChildren();
    const groups = [["初期プリセット", presets.filter((p) => p.default)],
                    ["保存したプリセット", presets.filter((p) => !p.default)]];
    for (const [label, items] of groups) {
      if (!items.length) continue;
      const g = document.createElement("optgroup");
      g.label = label;
      for (const p of items) {
        const o = document.createElement("option");
        o.value = p.id;
        o.textContent = p.name;
        g.append(o);
      }
      select.append(g);
    }
    if (selectId) select.value = selectId;
    updatePresetButtons();
  }

  function updatePresetButtons() {
    const p = presets.find((x) => x.id === $("preset-select").value);
    $("preset-delete").disabled = !p || p.default;
    $("preset-load").disabled = !p;
  }

  function open(key) {
    editing = key;
    draft = normalizeCharacter(clone(chars[key]));
    $("char-title").textContent = `AI ${key.toUpperCase()} のキャラクター設定`;
    $("char-panel").dataset.key = key;
    showError("");
    renderFields();
    renderPresets();
    $("char-panel").hidden = false;
    document.body.classList.add("panel-open");
    updatePreview();
    $("char-close").focus();
  }

  function close() {
    clearTimeout(previewTimer);
    $("char-panel").hidden = true;
    document.body.classList.remove("panel-open");
    editing = null;
    draft = null;
    $(`card-summary-${$("char-panel").dataset.key || "a"}`)?.focus();
  }

  async function apply() {
    clearTimeout(previewTimer);
    await updatePreview();
    if (!lastPreview || !lastPreview.ok) return;           // 入力エラーはパネルに表示済み
    if (lastPreview.name_error) return;                    // 同じ名前などは使えない（パネルに表示済み）
    chars[editing] = normalizeCharacter(clone(draft));
    save();
    renderCards();
    close();
  }

  async function savePreset() {
    const current = presets.find((p) => p.id === $("preset-select").value);
    const suggestion = current && !current.default ? current.name : draft.name;
    const name = window.prompt("プリセットの名前を入力してください（同じ名前があれば上書きします）", suggestion || "");
    if (name == null) return;
    const exists = presets.find((p) => !p.default && p.name === name.trim());
    if (exists && !window.confirm(`「${exists.name}」を上書きしますか？`)) return;
    try {
      const data = await postJSON("/api/presets", { name, character: draft });
      presets = data.presets;
      renderPresets(data.saved.id);
      showError("");
    } catch (err) {
      showError(err.message);
    }
  }

  async function deletePreset() {
    const p = presets.find((x) => x.id === $("preset-select").value);
    if (!p || p.default) return;
    if (!window.confirm(`プリセット「${p.name}」を削除しますか？`)) return;
    const res = await fetch(`/api/presets/${encodeURIComponent(p.id)}`, { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { showError(data.error || "削除できませんでした。"); return; }
    presets = data.presets;
    renderPresets();
  }

  function loadPreset() {
    const p = presets.find((x) => x.id === $("preset-select").value);
    if (!p) return;
    draft = normalizeCharacter(clone(p.character));
    renderFields();
    updatePreview();
  }

  function bind() {
    for (const key of KEYS) {
      const card = $(`card-${key}`);
      card.addEventListener("click", (e) => {
        if (e.target.closest("select, label.model-select")) return;   // モデルの選択では開かない
        open(key);
      });
      $(`card-summary-${key}`).addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(key); }
      });
    }
    $("char-close").addEventListener("click", close);
    $("char-cancel").addEventListener("click", close);
    $("char-backdrop").addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("char-panel").hidden) close();
    });
    $("char-apply").addEventListener("click", apply);
    $("char-extra").addEventListener("input", () => { draft.extra = $("char-extra").value; schedulePreview(); });
    // 全部ランダム：チェックを選び直し、自由記入と追加の指示も空にする（項目ごとの🎲は自由記入を残す）
    $("random-all").addEventListener("click", () => {
      const hasText = draft.extra.trim() || Object.values(draft.free).some((v) => (v || "").trim());
      if (hasText && !window.confirm("自由記入と「追加の指示」も消えますがよいですか？")) return;
      draft.free = {};
      draft.extra = "";
      fields.forEach(randomize);
      renderFields();
      schedulePreview();
    });
    $("preset-select").addEventListener("change", updatePresetButtons);
    $("preset-load").addEventListener("click", loadPreset);
    $("preset-save").addEventListener("click", savePreset);
    $("preset-delete").addEventListener("click", deletePreset);
  }

  async function init() {
    bind();
    const res = await fetch("/api/characters");
    const data = await res.json();
    fields = data.fields;
    presets = data.presets;
    // このブラウザで最後に使った設定 → 前回の会話の設定（なければ初期プリセット）の順
    const saved = loadSaved();
    chars.a = normalizeCharacter(saved ? saved.a : data.character_a);
    chars.b = normalizeCharacter(saved ? saved.b : data.character_b);
    renderCards();
  }

  return {
    init,
    get: (key) => clone(chars[key]),
    names: () => ({ A: chars.a?.name || "AI A", B: chars.b?.name || "AI B" }),
    conflict: () => nameConflict(chars.a, chars.b),
    onChange: (fn) => listeners.push(fn),
  };
})();
