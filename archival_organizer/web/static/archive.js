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
