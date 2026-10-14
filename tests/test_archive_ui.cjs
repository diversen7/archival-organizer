const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(
  path.join(__dirname, "../archival_organizer/web/static/archive.js"), "utf8",
);

function loadSidebar(storage) {
  const root = { dataset: {} };
  const panel = { hidden: false };
  const label = { textContent: "Hide navigation" };
  const button = { attributes: {}, disabled: true };
  let click;
  button.setAttribute = (key, value) => { button.attributes[key] = value; };
  button.querySelector = () => label;
  button.addEventListener = (event, callback) => { click = callback; };
  runInNewContext(script, {
    localStorage: storage,
    document: {
      documentElement: root,
      querySelector: (selector) => ({ "#sidebar-toggle": button, "#archive-sidebar": panel })[selector] ?? null,
      querySelectorAll: () => [],
    },
  });
  return { root, panel, label, button, click: () => click() };
}

test("navigation toggle hides the column, restores it, and remembers the choice across pages", () => {
  const saved = new Map();
  const storage = {
    getItem: (key) => saved.get(key),
    setItem: (key, value) => saved.set(key, value),
  };
  const page = loadSidebar(storage);
  assert.equal(page.panel.hidden, false);
  assert.equal(page.button.disabled, false);
  page.click();
  assert.equal(page.panel.hidden, true);
  assert.equal(page.root.dataset.sidebarHidden, "true");
  assert.equal(page.button.attributes["aria-expanded"], "false");
  assert.equal(page.label.textContent, "Show navigation");
  const next = loadSidebar(storage);
  assert.equal(next.panel.hidden, true);
  next.click();
  assert.equal(next.panel.hidden, false);
  assert.equal(next.root.dataset.sidebarHidden, "false");
  assert.equal(next.button.attributes["aria-expanded"], "true");
  assert.equal(next.label.textContent, "Hide navigation");
  assert.equal(loadSidebar(storage).panel.hidden, false);
});

test("navigation toggle still works when storage is unavailable", () => {
  const fail = () => { throw new Error("Storage blocked"); };
  const page = loadSidebar({ getItem: fail, setItem: fail });
  assert.equal(page.panel.hidden, false);
  page.click();
  assert.equal(page.panel.hidden, true);
  page.click();
  assert.equal(page.panel.hidden, false);
});

function loadPage(storage, defaults) {
  const sections = Object.entries(defaults).map(([name, open]) => ({
    dataset: { disclosure: name },
    open,
    listeners: {},
    addEventListener(event, callback) { this.listeners[event] = callback; },
    toggle() {
      this.open = !this.open;
      this.listeners.toggle();
    },
  }));
  runInNewContext(script, {
    localStorage: storage,
    document: {
      querySelector: () => null,
      querySelectorAll: () => sections,
    },
  });
  return Object.fromEntries(sections.map((section) => [section.dataset.disclosure, section]));
}

test("section choices survive navigation independently, including absent sections", () => {
  const saved = new Map();
  const storage = {
    getItem: (key) => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, value),
  };
  const defaults = {
    "basic-information": true,
    "beginning-ending-text": false,
    "visual-layout-description": false,
    "full-transcription": true,
    subfolders: true,
  };
  const first = loadPage(storage, defaults);
  for (const [name, open] of Object.entries(defaults)) {
    assert.equal(first[name].open, open);
  }
  first["basic-information"].toggle();
  first["beginning-ending-text"].toggle();
  first.subfolders.toggle();

  // Preview pages can omit metadata sections without losing their preferences.
  assert.equal(loadPage(storage, { subfolders: true }).subfolders.open, false);
  const next = loadPage(storage, defaults);
  assert.equal(next["basic-information"].open, false);
  assert.equal(next["beginning-ending-text"].open, true);
  assert.equal(next["visual-layout-description"].open, false);
  assert.equal(next["full-transcription"].open, true);
  next["basic-information"].toggle();
  assert.equal(loadPage(storage, defaults)["basic-information"].open, true);
});

