document.querySelectorAll("details[data-disclosure]").forEach((details) => {
  const key = `archive-browser:disclosure:${details.dataset.disclosure}`;
  try {
    const saved = localStorage.getItem(key);
    if (saved === "open" || saved === "closed") {
      details.open = saved === "open";
    }
  } catch {
    // Keep the default state when browser storage is unavailable.
  }

  details.addEventListener("toggle", () => {
    try {
      localStorage.setItem(key, details.open ? "open" : "closed");
    } catch {
      // Sections still work when browser storage is unavailable.
    }
  });
});

const sidebarToggle = document.querySelector("#sidebar-toggle");

if (sidebarToggle) {
  const sidebarPanel = document.querySelector("#archive-sidebar");
  const key = "archive-browser:sidebar";
  const setSidebarVisible = (visible) => {
    sidebarPanel.hidden = !visible;
    document.documentElement.dataset.sidebarHidden = String(!visible);
    sidebarToggle.setAttribute("aria-expanded", String(visible));
    const label = visible ? "Hide navigation" : "Show navigation";
    sidebarToggle.querySelector("span").textContent = label;
    sidebarToggle.setAttribute("aria-label", label);
    sidebarToggle.title = label;
  };
  let visible = true;
  try { visible = localStorage.getItem(key) !== "hidden"; } catch {}
  setSidebarVisible(visible);
  sidebarToggle.disabled = false;
  sidebarToggle.addEventListener("click", () => {
    const visible = sidebarPanel.hidden;
    setSidebarVisible(visible);
    try { localStorage.setItem(key, visible ? "shown" : "hidden"); } catch {}
  });
}

const filter = document.querySelector("#filter");

if (filter) {
  filter.addEventListener("input", () => {
    const query = filter.value.toLocaleLowerCase();
    document.querySelectorAll(filter.dataset.filterTarget).forEach((item) => {
      item.hidden = !item.dataset.search.includes(query);
    });
  });
}

const sections = document.querySelector("#sections");
const activeSection = sections?.querySelector(".section.active");

if (activeSection) {
  requestAnimationFrame(() => {
    sections.scrollTop +=
      activeSection.getBoundingClientRect().top - sections.getBoundingClientRect().top - 8;
  });
}

const page = document.querySelector("main[data-previous-url]");
const reviewerInput = document.querySelector("#reviewer-name");

if (reviewerInput) {
  const reviewerKey = "archive-browser:reviewer-name";
  const editor = document.querySelector("#reviewer-editor");
  const summary = document.querySelector("#reviewer-summary");
  const tools = document.querySelector("#reviewer-tools");
  const displayName = document.querySelector("#reviewer-display-name");
  const start = document.querySelector("#reviewer-start");
  const change = document.querySelector("#reviewer-change");
  const cancel = document.querySelector("#reviewer-cancel");
  const stop = document.querySelector("#reviewer-stop");
  const status = document.querySelector("#reviewer-status");
  const validName = (name) => name.length > 0 && name.length <= 100;
  let reviewerName = "";
  try { reviewerName = (localStorage.getItem(reviewerKey) || "").trim(); } catch {}
  if (!validName(reviewerName)) reviewerName = "";
  reviewerInput.value = reviewerName;

  const renderReviewer = (editing = false) => {
    editor.hidden = Boolean(reviewerName) && !editing;
    summary.hidden = !reviewerName || editing;
    tools.hidden = !reviewerName || editing;
    displayName.textContent = reviewerName;
    start.textContent = reviewerName ? "Save name" : "Start reviewing";
    cancel.hidden = !reviewerName;
    stop.hidden = !reviewerName;
    status.textContent = "";
    status.hidden = true;
  };
  const nameError = () => {
    status.textContent = "Enter a reviewer name (1–100 characters) before making edits.";
    status.hidden = false;
    reviewerInput.focus();
  };
  renderReviewer();
  editor.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = reviewerInput.value.trim();
    if (!validName(name)) {
      nameError();
      return;
    }
    reviewerName = name;
    reviewerInput.value = name;
    try { localStorage.setItem(reviewerKey, name); } catch {}
    renderReviewer();
    change.focus();
  });
  change.addEventListener("click", () => {
    reviewerInput.value = reviewerName;
    renderReviewer(true);
    reviewerInput.focus();
  });
  cancel.addEventListener("click", () => {
    reviewerInput.value = reviewerName;
    renderReviewer();
    change.focus();
  });
  stop.addEventListener("click", () => {
    reviewerName = "";
    reviewerInput.value = "";
    try { localStorage.removeItem(reviewerKey); } catch {}
    renderReviewer();
    reviewerInput.focus();
  });
  tools.querySelectorAll("form[data-review-edit]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!validName(reviewerName) || !editor.hidden) {
        event.preventDefault();
        renderReviewer(true);
        nameError();
        return;
      }
      form.querySelector('[name="reviewer_name"]').value = reviewerName;
    });
  });
}

const imagePopup = document.querySelector("#image-popup");

if (imagePopup) {
  const openImage = () => {
    if (imagePopup.open) return;
    const image = imagePopup.querySelector("img");
    image.src = image.dataset.originalUrl;
    imagePopup.showModal();
  };
  document.querySelector(".image-card").addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    if (imagePopup.open) return;
    history.pushState({ ...history.state, archiveImagePopup: true }, "");
    openImage();
  });
  window.addEventListener("popstate", () => {
    if (history.state?.archiveImagePopup) {
      openImage();
    } else if (imagePopup.open) {
      imagePopup.close();
    }
  });
  imagePopup.addEventListener("close", () => {
    if (history.state?.archiveImagePopup) history.back();
  });
  imagePopup.addEventListener("click", (event) => {
    if (event.target === imagePopup) imagePopup.close();
  });
  if (history.state?.archiveImagePopup) openImage();
}

const promptsPanel = document.querySelector("#prompts-used");

if (promptsPanel) {
  promptsPanel.querySelectorAll("[data-copy-prompt]").forEach((button) => {
    button.addEventListener("click", async () => {
      const content = document.getElementById(button.dataset.copyPrompt);
      const status = button.nextElementSibling;
      try {
        await navigator.clipboard.writeText(button.dataset.promptText ?? content.textContent);
        status.textContent = "Copied";
      } catch {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(content);
        selection.removeAllRanges();
        selection.addRange(range);
        status.textContent = "Text selected. Press Ctrl+C or ⌘C to copy.";
      }
    });
  });
}

if (page) {
  document.addEventListener("keydown", (event) => {
    if (imagePopup?.open) return;
    if (event.target.matches("input, textarea, select")) return;
    if (event.key === "ArrowLeft" && page.dataset.previousUrl) {
      location.href = page.dataset.previousUrl;
    }
    if (event.key === "ArrowRight" && page.dataset.nextUrl) {
      location.href = page.dataset.nextUrl;
    }
  });
}
