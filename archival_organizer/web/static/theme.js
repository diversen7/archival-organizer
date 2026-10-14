(() => {
  const key = "archive-browser:theme";
  const root = document.documentElement;
  let preference = "system";
  try {
    const saved = localStorage.getItem(key);
    if (saved === "light" || saved === "dark") preference = saved;
  } catch {
    // Follow the system preference when browser storage is unavailable.
  }

  const apply = () => {
    if (preference === "system") delete root.dataset.theme;
    else root.dataset.theme = preference;
  };
  // Apply saved choices before the stylesheet loads to avoid a flash of the wrong theme.
  apply();

  document.addEventListener("DOMContentLoaded", () => {
    const control = document.querySelector("#theme-mode");
    const systemControl = document.querySelector("#theme-system");
    const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
    const isDark = () => preference === "dark" || (preference === "system" && systemTheme.matches);
    const updateControls = () => {
      control.setAttribute("aria-checked", String(isDark()));
      control.title = isDark() ? "Switch to light mode" : "Switch to dark mode";
      systemControl.setAttribute("aria-pressed", String(preference === "system"));
    };
    const choose = (mode) => {
      preference = mode;
      apply();
      updateControls();
      try {
        if (preference === "system") localStorage.removeItem(key);
        else localStorage.setItem(key, preference);
      } catch {
        // Switching still works for this page when storage is unavailable.
      }
    };
    control.disabled = false;
    systemControl.disabled = false;
    updateControls();
    control.addEventListener("click", () => choose(isDark() ? "light" : "dark"));
    systemControl.addEventListener("click", () => choose("system"));
    systemTheme.addEventListener("change", updateControls);
  });
})();
