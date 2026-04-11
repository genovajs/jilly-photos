#!/usr/bin/env python3
"""
Ingest + Local Tagger UI (Flask) with stable identity + validation + safe reruns.

Key improvements:
- Duplicate detection uses ORIGINAL SHA-256 when possible
- Secondary duplicate detection using legacy fingerprint (filename + sizeBytes + mtime)
  to upgrade entries that were previously keyed by display-hash
- --no-tag backfill prefers original matching; display-hash fallback only when unavoidable
- --reset wipes repo-generated photo state (assets/thumbs, assets/display, data/photos.json, data/tags.json)
  with backups, then proceeds with ingest.

Usage:
  source .venv/bin/activate
  python3 tools/ingest.py --originals "/path/to/originals" --repo-root .
  python3 tools/ingest.py --originals "/path/to/originals" --repo-root . --no-tag
  python3 tools/ingest.py --originals "/path/to/originals" --repo-root . --reset
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, redirect, render_template_string, request, send_file, url_for
from PIL import Image, ImageOps, ExifTags


SUPPORTED_EXTS = {".jpg", ".jpeg", ".jpeg", ".png", ".tif", ".tiff"}

DEFAULT_THUMB_LONG_EDGE = 450
DEFAULT_DISPLAY_LONG_EDGE = 2000

DATA_REL = Path("data/photos.json")
TAGS_REL = Path("data/tags.json")
THUMBS_REL = Path("assets/thumbs")
DISPLAY_REL = Path("assets/display")

HOST = "127.0.0.1"
PORT = 5050


# ----------------------------
# General utilities
# ----------------------------
def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_dirs(repo_root: Path) -> None:
    for p in [repo_root / THUMBS_REL, repo_root / DISPLAY_REL, repo_root / DATA_REL.parent]:
        p.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"schemaVersion": 1, "generatedAt": now_iso(), "photos": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Dict[str, Any]) -> None:
    data["generatedAt"] = now_iso()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def backup_file(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    bak = path.with_name(path.name + f".bak.{ts}")
    bak.write_bytes(path.read_bytes())
    return bak


def safe_open_image(path: Path) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img


def resize_long_edge(img: Image.Image, long_edge: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= long_edge:
        return img.copy()
    if w >= h:
        new_w = long_edge
        new_h = int(round(h * (long_edge / w)))
    else:
        new_h = long_edge
        new_w = int(round(w * (long_edge / h)))
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def to_jpeg(img: Image.Image, out_path: Path, quality: int) -> None:
    if img.mode != "RGB":
        img = img.convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="JPEG", quality=quality, optimize=True, progressive=True)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def parse_fingerprint(fp: str) -> Tuple[Optional[int], Optional[int]]:
    # legacy fingerprint format: "size:<st_size>_mtime:<int(st_mtime)>"
    try:
        m = re.match(r"size:(\d+)_mtime:(\d+)", fp)
        if not m:
            return None, None
        return int(m.group(1)), int(m.group(2))
    except Exception:
        return None, None


def fingerprint_tuple_from_entry(src: Dict[str, Any]) -> Optional[Tuple[str, int, int]]:
    """
    Returns (originalFilename, sizeBytes, mtime) if present, else tries legacy fingerprint.
    """
    fn = src.get("originalFilename")
    sizeb = src.get("sizeBytes")
    mtime = src.get("mtime")

    if isinstance(fn, str) and isinstance(sizeb, int) and isinstance(mtime, int):
        return (fn, sizeb, mtime)

    fp = src.get("fingerprint")
    if isinstance(fn, str) and isinstance(fp, str):
        s, t = parse_fingerprint(fp)
        if s is not None and t is not None:
            return (fn, s, t)

    return None


# ----------------------------
# EXIF utilities
# ----------------------------
def _to_float_ratio(val) -> Optional[float]:
    try:
        if isinstance(val, tuple) and len(val) == 2:
            num, den = val
            return float(num) / float(den) if den else None
        return float(val)
    except Exception:
        return None


def _format_exposure_time(val) -> Optional[str]:
    f = _to_float_ratio(val)
    if f is None:
        return None
    if f >= 1:
        return f"{f:g}s"
    denom = round(1 / f)
    return f"1/{denom}s" if denom > 0 else None


def _format_fnumber(val) -> Optional[str]:
    f = _to_float_ratio(val)
    if f is None:
        return None
    s = f"{f:.1f}".rstrip("0").rstrip(".")
    return f"f/{s}"


def _format_focal_length(val) -> Optional[str]:
    f = _to_float_ratio(val)
    if f is None:
        return None
    return f"{f:.0f}mm"


def get_exif_fields(img: Image.Image) -> Dict[str, Optional[str]]:
    try:
        exif = img.getexif()
        if not exif:
            return {
                "dateTaken": None,
                "cameraMake": None,
                "cameraModel": None,
                "lensModel": None,
                "exposureTime": None,
                "fNumber": None,
                "iso": None,
                "focalLength": None,
            }

        tag_map = {v: k for k, v in ExifTags.TAGS.items()}  # name -> id

        def get(tag_name: str):
            tid = tag_map.get(tag_name)
            return exif.get(tid) if tid is not None else None

        dt = get("DateTimeOriginal") or get("DateTime")
        if isinstance(dt, bytes):
            dt = dt.decode("utf-8", errors="ignore")
        if isinstance(dt, str) and ":" in dt[:10]:
            dt = dt.replace(":", "-", 2).replace(" ", "T", 1)

        make = get("Make")
        model = get("Model")
        lens = get("LensModel") or get("LensSpecification")

        if isinstance(make, bytes):
            make = make.decode("utf-8", errors="ignore")
        if isinstance(model, bytes):
            model = model.decode("utf-8", errors="ignore")
        if isinstance(lens, bytes):
            lens = lens.decode("utf-8", errors="ignore")

        exposure = _format_exposure_time(get("ExposureTime"))
        fnum = _format_fnumber(get("FNumber"))
        iso = get("ISOSpeedRatings") or get("PhotographicSensitivity")
        if isinstance(iso, (list, tuple)) and iso:
            iso = iso[0]
        iso_str = str(iso) if iso is not None else None

        focal = _format_focal_length(get("FocalLength"))

        out = {
            "dateTaken": dt if isinstance(dt, str) else None,
            "cameraMake": str(make).strip() if make else None,
            "cameraModel": str(model).strip() if model else None,
            "lensModel": str(lens).strip() if lens else None,
            "exposureTime": exposure,
            "fNumber": fnum,
            "iso": iso_str,
            "focalLength": focal,
        }

        for k, v in list(out.items()):
            if not v or v == "None":
                out[k] = None
        return out
    except Exception:
        return {
            "dateTaken": None,
            "cameraMake": None,
            "cameraModel": None,
            "lensModel": None,
            "exposureTime": None,
            "fNumber": None,
            "iso": None,
            "focalLength": None,
        }


# ----------------------------
# ID and tag helpers
# ----------------------------
def make_id(exif_date_taken: Optional[str], original_name: str) -> str:
    if exif_date_taken and len(exif_date_taken) >= 10:
        date_part = exif_date_taken[:10]
    else:
        date_part = time.strftime("%Y-%m-%d", time.localtime())
    base = slugify(Path(original_name).stem) or "photo"
    return f"{date_part}_{base}"


def uniquify_id(candidate: str, existing_ids: set[str]) -> str:
    if candidate not in existing_ids:
        return candidate
    i = 2
    while f"{candidate}-{i}" in existing_ids:
        i += 1
    return f"{candidate}-{i}"


def normalize_tag_list(tags: List[str]) -> List[str]:
    out: List[str] = []
    for t in tags:
        s = slugify(str(t))
        if s and s not in out:
            out.append(s)
    return out


# ----------------------------
# Tag index JSON
# ----------------------------
def generate_tag_index(photos_json: Dict[str, Any]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for ph in photos_json.get("photos", []):
        tags = ph.get("tags", [])
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, str) and t.strip():
                    counts[t] = counts.get(t, 0) + 1
    tags_sorted = sorted(counts.keys())
    return {
        "schemaVersion": 1,
        "generatedAt": now_iso(),
        "tags": [{"name": t, "count": counts[t]} for t in tags_sorted],
    }


def write_tag_index(repo_root: Path, photos_json: Dict[str, Any]) -> None:
    save_json(repo_root / TAGS_REL, generate_tag_index(photos_json))


# ----------------------------
# Validator (embedded + button hook)
# ----------------------------
def validate_repo(repo_root: Path, photos_json: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    photos = photos_json.get("photos", [])
    if not isinstance(photos, list):
        return {"errors": ["photos.json photos is not a list"], "warnings": []}

    seen_ids = set()
    for i, ph in enumerate(photos):
        pid = ph.get("id")
        if not pid or not isinstance(pid, str):
            errors.append(f"Photo at index {i} missing valid 'id'")
            continue
        if pid in seen_ids:
            errors.append(f"Duplicate id: {pid}")
        seen_ids.add(pid)

        paths = ph.get("paths", {}) or {}
        thumb = paths.get("thumb")
        display = paths.get("display")
        if not thumb or not isinstance(thumb, str):
            errors.append(f"{pid}: missing paths.thumb")
        else:
            if not (repo_root / thumb).exists():
                errors.append(f"{pid}: missing thumb file: {thumb}")
        if not display or not isinstance(display, str):
            errors.append(f"{pid}: missing paths.display")
        else:
            if not (repo_root / display).exists():
                errors.append(f"{pid}: missing display file: {display}")

        tags = ph.get("tags", [])
        if not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
            errors.append(f"{pid}: tags must be list[str]")

        src = ph.get("source", {}) or {}
        if not isinstance(src, dict):
            warnings.append(f"{pid}: source is not an object")
        else:
            if not src.get("hash"):
                warnings.append(f"{pid}: source.hash missing")
            if not src.get("originalFilename"):
                warnings.append(f"{pid}: source.originalFilename missing")
            if not src.get("importedAt"):
                warnings.append(f"{pid}: source.importedAt missing (recommended)")

    # orphan warnings
    thumbs_dir = repo_root / THUMBS_REL
    display_dir = repo_root / DISPLAY_REL
    referenced_thumbs = set()
    referenced_display = set()
    for ph in photos:
        paths = ph.get("paths", {}) or {}
        if isinstance(paths.get("thumb"), str):
            referenced_thumbs.add((repo_root / paths["thumb"]).resolve())
        if isinstance(paths.get("display"), str):
            referenced_display.add((repo_root / paths["display"]).resolve())

    if thumbs_dir.exists():
        for f in thumbs_dir.glob("*.jpg"):
            if f.resolve() not in referenced_thumbs:
                warnings.append(f"Orphan thumb not referenced: {f.relative_to(repo_root)}")
    if display_dir.exists():
        for f in display_dir.glob("*.jpg"):
            if f.resolve() not in referenced_display:
                warnings.append(f"Orphan display not referenced: {f.relative_to(repo_root)}")

    return {"errors": errors, "warnings": warnings}


# ----------------------------
# Pending queue
# ----------------------------
@dataclass
class PendingItem:
    id: str
    original_path: Path
    display_rel: str
    thumb_rel: str
    exif: Dict[str, Optional[str]]
    source: Dict[str, Any]  # includes hash + offline info
    original_filename: str


# ----------------------------
# Local Tagger UI (Flask)
# ----------------------------
TEMPLATE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Photo Tagger</title>
  <style>
    :root { --maxw: 1120px; --border: 1px solid rgba(0,0,0,.15); }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; background:#fff; color:#111; }
    header { position: sticky; top:0; background: rgba(255,255,255,.92); backdrop-filter: blur(8px); border-bottom: var(--border); z-index:10; }
    .wrap { max-width: var(--maxw); margin: 0 auto; padding: 12px 16px; }
    .row { display:flex; gap:16px; align-items:center; justify-content: space-between; flex-wrap: wrap; }
    .pill { display:inline-flex; gap:8px; align-items:center; padding:6px 10px; border: var(--border); border-radius: 999px; font-size: 12px; background:#f7f7f7; }
    main .wrap { display:grid; grid-template-columns: 1.6fr 1fr; gap: 16px; padding-top: 16px; }
    @media (max-width: 900px) { main .wrap { grid-template-columns: 1fr; } }
    .card { border: var(--border); border-radius: 16px; overflow:hidden; background:#fff; }
    .imgbox { background:#000; display:grid; place-items:center; min-height: 360px; }
    img { max-width:100%; max-height: 78vh; display:block; }
    .meta { padding: 10px 12px; border-top: var(--border); font-size: 12px; opacity:.88; line-height: 1.45; }
    .form { padding: 12px; display:grid; gap: 10px; }
    label { display:grid; gap: 6px; font-size: 12px; }
    input { padding: 10px 12px; border-radius: 12px; border: var(--border); font-size: 14px; }
    .chips { display:flex; flex-wrap: wrap; gap: 8px; }
    .chipbtn { padding: 7px 10px; border-radius: 999px; border: var(--border); background:#fff; cursor:pointer; font-size: 12px; }
    .btnrow { display:flex; gap:10px; flex-wrap: wrap; }
    .btn { padding: 10px 14px; border-radius: 12px; border: var(--border); background: #111; color:#fff; cursor:pointer; font-weight: 650; }
    .btn.secondary { background:#fff; color:#111; }
    .hint { font-size: 12px; opacity: .75; }
    .prevbox { border: var(--border); border-radius: 14px; padding: 10px; background:#fafafa; }
    .prevtags { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; opacity: .9; }
    .sectionTitle { font-size: 12px; margin: 6px 0 6px; opacity:.85; }
  </style>
</head>
<body>
<header>
  <div class="wrap row">
    <div class="row" style="gap:10px;">
      <div class="pill"><b>Queue</b> {{idx+1}} / {{total}}</div>
      <div class="pill"><b>ID</b> {{item.id}}</div>
    </div>
    <div class="row" style="gap:10px;">
      <button class="btn secondary" id="prevBtn" type="button">← Prev</button>
      <button class="btn secondary" id="nextBtn" type="button">Next →</button>
    </div>
  </div>
</header>

<main>
  <div class="wrap">
    <div class="card">
      <div class="imgbox">
        <img src="{{ url_for('serve_display', photo_id=item.id) }}" alt="photo"/>
      </div>
      <div class="meta">
        <div><b>Original:</b> {{item.original_filename}}</div>
        <div><b>Date:</b> {{item.exif.get('dateTaken') or '—'}} &nbsp; <b>Camera:</b> {{item.exif.get('cameraModel') or '—'}}</div>
        <div>
          <b>Shutter:</b> {{item.exif.get('exposureTime') or '—'}} &nbsp;
          <b>Aperture:</b> {{item.exif.get('fNumber') or '—'}} &nbsp;
          <b>ISO:</b> {{item.exif.get('iso') or '—'}} &nbsp;
          <b>Focal:</b> {{item.exif.get('focalLength') or '—'}}
        </div>
      </div>
    </div>

    <div class="card">
      <form class="form" id="tagForm">
        <div class="hint">Tags are space-separated. Click suggestions below. <b>Enter</b> saves.</div>

        <div class="prevbox">
          <div class="row" style="justify-content: space-between; gap:10px;">
            <div class="sectionTitle" style="margin:0;"><b>Previous tags</b></div>
            <button class="btn secondary" id="copyPrev" type="button">Copy previous tags (replace)</button>
          </div>
          <div class="prevtags" id="prevTagsText">None yet.</div>
        </div>

        <label>
          Tags (space-separated)
          <input id="tags" placeholder="e.g., film bw prague-2024 street night"/>
        </label>

        <div>
          <div class="sectionTitle">Tag suggestions (click)</div>
          <div class="chips" id="tagSuggestions"></div>
        </div>

        <div class="btnrow">
          <button class="btn" type="submit">Save & Next</button>
          <button class="btn secondary" id="skip" type="button">Skip</button>
        </div>
      </form>
    </div>
  </div>
</main>

<script>
  const idx = {{idx}};
  const total = {{total}};
  const defaultTags = {{ default_tags | tojson }};

  const tagsEl = document.getElementById("tags");
  const suggestionsEl = document.getElementById("tagSuggestions");
  const prevTagsText = document.getElementById("prevTagsText");
  const copyPrevBtn = document.getElementById("copyPrev");

  function parseTags(s) { return (s || "").trim().split(/\s+/).filter(Boolean); }
  function setTags(list) { tagsEl.value = (list || []).join(" ").trim(); }

  async function loadSuggestionBank() {
    const [bankRes, prevRes] = await Promise.all([fetch("/api/tagbank"), fetch("/api/prev")]);
    const bank = await bankRes.json();
    const prev = await prevRes.json();

    suggestionsEl.innerHTML = "";
    (bank.tags || []).slice(0, 40).forEach(t => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chipbtn";
      b.textContent = t.name;
      b.title = `${t.count} photo(s)`;
      b.addEventListener("click", () => {
        const cur = parseTags(tagsEl.value);
        if (!cur.includes(t.name)) cur.push(t.name);
        setTags(cur);
        tagsEl.focus();
      });
      suggestionsEl.appendChild(b);
    });

    if (prev.ok) {
      prevTagsText.textContent = prev.tagsText || "—";
      copyPrevBtn.disabled = false;
    } else {
      prevTagsText.textContent = "None yet.";
      copyPrevBtn.disabled = true;
    }
  }

  if (defaultTags && defaultTags.length) setTags(defaultTags);

  copyPrevBtn.addEventListener("click", async () => {
    const res = await fetch("/api/prev");
    const prev = await res.json();
    if (!prev.ok) return;
    setTags(prev.tags || []);
    tagsEl.focus();
  });

  document.getElementById("prevBtn").addEventListener("click", () => location.href = "/tag/" + Math.max(0, idx - 1));
  document.getElementById("nextBtn").addEventListener("click", () => location.href = "/tag/" + Math.min(total - 1, idx + 1));

  document.getElementById("skip").addEventListener("click", async () => {
    await fetch("/api/skip", { method: "POST" });
    location.href = (idx + 1 >= total) ? "/done" : ("/tag/" + (idx + 1));
  });

  document.getElementById("tagForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = { idx, tags: tagsEl.value.trim() };
    const res = await fetch("/api/save", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
    const out = await res.json();
    if (!out.ok) { alert(out.error || "Save failed"); return; }
    location.href = (idx + 1 >= total) ? "/done" : ("/tag/" + (idx + 1));
  });

  loadSuggestionBank();
</script>
</body>
</html>
"""

