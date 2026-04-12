const PHOTOS_URL = "data/photos.json";
const TAGS_URL   = "data/tags.json";
const PAGE_SIZE  = 30;
const STRIP_HALF = 3; // thumbnails each side of current photo in film strip

// DOM refs
const statusEl       = document.getElementById("status");
const gridEl         = document.getElementById("grid");
const loadMoreBtn    = document.getElementById("loadMoreBtn");
const sortBtnsEl     = document.getElementById("sortBtns");
const tagStripEl     = document.getElementById("tagStrip");
const activeChipsRow = document.getElementById("activeChipsRow");
const chipsListEl    = document.getElementById("chipsList");
const modeAndBtn     = document.getElementById("modeAnd");
const modeOrBtn      = document.getElementById("modeOr");

// Lightbox
const lightbox   = document.getElementById("lightbox");
const lbImg      = document.getElementById("lbImg");
const lbClose    = document.getElementById("lbClose");
const lbPrev     = document.getElementById("lbPrev");
const lbNext     = document.getElementById("lbNext");
const lbStripEl  = document.getElementById("lbStrip");
const lbMetaGrid = document.getElementById("lbMetaGrid");
const lbTagsEl   = document.getElementById("lbTags");

// State
let allPhotos     = [];
let viewPhotos    = [];
let selectedTags  = new Set();
let filterMode    = "AND";
let sortMode      = "date_desc";
let page          = 1;
let renderedCount = 0;
let lbIndex       = -1;
let sentinelEl    = null;
let observer      = null;
let isInitializing = true;

// ── Utilities ──────────────────────────────────────────────────

function parseDateTaken(p) {
  const dt = p?.exif?.dateTaken;
  if (!dt) return null;
  const t = Date.parse(dt);
  return Number.isNaN(t) ? null : t;
}

function formatDateReadable(dtStr) {
  if (!dtStr) return null;
  const t = Date.parse(dtStr);
  if (Number.isNaN(t)) return dtStr;
  return new Date(t).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit",
  });
}

function hasAllTags(photo, tags) {
  const pt = new Set(photo?.tags || []);
  for (const t of tags) if (!pt.has(t)) return false;
  return true;
}

function hasAnyTag(photo, tags) {
  const pt = new Set(photo?.tags || []);
  for (const t of tags) if (pt.has(t)) return true;
  return false;
}

function applyFilter(arr) {
  const tags = Array.from(selectedTags);
  if (tags.length === 0) return arr;
  if (filterMode === "AND") return arr.filter(p => hasAllTags(p, tags));
  return arr.filter(p => hasAnyTag(p, tags));
}

function sortPhotos(arr, mode) {
  const copy = [...arr];
  if (mode === "random") {
    for (let i = copy.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy;
  }
  copy.sort((a, b) => {
    const ta = parseDateTaken(a) ?? 0;
    const tb = parseDateTaken(b) ?? 0;
    return mode === "date_asc" ? (ta - tb) : (tb - ta);
  });
  return copy;
}

// ── URL state ──────────────────────────────────────────────────

function getUrlState() {
  const sp   = new URLSearchParams(window.location.search);
  const tags = (sp.get("tags") || "").split(",").map(t => t.trim()).filter(Boolean);
  const mode = (sp.get("mode") || "AND").toUpperCase() === "OR" ? "OR" : "AND";
  const sortRaw = sp.get("sort") ?? "";
  const sort = ["date_desc", "date_asc", "random"].includes(sortRaw) ? sortRaw : "date_desc";
  const p    = Math.max(1, parseInt(sp.get("page") || "1", 10) || 1);
  return { tags, mode, sort, page: p };
}

function setUrlState() {
  const sp = new URLSearchParams();
  if (selectedTags.size) sp.set("tags", [...selectedTags].join(","));
  sp.set("mode", filterMode);
  sp.set("sort", sortMode);
  sp.set("page", String(page));
  history.replaceState(null, "", `${window.location.pathname}?${sp.toString()}`);
}

// ── Sort ───────────────────────────────────────────────────────

function syncSortUI() {
  sortBtnsEl.querySelectorAll(".sort-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.sort === sortMode);
  });
}