test("sections remain usable when browser storage is blocked", () => {
  const storage = {
    getItem() { throw new Error("Storage blocked"); },
    setItem() { throw new Error("Storage blocked"); },
  };
  const sections = loadPage(storage, { "basic-information": true });
  assert.equal(sections["basic-information"].open, true);
  sections["basic-information"].toggle();
  assert.equal(sections["basic-information"].open, false);
});

test("prompt section copies the original saved text", async () => {
  const savedText = "Historical prompt\nPreserve æøå and <source> text";
  let copied;
  let copyPrompt;
  const status = { textContent: "" };
  const button = {
    dataset: { copyPrompt: "prompt-1", promptText: savedText },
    nextElementSibling: status,
    addEventListener(_event, callback) { copyPrompt = callback; },
  };
  const panel = {
    querySelectorAll() { return [button]; },
  };
  runInNewContext(script, {
    document: {
      querySelectorAll: () => [],
      querySelector(selector) {
        if (selector === "#prompts-used") return panel;
        return null;
      },
      getElementById: () => ({ textContent: "Historical prompt Preserve æøå and <source> text" }),
    },
    navigator: { clipboard: { async writeText(text) { copied = text; } } },
  });
  await copyPrompt();
  assert.equal(copied, savedText);
  assert.equal(status.textContent, "Copied");
});

