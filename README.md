# Photography Gallery

A static photo gallery (HTML/CSS/JS) that you can host on GitHub Pages. Photos are organized in a grid with tag-based filtering, date sorting, and a lightbox viewer. A set of local Python tools handles ingesting photos from disk, tagging them, and managing tags over time.

**Live example:** [djnarayanan-photos on GitHub Pages](https://dj-narayanan-19.github.io/djnarayanan-photos/)

---

## How it works

- `data/photos.json` is the photo database — it stores metadata, EXIF info, tags, and paths to derivatives.
- `assets/thumbs/` and `assets/display/` hold resized versions of your photos (originals are kept off-repo).
- The gallery reads these static files directly — no server needed.

```
repo/
  index.html
  gallery.html
  css/styles.css
  js/app.js
  data/
    photos.json       # photo database (tags, metadata, paths)
    tags.json         # derived tag index (auto-generated)
  assets/
    thumbs/           # small thumbnails (450px, fast to load)
    display/          # larger images (2000px, used in lightbox)
  tools/
    ingest.py         # scan originals, generate derivatives, launch tagger UI
    tag_maint.py      # rename, merge, or delete tags across all photos
    validate.py       # check data integrity
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/dj-narayanan-19/djnarayanan-photos.git
cd djnarayanan-photos
```

Keep your original photos folder **outside** the repo (to avoid committing large files):

```
parent-folder/
  djnarayanan-photos/   ← this repo
  my-originals/         ← your photos live here
    IMG_0001.jpg
    ...
```

### 2. Create a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install Flask Pillow
```

---

## Ingesting photos (`ingest.py`)

`ingest.py` scans your originals folder, generates resized derivatives, extracts EXIF data, and launches a local tagging UI in your browser.

### First run / clean slate

Use `--reset` to rebuild everything from scratch. This clears existing derivatives and metadata (backups are created automatically).

```bash
python3 tools/ingest.py --originals ../my-originals --repo-root . --reset
```

A browser window will open at `http://127.0.0.1:5050` — tag each photo and click **Next**. When done, click **Validate & Exit**.

### Adding new photos

Run without `--reset` to add only new photos (already-ingested photos are skipped):

```bash
python3 tools/ingest.py --originals ../my-originals --repo-root .
```

### Backfill without tagging

To regenerate missing derivatives or update metadata without opening the UI:

```bash
python3 tools/ingest.py --originals ../my-originals --repo-root . --no-tag
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--originals PATH` | *(required)* | Path to your original photos folder |
| `--repo-root PATH` | `.` | Path to the repo root |
| `--thumb-long-edge N` | `450` | Thumbnail max dimension (px) |
| `--display-long-edge N` | `2000` | Display image max dimension (px) |
| `--reset` | — | Clear and rebuild all derivatives and metadata |
| `--no-tag` | — | Backfill/generate derivatives without opening the UI |

### Commit after ingesting

```bash
git add data/photos.json data/tags.json assets/thumbs assets/display
git commit -m "Add new photos"
git push
```

---

## Managing tags (`tag_maint.py`)

`tag_maint.py` launches a local UI for bulk tag operations — useful for renaming inconsistent tags, merging duplicates, or cleaning up.

```bash
python3 tools/tag_maint.py --repo-root .
```

Opens at `http://127.0.0.1:5051`. From there you can:

- **Rename** a tag across all photos (e.g. `bw` → `black-and-white`)
- **Merge** multiple tags into one (e.g. `x100v` + `x100-v` → `x100v`)
- **Delete** a tag from all photos
- **Browse** all tags with usage counts and co-occurrence stats

All changes are backed up automatically before writing. After making changes, commit the updated data files:

```bash
git add data/photos.json data/tags.json
git commit -m "Update tags"
git push
```

---

## Validating data (`validate.py`)

Run this to check for missing derivatives, duplicate IDs, or orphaned files:

```bash
python3 tools/validate.py --repo-root .
```

To see orphaned files in `assets/` that aren't referenced by any photo:

```bash
python3 tools/validate.py --repo-root . --clean-orphans --dry-run
```

Remove them:

```bash
python3 tools/validate.py --repo-root . --clean-orphans
```

---

## Hosting on GitHub Pages

To publish your own version:

1. Fork or clone this repo into your own GitHub account.
2. Go to your repo on GitHub → **Settings** → **Pages**.
3. Under **Build and deployment**, select **Deploy from a branch**.
4. Choose `main` and `/ (root)`, then click **Save**.

Your gallery will be live at `https://<your-username>.github.io/<repo-name>/`.

For detailed instructions, see [GitHub's Pages documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site).