// ── Tag strip ──────────────────────────────────────────────────

function buildTagStrip(tagIndex) {
  tagStripEl.innerHTML = "";

  const tags = (tagIndex?.tags || [])
    .slice()
    .sort((a, b) => (b.count ?? 0) - (a.count ?? 0) || String(a.name).localeCompare(String(b.name)));

  for (const t of tags) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tag-pill";
    btn.dataset.tag = t.name;

    const nameSpan = document.createElement("span");
    nameSpan.textContent = t.name;

    const countSpan = document.createElement("span");
    countSpan.className = "pill-count";
    countSpan.textContent = String(t.count ?? 0);

    btn.append(nameSpan, countSpan);

    btn.addEventListener("click", () => {
      if (selectedTags.has(t.name)) selectedTags.delete(t.name);
      else selectedTags.add(t.name);
      rebuildView();
    });

    tagStripEl.appendChild(btn);
  }

  syncTagStripUI();
}

function syncTagStripUI() {
  // Sync pill active states
  tagStripEl.querySelectorAll(".tag-pill").forEach(btn => {
    btn.classList.toggle("active", selectedTags.has(btn.dataset.tag));
  });

  // Rebuild chips
  chipsListEl.innerHTML = "";
  for (const tag of selectedTags) {
    const chip = document.createElement("span");
    chip.className = "chip";

    const label = document.createElement("span");
    label.textContent = tag;

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "chip-remove";
    removeBtn.innerHTML = "&#x2715;";
    removeBtn.setAttribute("aria-label", `Remove ${tag} filter`);
    removeBtn.addEventListener("click", () => {
      selectedTags.delete(tag);
      rebuildView();
    });

    chip.append(label, removeBtn);
    chipsListEl.appendChild(chip);
  }

  // Show/hide chips row
  activeChipsRow.classList.toggle("hidden", selectedTags.size === 0);

  // Sync AND/OR buttons
  modeAndBtn.classList.toggle("active", filterMode === "AND");
  modeOrBtn.classList.toggle("active", filterMode === "OR");
}

// ── Grid ───────────────────────────────────────────────────────

function rebuildView({ preservePage = false } = {}) {
  if (!preservePage) page = 1;
  viewPhotos    = sortPhotos(applyFilter(allPhotos), sortMode);
  renderedCount = 0;
  gridEl.innerHTML = "";
  render();
  if (!isInitializing) setUrlState();
  syncTagStripUI();
}

function render() {
  const total  = viewPhotos.length;
  const target = Math.min(total, page * PAGE_SIZE);

  statusEl.textContent = total === 0
    ? "No photos match your filter."
    : `${total} photo${total === 1 ? "" : "s"}`;

  if (renderedCount > target) {
    renderedCount = 0;
    gridEl.innerHTML = "";
  }

  for (let i = renderedCount; i < target; i++) {
    const p = viewPhotos[i];

    const tile = document.createElement("div");
    tile.className = "tile";
    tile.dataset.index = String(i);

    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = p.paths.thumb;
    img.alt = p.meta?.title ?? "";

    // Overlay with tag labels (visible on hover)
    const overlay = document.createElement("div");
    overlay.className = "tile-overlay";

    const tagsDiv = document.createElement("div");
    tagsDiv.className = "tile-tags";
    for (const tag of (p.tags || []).slice(0, 4)) {
      const s = document.createElement("span");
      s.className = "tile-tag";
      s.textContent = tag;
      tagsDiv.appendChild(s);
    }

    overlay.appendChild(tagsDiv);
    tile.append(img, overlay);
    tile.addEventListener("click", () => openLightbox(i));
    gridEl.appendChild(tile);
  }

  renderedCount = target;
  loadMoreBtn.style.display = renderedCount < total ? "flex" : "none";
}

// ── Lightbox ───────────────────────────────────────────────────