test("image opens in a modal while modified clicks keep the original link", () => {
  let onClick;
  let onClose;
  let onPopupClick;
  let onPopstate;
  const entries = [null];
  let position = 0;
  let backCalls = 0;
  const history = {
    get state() { return entries[position]; },
    pushState(state) { entries.splice(++position); entries.push(state); },
    back() { backCalls++; position--; onPopstate(); },
    forward() { position++; onPopstate(); },
  };
  const image = { dataset: { originalUrl: "/original/2/" } };
  const popup = {
    open: false,
    querySelector: () => image,
    showModal() { this.open = true; },
    close() { this.open = false; onClose(); },
    addEventListener(event, callback) {
      if (event === "close") onClose = callback;
      if (event === "click") onPopupClick = callback;
    },
  };
  runInNewContext(script, {
    history,
    window: { addEventListener(_event, callback) { onPopstate = callback; } },
    document: {
      querySelectorAll: () => [],
      querySelector(selector) {
        if (selector === "#image-popup") return popup;
        if (selector === ".image-card") {
          return { addEventListener(_event, callback) { onClick = callback; } };
        }
        return null;
      },
    },
  });
  const modifiedClick = { button: 0, ctrlKey: true, preventDefault() { assert.fail("Modified click must keep its link behavior"); } };
  onClick(modifiedClick);
  assert.equal(popup.open, false);
  let prevented = false;
  onClick({ button: 0, preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(popup.open, true);
  assert.equal(image.src, "/original/2/");
  history.back();
  assert.equal(popup.open, false);
  assert.equal(backCalls, 1, "Back closes the overlay without going back twice");
  history.forward();
  assert.equal(popup.open, true);
  popup.close();
  assert.equal(history.state, null);
  assert.equal(backCalls, 2, "Close or Escape removes the overlay history entry");
  history.forward();
  onPopupClick({ target: image });
  assert.equal(popup.open, true, "Clicking the scan keeps the overlay open");
  onPopupClick({ target: popup });
  assert.equal(popup.open, false, "Clicking outside the scan closes the overlay");
  assert.equal(backCalls, 3, "Backdrop dismissal also restores page history");
});

function loadReviewer(storage) {
  const ids = ["reviewer-name", "reviewer-editor", "reviewer-summary", "reviewer-tools",
    "reviewer-display-name", "reviewer-start", "reviewer-change", "reviewer-cancel",
    "reviewer-stop", "reviewer-status"];
  const nodes = Object.fromEntries(ids.map(id => [id, {
    value: "", textContent: "", hidden: false, listeners: {},
    addEventListener(event, callback) { this.listeners[event] = callback; },
    focus() { this.focused = true; },
  }]));
  const field = { value: "" };
  const editForm = {
    addEventListener(event, callback) { this.submit = callback; },
    querySelector: () => field,
  };
  nodes["reviewer-tools"].querySelectorAll = () => [editForm];
  runInNewContext(script, {
    localStorage: storage,
    document: {
      querySelectorAll: () => [],
      querySelector: (selector) => nodes[selector.slice(1)] ?? null,
    },
  });
  return {
    nodes, field, editForm,
    click(id) { nodes[id].listeners.click(); },
    save(name) {
      nodes["reviewer-name"].value = name;
      nodes["reviewer-editor"].listeners.submit({ preventDefault() {} });
    },
  };
}

function reviewerStorage(saved) {
  return {
    getItem: (key) => saved.get(key),
    setItem: (key, value) => saved.set(key, value),
    removeItem: (key) => saved.delete(key),
  };
}

test("review starts only with a valid committed name, remembers it, and stops explicitly", () => {
  const saved = new Map();
  const storage = reviewerStorage(saved);
  const page = loadReviewer(storage);
  const { nodes } = page;
  assert.equal(nodes["reviewer-editor"].hidden, false);
  assert.equal(nodes["reviewer-tools"].hidden, true);
  assert.equal(nodes["reviewer-summary"].hidden, true);
  page.save("   ");
  assert.equal(nodes["reviewer-tools"].hidden, true);
  assert.equal(nodes["reviewer-status"].hidden, false);
  page.save("A".repeat(101));
  assert.equal(nodes["reviewer-tools"].hidden, true);
  page.save("  Alice <reviewer>  ");
  assert.equal(nodes["reviewer-editor"].hidden, true);
  assert.equal(nodes["reviewer-tools"].hidden, false);
  assert.equal(nodes["reviewer-display-name"].textContent, "Alice <reviewer>");
  page.editForm.submit({ preventDefault() { assert.fail("Named edits should submit"); } });
  assert.equal(page.field.value, "Alice <reviewer>");
  const next = loadReviewer(storage);
  assert.equal(next.nodes["reviewer-editor"].hidden, true);
  assert.equal(next.nodes["reviewer-tools"].hidden, false);
  next.click("reviewer-change");
  assert.equal(next.nodes["reviewer-tools"].hidden, true);
  next.nodes["reviewer-name"].value = "Uncommitted name";
  next.click("reviewer-cancel");
  assert.equal(next.nodes["reviewer-display-name"].textContent, "Alice <reviewer>");
  assert.equal(next.nodes["reviewer-tools"].hidden, false);
  next.click("reviewer-change");
  next.save(" Bob ");
  next.editForm.submit({ preventDefault() { assert.fail("Named edits should submit"); } });
  assert.equal(next.field.value, "Bob");
  assert.equal(saved.get("archive-browser:reviewer-name"), "Bob");
  next.click("reviewer-change");
  next.click("reviewer-stop");
  assert.equal(next.nodes["reviewer-editor"].hidden, false);
  assert.equal(next.nodes["reviewer-tools"].hidden, true);
  assert.equal(saved.has("archive-browser:reviewer-name"), false);
  assert.equal(loadReviewer(storage).nodes["reviewer-tools"].hidden, true);
  let prevented = false;
  next.editForm.submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
});

test("invalid saved reviewer names stay in browsing mode", () => {
  for (const name of ["  ", "A".repeat(101)]) {
    const saved = new Map([["archive-browser:reviewer-name", name]]);
    const page = loadReviewer(reviewerStorage(saved));
    assert.equal(page.nodes["reviewer-tools"].hidden, true);
    assert.equal(page.nodes["reviewer-editor"].hidden, false);
  }
});

test("reviewing works with unavailable storage and edits are blocked while changing name", () => {
  const fail = () => { throw new Error("Storage blocked"); };
  const page = loadReviewer({ getItem: fail, setItem: fail, removeItem: fail });
  page.save("Dennis");
  assert.equal(page.nodes["reviewer-tools"].hidden, false);
  page.click("reviewer-change");
  let prevented = false;
  page.editForm.submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  page.click("reviewer-cancel");
  page.editForm.submit({ preventDefault() { assert.fail("Named edits should submit"); } });
  assert.equal(page.field.value, "Dennis");
  page.click("reviewer-change");
  page.click("reviewer-stop");
  assert.equal(page.nodes["reviewer-tools"].hidden, true);
});
