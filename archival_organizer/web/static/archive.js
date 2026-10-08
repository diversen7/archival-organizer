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

if (page) {
  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, textarea, select")) return;
    if (event.key === "ArrowLeft" && page.dataset.previousUrl) {
      location.href = page.dataset.previousUrl;
    }
    if (event.key === "ArrowRight" && page.dataset.nextUrl) {
      location.href = page.dataset.nextUrl;
    }
  });
}