function buildExposureStr(p) {
  const parts = [];
  if (p?.exif?.exposureTime) parts.push(p.exif.exposureTime + "s");
  if (p?.exif?.fNumber)      parts.push("f/" + p.exif.fNumber);
  if (p?.exif?.iso)          parts.push("ISO " + p.exif.iso);
  if (p?.exif?.focalLength)  parts.push(p.exif.focalLength + "mm");
  return parts.length ? parts.join(" · ") : null;
}

function buildFilmStrip(centerIndex) {
  lbStripEl.innerHTML = "";

  const total = viewPhotos.length;
  const start = Math.max(0, centerIndex - STRIP_HALF);
  const end   = Math.min(total - 1, centerIndex + STRIP_HALF);

  for (let i = start; i <= end; i++) {
    const p   = viewPhotos[i];
    const img = document.createElement("img");
    img.className = "lb-strip-thumb" + (i === centerIndex ? " active" : "");
    img.src     = p.paths.thumb;
    img.alt     = "";
    img.loading = "lazy";
    img.dataset.index = String(i);
    img.addEventListener("click", () => openLightbox(i));
    lbStripEl.appendChild(img);
  }

  // Scroll active thumb into view
  const activeThumb = lbStripEl.querySelector(".active");
  if (activeThumb) {
    activeThumb.scrollIntoView({ inline: "center", block: "nearest" });
  }
}

function openLightbox(index) {
  lbIndex = index;
  const p = viewPhotos[index];

  lbImg.src = p.paths.display;

  buildFilmStrip(index);

  // Metadata cells
  lbMetaGrid.innerHTML = "";
  const metaFields = [
    { key: "Date",     val: formatDateReadable(p?.exif?.dateTaken) },
    { key: "Camera",   val: p?.exif?.cameraModel || p?.exif?.cameraMake || null },
    { key: "Lens",     val: p?.exif?.lensModel || null },
    { key: "Exposure", val: buildExposureStr(p) },
  ];
  for (const m of metaFields) {
    if (!m.val) continue;
    const cell = document.createElement("div");
    cell.className = "lb-meta-cell";
    const key = document.createElement("div");
    key.className = "lb-meta-key";
    key.textContent = m.key;
    const val = document.createElement("div");
    val.className = "lb-meta-val";
    val.textContent = m.val;
    cell.append(key, val);
    lbMetaGrid.appendChild(cell);
  }

  // Tags — gold tint if tag matches an active filter
  lbTagsEl.innerHTML = "";
  for (const tag of (p?.tags || [])) {
    const span = document.createElement("span");
    span.className = "lb-tag" + (selectedTags.has(tag) ? " filter-match" : "");
    span.textContent = tag;
    lbTagsEl.appendChild(span);
  }

  lightbox.classList.remove("hidden");
  lightbox.setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
}

function closeLightbox() {
  lightbox.classList.add("hidden");
  lightbox.setAttribute("aria-hidden", "true");
  lbImg.src = "";
  lbIndex = -1;
  document.body.style.overflow = "";
}

function lbStep(dir) {
  if (lbIndex < 0) return;
  const next = lbIndex + dir;
  if (next < 0 || next >= viewPhotos.length) return;
  openLightbox(next);
}

// ── Swipe support ──────────────────────────────────────────────

let swipeStartX = 0, swipeStartY = 0, swipeActive = false;

function onLbTouchStart(e) {
  if (!e.touches || e.touches.length !== 1) return;
  swipeStartX = e.touches[0].clientX;
  swipeStartY = e.touches[0].clientY;
  swipeActive = true;
}

function onLbTouchMove(e) {
  if (!swipeActive || !e.touches || e.touches.length !== 1) return;
  const dx = Math.abs(e.touches[0].clientX - swipeStartX);
  const dy = Math.abs(e.touches[0].clientY - swipeStartY);
  if (dx > dy && dx > 10) e.preventDefault();
}

