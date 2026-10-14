const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(
  path.join(__dirname, "../archival_organizer/web/static/theme.js"), "utf8",
);
const key = "archive-browser:theme";

function loadTheme(storage, darkSystem = false) {
  const root = { dataset: {} };
  const control = { attributes: {}, setAttribute(key, value) { this.attributes[key] = value; } };
  const systemControl = { ...control, attributes: {} };
  let ready, toggle, reset, systemChange;
  control.addEventListener = (event, callback) => { toggle = callback; };
  systemControl.addEventListener = (event, callback) => { reset = callback; };
  const media = {
    matches: darkSystem,
    addEventListener: (event, callback) => { systemChange = callback; },
  };
  runInNewContext(script, {
    localStorage: storage,
    window: { matchMedia: () => media },
    document: {
      documentElement: root,
      addEventListener: (event, callback) => { ready = callback; },
      querySelector: (selector) => selector === "#theme-mode" ? control : systemControl,
    },
  });
  return {
    root, control, systemControl, ready,
    toggle() { toggle(); },
    reset() { reset(); },
    changeSystem(dark) { media.matches = dark; systemChange(); },
  };
}

test("system mode defaults to CSS preference; explicit choices persist and can be cleared", () => {
  const saved = new Map();
  const storage = {
    getItem: (key) => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, value),
    removeItem: (key) => saved.delete(key),
  };
  const page = loadTheme(storage);
  assert.equal(page.root.dataset.theme, undefined);
  page.ready();
  assert.equal(page.systemControl.attributes["aria-pressed"], "true");
  for (const mode of ["dark", "light"]) {
    page.toggle();
    assert.equal(page.root.dataset.theme, mode);
    const next = loadTheme(storage);
    // Restored before DOM ready, so the initial paint uses the saved theme.
    assert.equal(next.root.dataset.theme, mode);
    next.ready();
    assert.equal(next.control.attributes["aria-checked"], String(mode === "dark"));
  }
  page.reset();
  assert.equal(page.root.dataset.theme, undefined);
  assert.equal(saved.has(key), false);
  assert.equal(loadTheme(storage).root.dataset.theme, undefined);
});

test("unavailable storage allows switching themes for the current page", () => {
  const fail = () => { throw new Error("Storage blocked"); };
  const page = loadTheme({ getItem: fail, setItem: fail, removeItem: fail });
  page.ready();
  assert.equal(page.systemControl.attributes["aria-pressed"], "true");
  page.toggle();
  assert.equal(page.root.dataset.theme, "dark");
  page.reset();
  assert.equal(page.root.dataset.theme, undefined);
});

test("unknown stored preferences fall back to system", () => {
  const page = loadTheme({ getItem: () => "unknown" });
  page.ready();
  assert.equal(page.systemControl.attributes["aria-pressed"], "true");
  assert.equal(page.root.dataset.theme, undefined);
});

test("navigation preference is restored before DOM ready without affecting the theme", () => {
  for (const value of ["hidden", "shown", null, "unknown"]) {
    const page = loadTheme({ getItem: (key) => key === "archive-browser:sidebar" ? value : "dark" });
    assert.equal(page.root.dataset.sidebarHidden, String(value === "hidden"));
    assert.equal(page.root.dataset.theme, "dark");
  }
  const page = loadTheme({ getItem() { throw new Error("Storage blocked"); } });
  assert.equal(page.root.dataset.sidebarHidden, "false");
});

test("switch follows system changes until explicitly toggled, then follows again after reset", () => {
  const page = loadTheme({ getItem: () => null, setItem() {}, removeItem() {} }, true);
  page.ready();
  assert.equal(page.control.attributes["aria-checked"], "true");
  assert.equal(page.control.title, "Switch to light mode");
  page.changeSystem(false);
  assert.equal(page.control.attributes["aria-checked"], "false");
  page.toggle();
  assert.equal(page.root.dataset.theme, "dark");
  page.changeSystem(true);
  page.changeSystem(false);
  assert.equal(page.control.attributes["aria-checked"], "true");
  assert.equal(page.systemControl.attributes["aria-pressed"], "false");
  page.reset();
  assert.equal(page.control.attributes["aria-checked"], "false");
  assert.equal(page.root.dataset.theme, undefined);
  page.changeSystem(true);
  assert.equal(page.control.attributes["aria-checked"], "true");
  page.toggle();
  assert.equal(page.root.dataset.theme, "light");
});
