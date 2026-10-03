"""Training videos: files live in the videos folder next to the app; titles and descriptions in videos.json."""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO_DIR = os.path.join(HERE, "videos")
META_PATH = os.path.join(VIDEO_DIR, "videos.json")
TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".ogv": "video/ogg"}
# A PDF with the same name as a video shows as its printable steps.
SERVE_TYPES = {**TYPES, ".pdf": "application/pdf"}

# Videos we know are on the way. A card shows "coming soon" until a matching file is added.
COMING_SOON = [
    {"match": "facebook", "title": "Facebook ad setup walkthrough",
     "description": "Watch how to set up your Facebook lead ad, step by step."},
]


def _ensure():
    os.makedirs(VIDEO_DIR, exist_ok=True)


def _meta():
    try:
        with open(META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_meta(meta):
    _ensure()
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def nice_title(filename):
    base = os.path.splitext(filename)[0]
    return re.sub(r"[_\-]+", " ", base).strip().capitalize() or "Training video"


def list_videos():
    _ensure()
    meta = _meta()
    files = sorted(
        (f for f in os.listdir(VIDEO_DIR) if os.path.splitext(f)[1].lower() in TYPES),
        key=lambda f: meta.get(f, {}).get("added", "") or str(os.path.getmtime(os.path.join(VIDEO_DIR, f))),
    )
    videos = [{
        "file": f,
        "title": meta.get(f, {}).get("title") or nice_title(f),
        "description": meta.get(f, {}).get("description", ""),
        "size_mb": round(os.path.getsize(os.path.join(VIDEO_DIR, f)) / 1_000_000, 1),
        "pdf": os.path.splitext(f)[0] + ".pdf" if os.path.isfile(os.path.join(VIDEO_DIR, os.path.splitext(f)[0] + ".pdf")) else None,
    } for f in files]
    soon = [c for c in COMING_SOON if not any(c["match"] in v["file"].lower() or c["match"] in v["title"].lower() for v in videos)]
    return {"videos": videos, "coming_soon": [{"title": c["title"], "description": c["description"]} for c in soon],
            "folder": VIDEO_DIR}


def safe_name(name):
    name = os.path.basename(name or "")
    base, ext = os.path.splitext(name)
    base = re.sub(r"[^\w\- ]+", "", base).strip() or "video"
    return base[:80] + ext.lower()


def video_path(name):
    name = os.path.basename(name)
    path = os.path.join(VIDEO_DIR, name)
    if os.path.splitext(name)[1].lower() in SERVE_TYPES and os.path.isfile(path):
        return path
    return None


def start_upload(filename):
    """Returns (final filename, open file) for a new upload, never overwriting an existing video."""
    _ensure()
    name = safe_name(filename)
    if os.path.splitext(name)[1] not in TYPES:
        raise ValueError("That isn't a video file. Use an .mp4, .mov, .m4v or .webm video.")
    base, ext = os.path.splitext(name)
    n = 2
    while os.path.exists(os.path.join(VIDEO_DIR, name)):
        name = f"{base} {n}{ext}"
        n += 1
    return name, open(os.path.join(VIDEO_DIR, name), "wb")


def set_info(filename, title=None, description=None, added=None):
    meta = _meta()
    entry = meta.setdefault(os.path.basename(filename), {})
    if title is not None:
        entry["title"] = title.strip()[:120]
    if description is not None:
        entry["description"] = description.strip()[:300]
    if added:
        entry["added"] = added
    _save_meta(meta)


def delete(filename):
    path = video_path(filename)
    if path:
        os.remove(path)
        pdf = os.path.splitext(path)[0] + ".pdf"
        if os.path.isfile(pdf):
            os.remove(pdf)
    meta = _meta()
    meta.pop(os.path.basename(filename), None)
    _save_meta(meta)