DONE_TEMPLATE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Done</title>
  <style>
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; }
    .wrap { max-width: 920px; margin: 0 auto; padding: 28px 18px; }
    .card { border: 1px solid rgba(0,0,0,.15); border-radius: 16px; padding: 18px; }
    .cmd { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; background:#f6f6f6; padding: 10px 12px; border-radius: 12px; }
    .btn { padding: 10px 14px; border-radius: 12px; border: 1px solid rgba(0,0,0,.15); background: #111; color:#fff; cursor:pointer; font-weight: 650; }
    .btn.secondary { background:#fff; color:#111; }
    .row { display:flex; gap:10px; flex-wrap: wrap; align-items:center; }
    pre { background:#f6f6f6; padding: 10px 12px; border-radius: 12px; overflow:auto; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h2>All set ✅</h2>
      <p>No more photos in the queue.</p>

      <div class="row">
        <form action="/validate-exit" method="post">
          <button class="btn" type="submit">Validate & Exit</button>
        </form>
        <a class="btn secondary" href="/exit">Exit without validate</a>
      </div>

      {% if report %}
        <h3>Validation report</h3>
        <pre>{{ report }}</pre>
      {% endif %}

      <p>Git steps:</p>
      <div class="cmd">git add data/photos.json data/tags.json assets/thumbs assets/display<br/>git commit -m "Add photos"<br/>git push</div>
    </div>
  </div>
</body>
</html>
"""


def create_app(
    pending: List[PendingItem],
    repo_root: Path,
    photos_json_path: Path,
    photos_json: Dict[str, Any],
) -> Flask:
    app = Flask(__name__)
    prev_state: Dict[str, Any] = {"ok": False}
    validation_cache: Optional[str] = None

    def prev_text(tags: List[str]) -> str:
        return "tags=" + " ".join(tags) if tags else "tags=—"

    def write_photos_json():
        bak = backup_file(photos_json_path)
        if bak:
            print(f"[backup] Wrote {bak.name}")
        save_json(photos_json_path, photos_json)
        write_tag_index(repo_root, photos_json)

    def shutdown_server():
        func = request.environ.get("werkzeug.server.shutdown")
        if func:
            func()

    @app.get("/")
    def root():
        if not pending:
            return redirect(url_for("done"))
        return redirect(url_for("tag_page", idx=0))

    @app.get("/tag/<int:idx>")
    def tag_page(idx: int):
        if not pending:
            return redirect(url_for("done"))
        idx = max(0, min(idx, len(pending) - 1))
        item = pending[idx]

        default_tags: List[str] = []
        if item.exif.get("cameraModel"):
            default_tags.append(slugify(item.exif["cameraModel"]))
        default_tags = normalize_tag_list(default_tags)

        return render_template_string(TEMPLATE, item=item, idx=idx, total=len(pending), default_tags=default_tags)

    @app.get("/display/<photo_id>")
    def serve_display(photo_id: str):
        for it in pending:
            if it.id == photo_id:
                return send_file(repo_root / it.display_rel)
        return ("Not found", 404)

    @app.get("/api/tagbank")
    def api_tagbank():
        return jsonify(generate_tag_index(photos_json))

    @app.get("/api/prev")
    def api_prev():
        if not prev_state.get("ok"):
            return jsonify({"ok": False})
        return jsonify({"ok": True, "tags": prev_state.get("tags", []), "tagsText": prev_text(prev_state.get("tags", []))})

    @app.post("/api/skip")
    def api_skip():
        return jsonify({"ok": True})

    @app.post("/api/save")
    def api_save():
        nonlocal photos_json, prev_state
        payload = request.get_json(force=True)
        idx = int(payload.get("idx", -1))
        if idx < 0 or idx >= len(pending):
            return jsonify({"ok": False, "error": "Bad idx"})

        it = pending[idx]
        tags_str = payload.get("tags", "")
        tags = normalize_tag_list(re.split(r"\s+", tags_str.strip())) if tags_str else []

        entry = {
            "id": it.id,
            "source": it.source,
            "paths": {"thumb": it.thumb_rel, "display": it.display_rel, "original": None},
            "exif": it.exif,
            "meta": {"title": None, "caption": None, "location": None},
            "tags": tags,
        }

        photos = photos_json.get("photos", [])
        replaced = False
        for i, ph in enumerate(photos):
            if ph.get("id") == it.id:
                entry["tags"] = tags if tags else ph.get("tags", [])
                photos[i] = entry
                replaced = True
                break
        if not replaced:
            photos.append(entry)

        photos_json["photos"] = photos
        write_photos_json()

        prev_state = {"ok": True, "tags": tags}
        return jsonify({"ok": True})

    @app.get("/done")
    def done():
        return render_template_string(DONE_TEMPLATE, report=validation_cache)

    @app.post("/validate-exit")
    def validate_exit():
        nonlocal validation_cache
        report = validate_repo(repo_root, photos_json)
        lines = []
        lines.append(f"Errors ({len(report['errors'])}):")
        lines += [f"  - {e}" for e in report["errors"]]
        lines.append("")
        lines.append(f"Warnings ({len(report['warnings'])}):")
        lines += [f"  - {w}" for w in report["warnings"]]
        validation_cache = "\n".join(lines)

        resp = render_template_string(DONE_TEMPLATE, report=validation_cache)
        shutdown_server()
        return resp

    @app.get("/exit")
    def exit_now():
        shutdown_server()
        return "Exiting… You can close this tab."

    return app


# ----------------------------
# Scanning + derivatives
# ----------------------------
def scan_originals(originals_dir: Path) -> List[Path]:
    paths = []
    for p in originals_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            paths.append(p)
    paths.sort()
    return paths


def generate_derivatives(repo_root: Path, original_path: Path, photo_id: str,
                         thumb_long_edge: int, display_long_edge: int) -> Tuple[str, str, Dict[str, Optional[str]]]:
    img = safe_open_image(original_path)
    exif_fields = get_exif_fields(img)

    display_img = resize_long_edge(img, display_long_edge)
    thumb_img = resize_long_edge(img, thumb_long_edge)

    thumb_rel = (THUMBS_REL / f"{photo_id}.jpg").as_posix()
    display_rel = (DISPLAY_REL / f"{photo_id}.jpg").as_posix()

    to_jpeg(thumb_img, repo_root / thumb_rel, quality=78)
    to_jpeg(display_img, repo_root / display_rel, quality=86)

    return thumb_rel, display_rel, exif_fields


def ensure_derivatives_missing(repo_root: Path, ph: Dict[str, Any]) -> bool:
    paths = ph.get("paths", {}) or {}
    thumb = paths.get("thumb")
    display = paths.get("display")
    if isinstance(thumb, str) and not (repo_root / thumb).exists():
        return True
    if isinstance(display, str) and not (repo_root / display).exists():
        return True
    return False


# ----------------------------
# Reset helpers
# ----------------------------
def reset_repo_state(repo_root: Path) -> None:
    """
    Destructive reset of generated photo artifacts + DB, with backups for JSON.
    """
    print("[reset] Resetting repo-generated state…")

    # 1) wipe derivatives folders
    for rel in [THUMBS_REL, DISPLAY_REL]:
        d = repo_root / rel
        if d.exists():
            for f in d.glob("*.jpg"):
                try:
                    f.unlink()
                except Exception:
                    pass
        d.mkdir(parents=True, exist_ok=True)

    # 2) reset photos.json
    photos_path = repo_root / DATA_REL
    bak = backup_file(photos_path)
    if bak:
        print(f"[reset] Backed up {DATA_REL} -> {bak.name}")
    save_json(photos_path, {"schemaVersion": 1, "generatedAt": now_iso(), "photos": []})
    print(f"[reset] Wrote empty {DATA_REL}")

    # 3) reset tags.json
    tags_path = repo_root / TAGS_REL
    bak2 = backup_file(tags_path)
    if bak2:
        print(f"[reset] Backed up {TAGS_REL} -> {bak2.name}")
    save_json(tags_path, {"schemaVersion": 1, "generatedAt": now_iso(), "tags": []})
    print(f"[reset] Wrote empty {TAGS_REL}")

    print("[reset] Done.")


# ----------------------------
# Main
# ----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--originals", required=True, help="Path to folder containing original JPEG exports")
    ap.add_argument("--repo-root", default=".", help="Repo root (default: .)")
    ap.add_argument("--thumb-long-edge", type=int, default=DEFAULT_THUMB_LONG_EDGE)
    ap.add_argument("--display-long-edge", type=int, default=DEFAULT_DISPLAY_LONG_EDGE)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-tag", action="store_true",
                    help="Do not launch tagger UI. Backfill hashes/source fields and create missing derivatives.")
    ap.add_argument("--reset", action="store_true",
                    help="Destructive reset: clears assets/thumbs, assets/display, data/photos.json, data/tags.json (with backups), then runs ingest.")
    args = ap.parse_args()

    originals_dir = Path(args.originals).expanduser().resolve()
    repo_root = Path(args.repo_root).expanduser().resolve()
    ensure_dirs(repo_root)

    if args.reset:
        reset_repo_state(repo_root)

    photos_json_path = repo_root / DATA_REL
    photos_json = load_json(photos_json_path)
    photos = photos_json.get("photos", [])
    if not isinstance(photos, list):
        print("[error] data/photos.json 'photos' is not a list")
        sys.exit(1)

    print(f"[info] Repo: {repo_root}")
    print(f"[info] Originals: {originals_dir}")
    originals = scan_originals(originals_dir)
    print(f"[scan] Found {len(originals)} original file(s)")

    # Build indexes from existing photos.json
    existing_ids = {ph.get("id") for ph in photos if isinstance(ph.get("id"), str)}

    hash_to_photo: Dict[str, Dict[str, Any]] = {}
    fp_to_photo: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
    for ph in photos:
        src = ph.get("source", {}) or {}
        if isinstance(src, dict):
            h = src.get("hash")
            if isinstance(h, str) and h:
                hash_to_photo[h] = ph
            ft = fingerprint_tuple_from_entry(src)
            if ft:
                fp_to_photo[ft] = ph

    # Precompute hashes for originals (sha of ORIGINAL file bytes)
    originals_info: List[Tuple[Path, str, int, int, str]] = []
    print("[hash] Computing SHA-256 of original files...")
    for i, p in enumerate(originals, start=1):
        st = p.stat()
        h = sha256_file(p)
        originals_info.append((p, p.name, st.st_size, int(st.st_mtime), h))
        if i % 25 == 0 or i == len(originals):
            print(f"  hashed {i}/{len(originals)}")

    # NO-TAG MODE: update metadata without launching UI
    if args.no_tag:
        print("[mode] --no-tag: backfilling metadata/hashes + creating missing derivatives (no UI)")

        updated = 0
        regen = 0
        upgraded = 0
        missing_original_matches = 0

        # quick lookups for originals
        hash_to_file = {h: (p, fn, size, mtime, h) for (p, fn, size, mtime, h) in originals_info}
        fp_to_file = {(fn, size, mtime): (p, fn, size, mtime, h) for (p, fn, size, mtime, h) in originals_info}

        for ph in photos:
            src = ph.get("source", {}) or {}
            if not isinstance(src, dict):
                src = {}
            pid = ph.get("id")

            # attempt to match an original
            file_tuple = None

            # A) by hash (if hashSource already original, try it)
            h0 = src.get("hash")
            hs0 = src.get("hashSource")
            if isinstance(h0, str) and h0 and hs0 == "original":
                file_tuple = hash_to_file.get(h0)

            # B) by fingerprint (preferred upgrade path)
            ft = fingerprint_tuple_from_entry(src)
            if not file_tuple and ft:
                file_tuple = fp_to_file.get(ft)

            # C) if hash exists but hashSource isn't original, still try lookup (maybe it already is)
            if not file_tuple and isinstance(h0, str) and h0:
                file_tuple = hash_to_file.get(h0)

            if file_tuple:
                p, fn, size, mtime, h = file_tuple
                changed = False

                # upgrade identity to ORIGINAL hash
                if src.get("hash") != h or src.get("hashSource") != "original":
                    src["hash"] = h
                    src["hashSource"] = "original"
                    changed = True
                    upgraded += 1

                # metadata
                src["originalFilename"] = fn
                src["sizeBytes"] = size
                src["mtime"] = mtime
                src["fingerprint"] = f"size:{size}_mtime:{mtime}"
                if not src.get("originalPathHint"):
                    src["originalPathHint"] = str(p)
                    changed = True
                if not src.get("importedAt"):
                    src["importedAt"] = now_iso()
                    changed = True

                ph["source"] = src

                # merge exif gaps
                try:
                    img = safe_open_image(p)
                    ex = get_exif_fields(img)
                    old = ph.get("exif", {}) or {}
                    merged = dict(old)
                    for k, v in ex.items():
                        if merged.get(k) is None and v is not None:
                            merged[k] = v
                    ph["exif"] = merged
                except Exception:
                    pass

                # regen derivatives if missing
                if ensure_derivatives_missing(repo_root, ph):
                    if isinstance(pid, str) and pid:
                        thumb_rel, display_rel, ex = generate_derivatives(
                            repo_root, p, pid, args.thumb_long_edge, args.display_long_edge
                        )
                        ph.setdefault("paths", {})
                        ph["paths"]["thumb"] = thumb_rel
                        ph["paths"]["display"] = display_rel
                        # merge exif
                        old = ph.get("exif", {}) or {}
                        merged = dict(old)
                        merged.update(ex)
                        ph["exif"] = merged
                        regen += 1

                if changed:
                    updated += 1

                continue

            # If we couldn't match originals, do NOT overwrite a known original identity.
            # Only keep existing info + ensure importedAt exists.
            missing_original_matches += 1
            if not src.get("importedAt"):
                src["importedAt"] = now_iso()
                ph["source"] = src
                updated += 1

        # add brand new files by original hash
        existing_hashes = {ph.get("source", {}).get("hash") for ph in photos if isinstance(ph.get("source", {}), dict)}
        existing_hashes = {h for h in existing_hashes if isinstance(h, str) and h}

        new_files = 0
        for (p, fn, size, mtime, h) in originals_info:
            if h in existing_hashes:
                continue

            try:
                img = safe_open_image(p)
                exif = get_exif_fields(img)
            except Exception:
                exif = {"dateTaken": None}

            candidate_id = make_id(exif.get("dateTaken"), fn)
            pid = uniquify_id(candidate_id, existing_ids)
            existing_ids.add(pid)

            thumb_rel, display_rel, ex = generate_derivatives(
                repo_root, p, pid, args.thumb_long_edge, args.display_long_edge
            )

            entry = {
                "id": pid,
                "source": {
                    "originalFilename": fn,
                    "hash": h,
                    "hashSource": "original",
                    "sizeBytes": size,
                    "mtime": mtime,
                    "fingerprint": f"size:{size}_mtime:{mtime}",
                    "originalPathHint": str(p),
                    "importedAt": now_iso(),
                },
                "paths": {"thumb": thumb_rel, "display": display_rel, "original": None},
                "exif": ex,
                "meta": {"title": None, "caption": None, "location": None},
                "tags": [],
            }
            photos.append(entry)
            new_files += 1

        photos_json["photos"] = photos

        bak = backup_file(photos_json_path)
        if bak:
            print(f"[backup] Wrote {bak.name}")
        save_json(photos_json_path, photos_json)
        write_tag_index(repo_root, photos_json)

        report = validate_repo(repo_root, photos_json)
        print("\n=== VALIDATE (post --no-tag) ===")
        print(f"Errors: {len(report['errors'])}")
        for e in report["errors"][:20]:
            print(f"  - {e}")
        if len(report["errors"]) > 20:
            print("  ...")
        print(f"Warnings: {len(report['warnings'])}")
        for w in report["warnings"][:20]:
            print(f"  - {w}")
        if len(report["warnings"]) > 20:
            print("  ...")

        print("\n=== BACKFILL STATS ===")
        print(f"Upgraded to original hash: {upgraded}")
        print(f"Updated entries: {updated}")
        print(f"Regenerated derivative sets: {regen}")
        print(f"New files added (untagged): {new_files}")
        print(f"Entries not matchable to originals this run: {missing_original_matches}")

        sys.exit(0)

    # NORMAL MODE: create pending items for NEW files by hash
    existing_hashes = set(hash_to_photo.keys())
    pending: List[PendingItem] = []

    upgraded_in_normal = 0
    regenerated_in_normal = 0

    print("[diff] Building queue of new photos (by hash; fallback by fingerprint upgrade)...")
    for (p, fn, size, mtime, h) in originals_info:
        # Primary match: original hash already present
        if h in existing_hashes:
            continue

        # Secondary match: legacy fingerprint match => upgrade existing entry rather than re-add
        fp_key = (fn, size, mtime)
        legacy_match = fp_to_photo.get(fp_key)
        if legacy_match:
            src = legacy_match.get("source", {}) or {}
            if not isinstance(src, dict):
                src = {}
            changed = False

            # Upgrade to original hash identity
            if src.get("hash") != h or src.get("hashSource") != "original":
                src["hash"] = h
                src["hashSource"] = "original"
                changed = True
                upgraded_in_normal += 1

            src["originalFilename"] = fn
            src["sizeBytes"] = size
            src["mtime"] = mtime
            src["fingerprint"] = f"size:{size}_mtime:{mtime}"
            if not src.get("originalPathHint"):
                src["originalPathHint"] = str(p)
                changed = True
            if not src.get("importedAt"):
                src["importedAt"] = now_iso()
                changed = True

            legacy_match["source"] = src

            # merge exif gaps
            try:
                img = safe_open_image(p)
                ex = get_exif_fields(img)
                old = legacy_match.get("exif", {}) or {}
                merged = dict(old)
                for k, v in ex.items():
                    if merged.get(k) is None and v is not None:
                        merged[k] = v
                legacy_match["exif"] = merged
            except Exception:
                pass

            # regenerate derivatives if missing
            pid = legacy_match.get("id")
            if ensure_derivatives_missing(repo_root, legacy_match):
                if isinstance(pid, str) and pid:
                    thumb_rel, display_rel, ex = generate_derivatives(
                        repo_root, p, pid, args.thumb_long_edge, args.display_long_edge
                    )
                    legacy_match.setdefault("paths", {})
                    legacy_match["paths"]["thumb"] = thumb_rel
                    legacy_match["paths"]["display"] = display_rel
                    # exif merge
                    old = legacy_match.get("exif", {}) or {}
                    merged = dict(old)
                    merged.update(ex)
                    legacy_match["exif"] = merged
                    regenerated_in_normal += 1

            # Update indexes so subsequent files can match by hash
            if changed:
                existing_hashes.add(h)
                hash_to_photo[h] = legacy_match

            # Treat as already ingested (skip queue)
            continue

        # Otherwise: genuinely new file => queue for tagging
        try:
            img = safe_open_image(p)
            exif = get_exif_fields(img)
        except Exception:
            exif = {"dateTaken": None}

        candidate_id = make_id(exif.get("dateTaken"), fn)
        pid = uniquify_id(candidate_id, existing_ids)
        existing_ids.add(pid)

        thumb_rel, display_rel, ex = generate_derivatives(
            repo_root, p, pid, args.thumb_long_edge, args.display_long_edge
        )

        src = {
            "originalFilename": fn,
            "hash": h,
            "hashSource": "original",
            "sizeBytes": size,
            "mtime": mtime,
            "fingerprint": f"size:{size}_mtime:{mtime}",
            "originalPathHint": str(p),
            "importedAt": now_iso(),
        }

        pending.append(PendingItem(
            id=pid,
            original_path=p,
            display_rel=display_rel,
            thumb_rel=thumb_rel,
            exif=ex,
            source=src,
            original_filename=fn
        ))

    if upgraded_in_normal or regenerated_in_normal:
        bak = backup_file(photos_json_path)
        if bak:
            print(f"[backup] Wrote {bak.name}")
        save_json(photos_json_path, photos_json)
        write_tag_index(repo_root, photos_json)
        print(f"[upgrade] Upgraded {upgraded_in_normal} existing entries to original-hash identity.")
        print(f"[upgrade] Regenerated {regenerated_in_normal} derivative sets during upgrade.")

    if not pending:
        print("[done] No new photos detected.")
        write_tag_index(repo_root, photos_json)
        report = validate_repo(repo_root, photos_json)
        print(f"[validate] Errors={len(report['errors'])} Warnings={len(report['warnings'])}")
        sys.exit(0)

    print(f"[queue] Prepared {len(pending)} new photo(s). Launching tagger at http://{args.host}:{args.port}")
    write_tag_index(repo_root, photos_json)

    app = create_app(pending, repo_root, photos_json_path, photos_json)

    try:
        import webbrowser
        webbrowser.open(f"http://{args.host}:{args.port}")
    except Exception:
        pass

    app.run(host=args.host, port=args.port, debug=False)
    print("[exit] Server stopped. You can now run git add/commit/push.")


if __name__ == "__main__":
    main()