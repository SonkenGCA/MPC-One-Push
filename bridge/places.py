"""Live's Places folders (the folders you add to the browser yourself, e.g. your drum kits).

Live 12.4's script API reports browser.user_folders as an empty list, and Live's browser skips
directory junctions (tested 2026-10-29), so AbletonOSC can't reach these folders at all. Instead the
app reads the folder list from Live's Library.cfg (read-only), lists the folders straight from disk,
and loads samples by path through the add-on's /live/track/load_sample.
"""

import glob
import os
import re

PREFS_ROOT = os.path.join(os.environ.get("APPDATA", ""), "Ableton")
SAMPLE_EXTS = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}


def library_cfg():
    found = glob.glob(os.path.join(PREFS_ROOT, "Live *", "Preferences", "Library.cfg"))
    return max(found, key=os.path.getmtime) if found else None


def read():
    """[(name, path), ...] of the user's Places folders, from Live's Library.cfg."""
    cfg = library_cfg()
    if not cfg:
        return []
    with open(cfg, encoding="utf-8", errors="replace") as f:
        text = f.read()
    folders = []
    for tag in re.findall(r"<UserFolderInfo\b[^>]*>", text):
        path = re.search(r'Path="([^"]*)"', tag)
        name = re.search(r'DisplayName="([^"]*)"', tag)
        if path:
            p = path.group(1)
            folders.append(((name.group(1) if name and name.group(1) else os.path.basename(p)), p))
    return folders


def places():
    """[{name, path, exists}] for every Places folder Live knows about."""
    return [{"name": n, "path": p, "exists": os.path.isdir(p)} for n, p in read()]


def is_sample(name):
    return os.path.splitext(name)[1].lower() in SAMPLE_EXTS


def list_dir(path):
    """Folder contents in the browser's format: folders first as "[F] name", then files."""
    dirs, files = [], []
    for e in os.scandir(path):
        if e.name.startswith((".", "$")):
            continue
        try:
            (dirs if e.is_dir() else files).append(e.name)
        except OSError:
            continue
    return ["[F] " + d for d in sorted(dirs, key=str.lower)] + sorted(files, key=str.lower)


def sample_index():
    """[(searchable text, file path, place name, folder inside the place)] for every sample in your Places
    folders. ~30,000 files take about half a second once the drive is awake."""
    out = []
    for p in places():
        if not p["exists"]:
            continue
        base = p["path"]
        for root, _dirs, files in os.walk(base):
            rel = os.path.relpath(root, base)
            rel = "" if rel == "." else rel
            for f in files:
                if is_sample(f) and not f.startswith((".", "$")):
                    out.append(((rel + " " + os.path.splitext(f)[0]).lower(), os.path.join(root, f), p["name"], rel))
    return out


def search(index, query, limit=80):
    """Samples whose folder + name contain every word of the query; the name matching first, then shorter."""
    words = query.lower().split()
    if not words:
        return []
    hits = []
    for text, path, place, rel in index:
        if all(w in text for w in words):
            name = os.path.splitext(os.path.basename(path))[0].lower()
            hits.append((not all(w in name for w in words), len(name), path, place, rel))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [(path, place, rel) for _a, _b, path, place, rel in hits[:limit]]
