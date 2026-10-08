const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(
  path.join(__dirname, "../archival_organizer/web/static/archive.js"), "utf8",
);

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