function onLbTouchEnd(e) {
  if (!swipeActive) return;
  swipeActive = false;
  const changed = e.changedTouches?.[0];
  if (!changed) return;
  const dx = changed.clientX - swipeStartX;
  const dy = changed.clientY - swipeStartY;
  if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy)) return;
  lbStep(dx < 0 ? 1 : -1);
}

function onKey(e) {
  if (lightbox.classList.contains("hidden")) return;
  if (e.key === "Escape")     closeLightbox();
  if (e.key === "ArrowLeft")  lbStep(-1);
  if (e.key === "ArrowRight") lbStep(1);
}

// ── Infinite scroll ────────────────────────────────────────────

function ensureInfiniteScroll() {
  if (!sentinelEl) {
    sentinelEl = document.createElement("div");
    sentinelEl.style.cssText = "height:1px;width:100%;";
    gridEl.after(sentinelEl);
  }

  if (observer) observer.disconnect();

  observer = new IntersectionObserver(entries => {
    if (!entries[0]?.isIntersecting) return;
    if (renderedCount < viewPhotos.length) {
      page += 1;
      render();
      if (!isInitializing) setUrlState();
    }
  }, { rootMargin: "1200px 0px", threshold: 0.01 });

  observer.observe(sentinelEl);
}

// ── Init ───────────────────────────────────────────────────────

async function init() {
  try {
    const [photosRes, tagsRes] = await Promise.all([
      fetch(PHOTOS_URL, { cache: "no-cache" }),
      fetch(TAGS_URL,   { cache: "no-cache" }).catch(() => null),
    ]);

    if (!photosRes.ok) throw new Error(`photos.json: ${photosRes.status}`);
    const photosData = await photosRes.json();
    allPhotos = Array.isArray(photosData.photos) ? photosData.photos : [];

    let tagIndex = null;
    if (tagsRes?.ok) {
      tagIndex = await tagsRes.json();
    } else {
      // Derive tag counts from photos if tags.json unavailable
      const counts = {};
      for (const p of allPhotos)
        for (const t of (p.tags || []))
          counts[t] = (counts[t] || 0) + 1;
      tagIndex = { tags: Object.keys(counts).sort().map(k => ({ name: k, count: counts[k] })) };
    }

    buildTagStrip(tagIndex);

    const url    = getUrlState();
    sortMode     = url.sort;
    filterMode   = url.mode;
    selectedTags = new Set(url.tags);
    page         = url.page;

    syncSortUI();
    syncTagStripUI();
    ensureInfiniteScroll();

    // Sort buttons
    sortBtnsEl.addEventListener("click", e => {
      const btn = e.target.closest(".sort-btn");
      if (!btn) return;
      sortMode = btn.dataset.sort;
      syncSortUI();
      rebuildView();
    });

    // AND / OR
    modeAndBtn.addEventListener("click", () => {
      filterMode = "AND";
      rebuildView();
    });
    modeOrBtn.addEventListener("click", () => {
      filterMode = "OR";
      rebuildView();
    });

    // Load more button
    loadMoreBtn.addEventListener("click", () => {
      page += 1;
      render();
      if (!isInitializing) setUrlState();
    });

    // Lightbox controls
    lbClose.addEventListener("click", closeLightbox);
    lbPrev.addEventListener("click", () => lbStep(-1));
    lbNext.addEventListener("click", () => lbStep(1));

    // Swipe
    lightbox.addEventListener("touchstart", onLbTouchStart, { passive: true });
    lightbox.addEventListener("touchmove",  onLbTouchMove,  { passive: false });
    lightbox.addEventListener("touchend",   onLbTouchEnd,   { passive: true });

    // Click backdrop to close
    lightbox.addEventListener("click", e => {
      if (e.target === lightbox) closeLightbox();
    });

    document.addEventListener("keydown", onKey);

    isInitializing = false;
    rebuildView({ preservePage: true });
    setUrlState();

  } catch (err) {
    statusEl.textContent = "Failed to load gallery data.";
    console.error(err);
  }
}

init();
