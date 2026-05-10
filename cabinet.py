#!/usr/bin/env python3
"""Build and serve a local Quarto note cabinet."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "raw"
TEMPLATE_DIR = ROOT / "template"
SITE_DIR = ROOT / "site"
NOTE_OUTPUT_DIR = "_manuscript"
CACHE_FILE = SITE_DIR / ".cabinet-cache.json"
GENERATED_DIRS = {".quarto", "_freeze", "_manuscript"}


@dataclass(frozen=True)
class Note:
    slug: str
    path: Path
    title: str
    author: str
    modified: float

    @property
    def parent_slug(self) -> str:
        parent = str(PurePosixPath(self.slug).parent)
        return "" if parent == "." else parent


def run(args: list[str], cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd or ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def slugify_segment(value: str) -> str:
    segment = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not segment:
        raise SystemExit("Each note path segment must contain at least one letter or number.")
    return segment


def normalize_note_path(value: str) -> str:
    value = value.strip().strip("/")
    value = re.sub(r"^raw/", "", value)
    value = re.sub(r"^site/", "", value)
    value = re.sub(r"/index(?:\.qmd|\.html)?$", "", value)
    value = re.sub(r"\.qmd$", "", value)
    value = re.sub(r"\.html$", "", value)
    parts = [slugify_segment(part) for part in value.split("/") if part and part != "."]
    if not parts:
        raise SystemExit("Note path must contain at least one segment.")
    return "/".join(parts)


def note_dirs() -> list[Path]:
    if not RAW_DIR.exists():
        return []
    dirs: list[Path] = []
    for index in RAW_DIR.rglob("index.qmd"):
        relative_parts = index.relative_to(RAW_DIR).parts[:-1]
        if any(part.startswith(".") or part.startswith("_") for part in relative_parts):
            continue
        dirs.append(index.parent)
    return sorted(dirs)


def strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def metadata_from_qmd(note_dir: Path) -> tuple[str, str]:
    text = (note_dir / "index.qmd").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", text, flags=re.S)
    title = note_dir.name
    author = ""
    if not match:
        return title, author
    yaml = match.group(1).splitlines()
    for index, line in enumerate(yaml):
        if line.startswith("title:"):
            title = strip_quotes(line.split(":", 1)[1])
        elif re.match(r"\s*-\s+name:", line):
            author = strip_quotes(line.split(":", 1)[1])
        elif line.startswith("author:") and index + 1 < len(yaml) and not author:
            inline = line.split(":", 1)[1].strip()
            if inline:
                author = strip_quotes(inline)
    return title, author


def notes_by_slug(notes: list[Note]) -> dict[str, Note]:
    return {note.slug: note for note in notes}


def notes_by_basename(notes: list[Note]) -> dict[str, list[Note]]:
    grouped: dict[str, list[Note]] = {}
    for note in notes:
        grouped.setdefault(PurePosixPath(note.slug).name, []).append(note)
    return grouped


def relative_note_url(from_slug: str, to_slug: str) -> str:
    source = SITE_DIR / Path(*from_slug.split("/"))
    target = SITE_DIR / "index.html" if not to_slug else SITE_DIR / Path(*to_slug.split("/")) / "index.html"
    return Path(os.path.relpath(target, start=source)).as_posix()


def resolve_note_ref(ref: str, current: Note, by_slug: dict[str, Note], by_name: dict[str, list[Note]]) -> Note:
    try:
        normalized = normalize_note_path(ref)
    except SystemExit as exc:
        raise SystemExit(f"Invalid note link in {current.slug}: {ref}") from exc
    if normalized in by_slug:
        return by_slug[normalized]
    basename_matches = by_name.get(normalized, [])
    if len(basename_matches) == 1:
        return basename_matches[0]
    if len(basename_matches) > 1:
        matches = ", ".join(note.slug for note in basename_matches)
        raise SystemExit(f"Ambiguous note link in {current.slug}: {ref} matches {matches}")
    raise SystemExit(f"Unknown note link in {current.slug}: {ref}")


def try_resolve_note_ref(ref: str, current: Note, by_slug: dict[str, Note], by_name: dict[str, list[Note]]) -> Note | None:
    try:
        return resolve_note_ref(ref, current, by_slug, by_name)
    except SystemExit:
        return None


def resolve_wiki_link_target(raw: str, current: Note, by_slug: dict[str, Note], by_name: dict[str, list[Note]]) -> Note | None:
    if "|" in raw:
        left, right = [part.strip() for part in raw.split("|", 1)]
        left_note = try_resolve_note_ref(left, current, by_slug, by_name)
        right_note = try_resolve_note_ref(right, current, by_slug, by_name)
        if right_note is not None:
            return right_note
        if left_note is not None:
            return left_note
        return None
    return try_resolve_note_ref(raw.strip(), current, by_slug, by_name)


def wiki_link_graph_data(notes: list[Note]) -> dict[str, list[dict[str, str]]]:
    by_slug = notes_by_slug(notes)
    by_name = notes_by_basename(notes)
    nodes = [
        {
            "id": note.slug,
            "title": note.title,
            "group": note.slug.split("/", 1)[0] if "/" in note.slug else "unmarked",
        }
        for note in sorted(notes, key=lambda item: item.slug)
    ]
    edges: set[tuple[str, str]] = set()
    for note in notes:
        text = (note.path / "index.qmd").read_text(encoding="utf-8")
        for match in re.finditer(r"\[\[([^\]\n]+)\]\]", text):
            target = resolve_wiki_link_target(match.group(1).strip(), note, by_slug, by_name)
            if target is None:
                continue
            edges.add((note.slug, target.slug))
    links = [{"source": source, "target": target} for source, target in sorted(edges)]
    return {"nodes": nodes, "links": links}


def wiki_link_replacer(current: Note, notes: list[Note]):
    by_slug = notes_by_slug(notes)
    by_name = notes_by_basename(notes)

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        if "|" in raw:
            left, right = [part.strip() for part in raw.split("|", 1)]
            right_note = try_resolve_note_ref(right, current, by_slug, by_name)
            left_note = try_resolve_note_ref(left, current, by_slug, by_name)
            if right_note is not None:
                label, target = left, right_note
            elif left_note is not None:
                label, target = right, left_note
            else:
                target = resolve_note_ref(right, current, by_slug, by_name)
                label = left
        else:
            target = resolve_note_ref(raw, current, by_slug, by_name)
            label = target.title
        return f"[{label}]({relative_note_url(current.slug, target.slug)})"

    return replace


def preprocess_wiki_links(note: Note, notes: list[Note]) -> None:
    index = note.path / "index.qmd"
    original = index.read_text(encoding="utf-8")
    processed = re.sub(r"\[\[([^\]\n]+)\]\]", wiki_link_replacer(note, notes), original)
    if processed == original:
        render_note(note.path)
        return
    backup = index.with_suffix(".qmd.cabinet-backup")
    if backup.exists():
        backup.unlink()
    index.replace(backup)
    index.write_text(processed, encoding="utf-8")
    try:
        render_note(note.path)
    finally:
        index.unlink(missing_ok=True)
        backup.replace(index)


def discover_notes() -> list[Note]:
    notes: list[Note] = []
    for note_dir in note_dirs():
        title, author = metadata_from_qmd(note_dir)
        notes.append(
            Note(
                slug=note_dir.relative_to(RAW_DIR).as_posix(),
                path=note_dir,
                title=title,
                author=author,
                modified=(note_dir / "index.qmd").stat().st_mtime,
            )
        )
    return notes


def copy_template(note_path: str, title: str, author: str, force: bool = False) -> Path:
    target = RAW_DIR / Path(*note_path.split("/"))
    if target.exists() and not force:
        raise SystemExit(f"Note already exists: {target}")
    if not TEMPLATE_DIR.is_dir():
        raise SystemExit(f"Missing template directory: {TEMPLATE_DIR}")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(TEMPLATE_DIR, target)
    index = target / "index.qmd"
    text = index.read_text(encoding="utf-8")
    text = text.replace("{{ title }}", title).replace("{{ author }}", author)
    index.write_text(text, encoding="utf-8")
    return target


def render_note(note_dir: Path) -> None:
    print(f"Rendering {note_dir.relative_to(ROOT)}", flush=True)
    run(["quarto", "render", "."], cwd=note_dir)


def note_source_files(note: Note) -> list[Path]:
    files: list[Path] = []
    for path in note.path.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(note.path)
        if any(part in GENERATED_DIRS for part in relative.parts):
            continue
        if path.name == ".DS_Store" or path.name.endswith(".cabinet-backup"):
            continue
        files.append(path)
    return sorted(files)


def note_hash(note: Note) -> str:
    digest = hashlib.sha256()
    digest.update(b"cabinet-cache-v2\n")
    for path in note_source_files(note):
        relative = path.relative_to(note.path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_cache() -> dict[str, str]:
    if not CACHE_FILE.is_file():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    notes = data.get("notes", {})
    return notes if isinstance(notes, dict) else {}


def save_cache(cache: dict[str, str]) -> None:
    SITE_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"notes": cache}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def rendered_output_exists(note: Note) -> bool:
    return (note.path / NOTE_OUTPUT_DIR / "index.html").is_file()


def copy_output(note: Note) -> None:
    source = note.path / NOTE_OUTPUT_DIR
    if not source.is_dir():
        raise SystemExit(f"Missing rendered output for {note.slug}: {source}")
    destination = SITE_DIR / Path(*note.slug.split("/"))
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def tree_from_notes(notes: list[Note]) -> dict:
    root: dict = {"children": {}, "note": None}
    for note in sorted(notes, key=lambda item: item.slug):
        node = root
        for part in note.slug.split("/"):
            node = node["children"].setdefault(part, {"children": {}, "note": None})
        node["note"] = note
    return root


def folder_label(slug: str) -> str:
    if not slug:
        return "Unmarked"
    return PurePosixPath(slug).name.replace("-", " ").title()


def note_groups(notes: list[Note]) -> dict[str, list[Note]]:
    groups: dict[str, list[Note]] = {}
    for note in notes:
        groups.setdefault(note.parent_slug, []).append(note)
    return {folder: sorted(items, key=lambda note: (note.title.lower(), note.slug)) for folder, items in groups.items()}


def child_folders(notes: list[Note]) -> dict[str, list[str]]:
    children: dict[str, set[str]] = {}
    for note in notes:
        parts = note.slug.split("/")
        for index in range(len(parts) - 1):
            parent = "/".join(parts[:index])
            child = "/".join(parts[: index + 1])
            children.setdefault(parent, set()).add(child)
    return {folder: sorted(items) for folder, items in children.items()}


def render_home_note(note: Note) -> str:
    search = html.escape((note.title + " " + note.slug + " " + note.author).lower())
    return f"""<li class="home-note" data-search="{search}">
  <a href="{html.escape(note.slug)}/index.html">
    <span>{html.escape(note.title)}</span>
    <small>{html.escape(note.slug)}</small>
  </a>
</li>"""


def render_home_sections(notes: list[Note]) -> str:
    groups = note_groups(notes)
    children = child_folders(notes)

    def render_folder(folder: str, level: int) -> str:
        heading_level = min(level + 2, 6)
        pieces: list[str] = []
        folder_notes = groups.get(folder, [])
        child_html = "".join(render_folder(child, level + 1) for child in children.get(folder, []))
        if folder or folder_notes:
            note_html = ""
            if folder_notes:
                note_html = f"""  <ul class="home-notes">{"".join(render_home_note(note) for note in folder_notes)}</ul>"""
            pieces.append(
                f"""<section class="home-folder depth-{level}" data-folder="{html.escape(folder or "unmarked")}">
  <h{heading_level}>{html.escape(folder_label(folder))}</h{heading_level}>
{note_html}
{child_html}
</section>"""
            )
        else:
            pieces.append(child_html)
        return "".join(pieces)

    rendered = render_folder("", 0)
    if not rendered:
        return '<p class="empty">No notes have been created yet.</p>'
    return rendered


def render_tree_nodes(
    node: dict,
    current_slug: str | None = None,
    base_slug: str | None = None,
    search_attrs: bool = False,
) -> str:
    pieces: list[str] = []
    for name, child in sorted(node["children"].items()):
        note = child["note"]
        descendants = render_tree_nodes(child, current_slug, base_slug, search_attrs)
        if note:
            if base_slug is None:
                href = f"{html.escape(note.slug)}/index.html"
            else:
                href = html.escape(relative_note_url(base_slug, note.slug))
            active = " active" if note.slug == current_slug else ""
            attrs = ""
            if search_attrs:
                attrs = f' data-search="{html.escape((note.title + " " + note.slug + " " + note.author).lower())}"'
            pieces.append(
                f"""<li class="tree-note{active}"{attrs}><a href="{href}"><span>{html.escape(note.title)}</span><small>{html.escape(note.slug)}</small></a>{descendants}</li>"""
            )
        else:
            pieces.append(f"""<li class="tree-folder"><span>{html.escape(name)}</span>{descendants}</li>""")
    if not pieces:
        return ""
    return "<ul>" + "".join(pieces) + "</ul>"


def note_sidebar_html(note: Note, notes: list[Note]) -> str:
    parent = note.parent_slug
    sibling_notes = sorted(
        [item for item in notes if item.parent_slug == parent],
        key=lambda item: (item.title.lower(), item.slug),
    )
    if not sibling_notes:
        sibling_notes = [note]
    items = "\n".join(
        f"""  <li><a href="{html.escape(relative_note_url(note.slug, item.slug))}" class="nav-link{" active" if item.slug == note.slug else ""}">{html.escape(item.title)}</a></li>"""
        for item in sibling_notes
    )
    title = html.escape(folder_label(parent))
    return f"""<div id="cabinet-sidebar-notes" class="sidebar toc-left cabinet-sidebar">
  <nav id="cabinet-notes" role="navigation" aria-label="Cabinet notes" class="toc-active">
    <h2 id="cabinet-notes-title">{title}</h2>
    <ul>
{items}
    </ul>
  </nav>
</div>"""


def note_panel_html(note: Note, notes: list[Note]) -> str:
    parent = note.parent_slug
    sibling_notes = sorted(
        [item for item in notes if item.parent_slug == parent],
        key=lambda item: (item.title.lower(), item.slug),
    )
    items = "\n".join(
        f"""  <li><a href="{html.escape(relative_note_url(note.slug, item.slug))}" class="nav-link{" active" if item.slug == note.slug else ""}">{html.escape(item.title)}</a></li>"""
        for item in sibling_notes
    )
    return f"""<div class="cabinet-notes-panel" id="cabinet-notes-panel" hidden>
  <nav role="navigation" aria-label="Cabinet notes" class="toc-active">
    <h2>{html.escape(folder_label(parent))}</h2>
    <ul>
{items}
    </ul>
  </nav>
</div>"""


def inject_note_navigation(note: Note, notes: list[Note]) -> None:
    page = SITE_DIR / Path(*note.slug.split("/")) / "index.html"
    if not page.is_file():
        return
    text = page.read_text(encoding="utf-8")
    css = """
<style id="cabinet-navigation-style">
  .cabinet-floating-controls {
    position: fixed;
    top: 14px;
    right: 18px;
    display: inline-flex;
    gap: 6px;
    z-index: 1000;
    background: rgba(255, 250, 240, 0.9);
    border: 1px solid rgba(37, 29, 22, 0.18);
    border-radius: 999px;
    padding: 4px;
    font-family: "ETBembo", "Palatino Linotype", Palatino, Georgia, serif;
    transition: transform 160ms ease, opacity 160ms ease;
  }
  .cabinet-floating-controls.is-hidden {
    transform: translateY(-130%);
    opacity: 0;
  }
  .cabinet-home-floating,
  .cabinet-notes-toggle {
    color: #6d6258;
    background: rgba(255, 255, 255, 0.32);
    border: 1px solid rgba(37, 29, 22, 0.18);
    border-radius: 999px;
    padding: 4px 10px;
    text-decoration: none;
    font: inherit;
    font-size: 0.9rem;
    cursor: pointer;
  }
  .cabinet-home-floating:hover,
  .cabinet-notes-toggle:hover {
    color: Maroon;
    border-color: rgba(37, 29, 22, 0.32);
  }
  .cabinet-notes-toggle {
    margin-left: 0;
  }
  .cabinet-home-floating:focus,
  .cabinet-notes-toggle:focus {
    outline: 0;
    border-color: rgba(37, 29, 22, 0.45);
  }
  #cabinet-sidebar-notes {
    grid-column: screen-start / body-start;
    align-self: start;
    margin-top: 0;
    margin-left: 0.75rem;
    width: 6.75rem;
  }
  #quarto-sidebar-toc-left {
    margin-left: 9.25rem;
  }
  #cabinet-sidebar-notes nav {
    padding-top: 0;
  }
  #cabinet-sidebar-notes h2,
  .cabinet-notes-panel h2 {
    font-size: .875rem;
    margin: 0 0 0.6rem;
  }
  #cabinet-sidebar-notes ul,
  .cabinet-notes-panel ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  #cabinet-sidebar-notes ul > li > a,
  .cabinet-notes-panel ul > li > a {
    border-left: 1px solid #e9ecef;
    padding-left: .6rem;
    font-size: .875rem;
    display: block;
  }
  #cabinet-sidebar-notes ul > li > a.active,
  .cabinet-notes-panel ul > li > a.active {
    border-left: 1px solid maroon;
    color: maroon !important;
  }
  .cabinet-notes-panel {
    position: fixed;
    top: 52px;
    right: 16px;
    width: min(18rem, calc(100vw - 32px));
    max-height: calc(100vh - 76px);
    overflow: auto;
    z-index: 1001;
    padding: 1rem;
    background: FloralWhite;
    box-shadow: 0 8px 28px rgba(37, 29, 22, 0.18);
  }
  @media (min-width: 1200px) {
    .cabinet-notes-toggle,
    .cabinet-notes-panel {
      display: none;
    }
  }
  @media (max-width: 1199px) {
    #cabinet-sidebar-notes {
      display: none;
    }
    #quarto-sidebar-toc-left {
      margin-left: 0;
    }
  }
</style>
"""
    home_href = html.escape(relative_note_url(note.slug, ""))
    floating_home = f"""<div class="cabinet-floating-controls" id="cabinet-floating-controls">
  <a class="cabinet-home-floating" href="{home_href}">Home</a>
  <button class="cabinet-notes-toggle" type="button" aria-controls="cabinet-notes-panel" aria-expanded="false">Notes</button>
</div>"""
    sidebar = note_sidebar_html(note, notes)
    panel = note_panel_html(note, notes)
    script = """
<script id="cabinet-navigation-script">
  (() => {
    const controls = document.getElementById('cabinet-floating-controls');
    const toggle = document.querySelector('.cabinet-notes-toggle');
    const panel = document.getElementById('cabinet-notes-panel');
    let lastY = window.scrollY;
    window.addEventListener('scroll', () => {
      const currentY = window.scrollY;
      controls?.classList.toggle('is-hidden', currentY > lastY && currentY > 80);
      lastY = currentY;
    }, { passive: true });
    toggle?.addEventListener('click', () => {
      const isOpen = !panel?.hasAttribute('hidden');
      if (isOpen) {
        panel?.setAttribute('hidden', '');
        toggle.setAttribute('aria-expanded', 'false');
      } else {
        panel?.removeAttribute('hidden');
        toggle.setAttribute('aria-expanded', 'true');
      }
    });
  })();
</script>
"""
    had_sidebar = '<nav class="cabinet-sidebar"' in text
    if "cabinet-navigation-style" not in text:
        text = text.replace("</head>", css + "\n</head>", 1)
    if not had_sidebar:
        body_end = text.find(">", text.find("<body"))
        if body_end != -1:
            text = text[: body_end + 1] + "\n" + floating_home + "\n" + panel + text[body_end + 1 :]
        toc = '<div id="quarto-sidebar-toc-left"'
        if toc in text:
            text = text.replace(toc, sidebar + "\n" + toc, 1)
        elif body_end != -1:
            text = text[: body_end + 1] + "\n" + sidebar + text[body_end + 1 :]
    if "cabinet-navigation-script" not in text:
        text = text.replace("</body>", script + "\n</body>", 1)
    page.write_text(text, encoding="utf-8")


def generate_index(notes: list[Note]) -> None:
    SITE_DIR.mkdir(exist_ok=True)
    def display_date(timestamp: float) -> str:
        value = datetime.fromtimestamp(timestamp)
        return f"{value.strftime('%b')} {value.day}, {value.year}"

    tree_html = render_home_sections(notes)
    graph_data = json.dumps(wiki_link_graph_data(notes), ensure_ascii=False)
    latest = "\n".join(
        f"""        <li><a href="{html.escape(note.slug)}/index.html">{html.escape(note.title)}</a><span>{display_date(note.modified)}</span></li>"""
        for note in sorted(notes, key=lambda item: item.modified, reverse=True)[:5]
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Typst Cabinet</title>
  <style>
    :root {{
      color-scheme: light;
      --paper: FloralWhite;
      --ink: #251d16;
      --muted: #6d6258;
      --accent: Maroon;
      --rule: rgba(37, 29, 22, 0.18);
    }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: "ETBembo", "Palatino Linotype", Palatino, Georgia, serif;
      line-height: 1.5;
    }}
    main {{
      width: min(920px, calc(100vw - 40px));
      margin: 0 auto;
      padding: 64px 0;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: clamp(2rem, 5vw, 4rem);
      font-weight: 400;
      letter-spacing: 0;
    }}
    .lede {{
      max-width: 54ch;
      margin: 0 0 40px;
      color: var(--muted);
      font-size: 1.1rem;
    }}
    .search {{
      width: 100%;
      box-sizing: border-box;
      margin: 0 0 28px;
      padding: 10px 12px;
      border: 1px solid var(--rule);
      background: rgba(255, 255, 255, 0.34);
      color: var(--ink);
      font: inherit;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 240px;
      gap: 48px;
      align-items: start;
    }}
    .note-tree {{
      padding: 0;
    }}
    .home-folder {{
      margin: 0 0 20px;
    }}
    .home-folder h2,
    .home-folder h3,
    .home-folder h4,
    .home-folder h5,
    .home-folder h6 {{
      margin: 0 0 8px;
      text-align: left;
      color: var(--ink);
      font-weight: 400;
      line-height: 1.1;
      letter-spacing: 0;
    }}
    .home-folder h2 {{
      font-size: 1.45rem;
    }}
    .home-folder h3 {{
      font-size: 1.2rem;
      margin-left: 0;
    }}
    .home-folder h4,
    .home-folder h5,
    .home-folder h6 {{
      font-size: 1.05rem;
      margin-left: 0;
    }}
    .home-notes {{
      margin: 0 0 14px;
      padding: 0;
      list-style: none;
    }}
    .home-note > a {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      padding: 6px 0;
      color: inherit;
      text-decoration: none;
    }}
    .home-note > a:hover span:first-child {{
      color: var(--accent);
    }}
    .home-note span {{
      font-size: 1.02rem;
    }}
    .home-note small {{
      align-self: center;
      color: var(--muted);
      font-size: 0.82rem;
      white-space: nowrap;
    }}
    .home-load-more {{
      margin: 0 0 14px;
      padding: 4px 10px;
      border: 1px solid var(--rule);
      background: rgba(255, 255, 255, 0.35);
      color: var(--ink);
      font: inherit;
      font-size: 0.88rem;
      cursor: pointer;
    }}
    .home-load-more:hover {{
      color: var(--accent);
    }}
    .latest {{
      padding-left: 22px;
    }}
    .latest h2 {{
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 0.95rem;
      font-weight: 400;
    }}
    .latest ul {{
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .latest li {{
      display: grid;
      gap: 2px;
      margin-bottom: 14px;
    }}
    .latest a {{
      color: var(--ink);
      text-decoration: none;
    }}
    .latest a:hover {{
      color: var(--accent);
    }}
    .latest span {{
      color: var(--muted);
      font-size: 0.88rem;
    }}
    .empty {{
      padding: 18px 0;
      color: var(--muted);
      border-bottom: 1px solid var(--rule);
    }}
    .graph-section {{
      margin-top: 0;
      border-top: 0;
      padding-top: 12px;
    }}
    .graph-section h2 {{
      margin: 0 0 8px;
      color: var(--ink);
      font-size: 1.35rem;
      font-weight: 400;
    }}
    .graph-section p {{
      margin: 0 0 16px;
      color: var(--muted);
      font-size: 0.95rem;
    }}
    .graph-layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 220px;
      gap: 14px;
      align-items: start;
    }}
    .graph-canvas {{
      width: 100%;
      height: 420px;
      border: 1px solid var(--rule);
      background: var(--paper);
      display: block;
      cursor: grab;
    }}
    .graph-canvas:active {{
      cursor: grabbing;
    }}
    .graph-controls {{
      display: grid;
      gap: 10px;
    }}
    .graph-controls label {{
      display: grid;
      gap: 4px;
      font-size: 0.86rem;
      color: var(--muted);
    }}
    .graph-controls .value {{
      color: var(--ink);
      font-size: 0.84rem;
    }}
    .graph-controls input {{
      width: 100%;
    }}
    @media (max-width: 640px) {{
      main {{
        width: min(100vw - 28px, 920px);
        padding: 36px 0;
      }}
      .layout {{
        grid-template-columns: 1fr;
        gap: 30px;
      }}
      .latest {{
        border-left: 0;
        padding-left: 0;
      }}
      .note-tree {{
        padding: 0;
      }}
      .home-note > a {{
        grid-template-columns: 1fr;
        gap: 4px;
        padding: 4px 0;
      }}
      .home-note small {{
        white-space: normal;
      }}
      .graph-layout {{
        grid-template-columns: 1fr;
      }}
      .graph-canvas {{
        height: 320px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Typst Cabinet</h1>
    <p class="lede">A local cabinet of Quarto-Typst notes rendered with the same Tufte-inspired style as the source handouts.</p>
    <input class="search" id="note-search" type="search" placeholder="Search notes" aria-label="Search notes">
    <div class="layout">
      <nav class="note-tree" aria-label="Notes">
        {tree_html}
      </nav>
      <aside class="latest" aria-label="Recently edited notes">
        <h2>Recent</h2>
        <ul>
{latest}
        </ul>
      </aside>
    </div>
    <section class="graph-section" aria-label="Wiki link graph">
      <h2>Wiki Link Graph</h2>
      <p>Explore how notes connect through wiki links. Tune forces to change the layout.</p>
      <div class="graph-layout">
        <canvas id="wiki-graph" class="graph-canvas" aria-label="Wiki link graph canvas"></canvas>
        <form class="graph-controls" id="graph-controls">
          <label>Center force <span class="value" id="center-force-value"></span><input id="center-force" type="range" min="0" max="1" step="0.01" value="0.23"></label>
          <label>Repel force <span class="value" id="repel-force-value"></span><input id="repel-force" type="range" min="0" max="1" step="0.01" value="0.29"></label>
          <label>Link force <span class="value" id="link-force-value"></span><input id="link-force" type="range" min="0" max="1" step="0.01" value="0.14"></label>
          <label>Link distance <span class="value" id="link-distance-value"></span><input id="link-distance" type="range" min="0" max="1" step="0.01" value="0.42"></label>
        </form>
      </div>
    </section>
  </main>
  <script>
    const search = document.getElementById('note-search');
    const noteLists = Array.from(document.querySelectorAll('.home-notes'));
    const visibleLimits = new Set();
    const INITIAL_NOTES_PER_FOLDER = 100;
    noteLists.forEach((list, index) => {{
      list.dataset.listId = String(index);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'home-load-more';
      button.hidden = true;
      button.dataset.forList = String(index);
      button.addEventListener('click', () => {{
        visibleLimits.add(String(index));
        applyHomeFilters();
      }});
      list.insertAdjacentElement('afterend', button);
    }});
    const applyHomeFilters = () => {{
      const value = search.value.trim().toLowerCase();
      const searching = Boolean(value);
      noteLists.forEach((list) => {{
        const listId = list.dataset.listId;
        const expanded = visibleLimits.has(listId);
        const items = Array.from(list.querySelectorAll(':scope > .home-note'));
        const nextElement = list.nextElementSibling;
        const loadMoreButton = nextElement?.classList.contains('home-load-more') ? nextElement : null;
        let visibleCount = 0;
        let hiddenByLimit = 0;
        items.forEach((note) => {{
          const matches = !value || note.dataset.search.includes(value);
          if (!matches) {{
            note.hidden = true;
            return;
          }}
          if (!searching && !expanded && visibleCount >= INITIAL_NOTES_PER_FOLDER) {{
            note.hidden = true;
            hiddenByLimit += 1;
            return;
          }}
          note.hidden = false;
          visibleCount += 1;
        }});
        if (loadMoreButton) {{
          loadMoreButton.hidden = searching || expanded || hiddenByLimit === 0;
          loadMoreButton.textContent = `+${{hiddenByLimit}}`;
        }}
      }});
      document.querySelectorAll('.home-folder').forEach((folder) => {{
        const visibleNote = folder.querySelector('.home-note:not([hidden])');
        const visibleSubfolder = folder.querySelector('.home-folder:not([hidden])');
        folder.hidden = !visibleNote && !visibleSubfolder;
      }});
    }};
    search.addEventListener('input', applyHomeFilters);
    applyHomeFilters();
    const graphData = {graph_data};
    const canvas = document.getElementById('wiki-graph');
    const controls = {{
      centerForce: document.getElementById('center-force'),
      repelForce: document.getElementById('repel-force'),
      linkForce: document.getElementById('link-force'),
      linkDistance: document.getElementById('link-distance'),
      centerForceValue: document.getElementById('center-force-value'),
      repelForceValue: document.getElementById('repel-force-value'),
      linkForceValue: document.getElementById('link-force-value'),
      linkDistanceValue: document.getElementById('link-distance-value')
    }};
    if (canvas && graphData.nodes.length) {{
      const ctx = canvas.getContext('2d');
      const dpr = Math.max(1, window.devicePixelRatio || 1);
      const byId = new Map();
      const palette = ['#e7d4f5', '#d9e4ff', '#d7f2df', '#ffe4cf', '#f6d4dd', '#f9efc7'];
      const hashColor = (value) => {{
        let hash = 0;
        for (let i = 0; i < value.length; i += 1) hash = (hash * 31 + value.charCodeAt(i)) | 0;
        return palette[Math.abs(hash) % palette.length];
      }};
      const nodes = graphData.nodes.map((node, index) => {{
        const item = {{
          ...node,
          x: 40 + (index * 17 % 420),
          y: 40 + (index * 29 % 320),
          vx: 0,
          vy: 0,
          radius: 7,
          color: hashColor(node.group),
          fixed: false
        }};
        byId.set(node.id, item);
        return item;
      }});
      const links = graphData.links
        .map((link) => ({{ source: byId.get(link.source), target: byId.get(link.target) }}))
        .filter((link) => link.source && link.target);
      let width = 0;
      let height = 0;
      let hoverNode = null;
      let dragNode = null;
      let suppressClick = false;
      let dragStart = null;
      const DRAG_CLICK_THRESHOLD_PX = 2;
      const scales = {{
        centerForce: {{ min: 0, max: 0.08 }},
        repelForce: {{ min: 50, max: 3000 }},
        linkForce: {{ min: 0.01, max: 0.5 }},
        linkDistance: {{ min: 20, max: 260 }}
      }};
      const scaledParamValue = (key, value) => {{
        const range = scales[key];
        return range.min + (range.max - range.min) * value;
      }};
      const params = {{
        centerForce: scaledParamValue('centerForce', Number(controls.centerForce.value)),
        repelForce: scaledParamValue('repelForce', Number(controls.repelForce.value)),
        linkForce: scaledParamValue('linkForce', Number(controls.linkForce.value)),
        linkDistance: scaledParamValue('linkDistance', Number(controls.linkDistance.value)),
        damping: 0.86
      }};
      const updateControlLabels = () => {{
        controls.centerForceValue.textContent = Number(controls.centerForce.value).toFixed(2);
        controls.repelForceValue.textContent = Number(controls.repelForce.value).toFixed(2);
        controls.linkForceValue.textContent = Number(controls.linkForce.value).toFixed(2);
        controls.linkDistanceValue.textContent = Number(controls.linkDistance.value).toFixed(2);
      }};
      const resize = () => {{
        const rect = canvas.getBoundingClientRect();
        width = rect.width;
        height = rect.height;
        canvas.width = Math.floor(width * dpr);
        canvas.height = Math.floor(height * dpr);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      }};
      const findNode = (x, y) => {{
        let best = null;
        let bestDist = Infinity;
        for (const node of nodes) {{
          const dx = node.x - x;
          const dy = node.y - y;
          const dist = Math.hypot(dx, dy);
          if (dist < node.radius + 4 && dist < bestDist) {{
            best = node;
            bestDist = dist;
          }}
        }}
        return best;
      }};
      const applyForces = () => {{
        const centerX = width / 2;
        const centerY = height / 2;
        for (let i = 0; i < nodes.length; i += 1) {{
          const a = nodes[i];
          for (let j = i + 1; j < nodes.length; j += 1) {{
            const b = nodes[j];
            let dx = b.x - a.x;
            let dy = b.y - a.y;
            const distSq = Math.max(25, dx * dx + dy * dy);
            const dist = Math.sqrt(distSq);
            dx /= dist;
            dy /= dist;
            const force = params.repelForce / distSq;
            const fx = force * dx;
            const fy = force * dy;
            if (!a.fixed) {{
              a.vx -= fx;
              a.vy -= fy;
            }}
            if (!b.fixed) {{
              b.vx += fx;
              b.vy += fy;
            }}
          }}
        }}
        for (const link of links) {{
          const dx = link.target.x - link.source.x;
          const dy = link.target.y - link.source.y;
          const dist = Math.max(1, Math.hypot(dx, dy));
          const stretch = dist - params.linkDistance;
          const force = params.linkForce * stretch;
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          if (!link.source.fixed) {{
            link.source.vx += fx;
            link.source.vy += fy;
          }}
          if (!link.target.fixed) {{
            link.target.vx -= fx;
            link.target.vy -= fy;
          }}
        }}
        for (const node of nodes) {{
          if (node.fixed) continue;
          node.vx += (centerX - node.x) * params.centerForce;
          node.vy += (centerY - node.y) * params.centerForce;
          node.vx *= params.damping;
          node.vy *= params.damping;
          node.x += node.vx;
          node.y += node.vy;
          node.x = Math.min(width - 10, Math.max(10, node.x));
          node.y = Math.min(height - 10, Math.max(10, node.y));
        }}
      }};
      const draw = () => {{
        ctx.clearRect(0, 0, width, height);
        ctx.lineWidth = 1;
        ctx.strokeStyle = 'rgba(37, 29, 22, 0.25)';
        for (const link of links) {{
          ctx.beginPath();
          ctx.moveTo(link.source.x, link.source.y);
          ctx.lineTo(link.target.x, link.target.y);
          ctx.stroke();
        }}
        ctx.font = '12px "ETBembo", "Palatino Linotype", Palatino, Georgia, serif';
        ctx.textBaseline = 'middle';
        for (const node of nodes) {{
          ctx.beginPath();
          ctx.fillStyle = node.color;
          ctx.arc(node.x, node.y, node.radius + (node === hoverNode ? 1.5 : 0), 0, Math.PI * 2);
          ctx.fill();
          ctx.strokeStyle = 'rgba(37, 29, 22, 0.35)';
          ctx.stroke();
          ctx.fillStyle = '#251d16';
          ctx.fillText(node.title, node.x + node.radius + 4, node.y);
        }}
      }};
      const tick = () => {{
        applyForces();
        draw();
        window.requestAnimationFrame(tick);
      }};
      const pointer = (event) => {{
        const rect = canvas.getBoundingClientRect();
        return {{ x: event.clientX - rect.left, y: event.clientY - rect.top }};
      }};
      const distanceBetween = (from, to) => Math.hypot(to.x - from.x, to.y - from.y);
      canvas.addEventListener('mousemove', (event) => {{
        const pos = pointer(event);
        hoverNode = findNode(pos.x, pos.y);
        if (dragNode) {{
          if (dragStart && distanceBetween(dragStart, pos) > DRAG_CLICK_THRESHOLD_PX) suppressClick = true;
          dragNode.x = pos.x;
          dragNode.y = pos.y;
          dragNode.vx = 0;
          dragNode.vy = 0;
        }}
      }});
      canvas.addEventListener('mousedown', (event) => {{
        const pos = pointer(event);
        suppressClick = false;
        dragStart = pos;
        dragNode = findNode(pos.x, pos.y);
        if (dragNode) {{
          dragNode.fixed = true;
          dragNode.x = pos.x;
          dragNode.y = pos.y;
        }}
      }});
      window.addEventListener('mouseup', () => {{
        if (dragNode) dragNode.fixed = false;
        dragNode = null;
        dragStart = null;
      }});
      canvas.addEventListener('click', (event) => {{
        if (suppressClick) {{
          suppressClick = false;
          return;
        }}
        const pos = pointer(event);
        const node = findNode(pos.x, pos.y);
        if (node) window.location.href = `${{node.id}}/index.html`;
      }});
      for (const [key, input] of Object.entries({{
        centerForce: controls.centerForce,
        repelForce: controls.repelForce,
        linkForce: controls.linkForce,
        linkDistance: controls.linkDistance
      }})) {{
        input.addEventListener('input', () => {{
          params[key] = scaledParamValue(key, Number(input.value));
          updateControlLabels();
        }});
      }}
      updateControlLabels();
      resize();
      window.addEventListener('resize', resize);
      tick();
    }}
  </script>
</body>
</html>
"""
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")


def build(slugs: list[str] | None = None, skip_render: bool = False) -> None:
    selected = {normalize_note_path(slug) for slug in slugs or []}
    notes = discover_notes()
    missing = selected.difference(note.slug for note in notes)
    if missing:
        raise SystemExit(f"Unknown note(s): {', '.join(sorted(missing))}")
    build_notes = [note for note in notes if not selected or note.slug in selected]
    SITE_DIR.mkdir(exist_ok=True)
    cache = load_cache()
    next_cache = dict(cache)
    for note in build_notes:
        current_hash = note_hash(note)
        needs_render = not skip_render and (cache.get(note.slug) != current_hash or not rendered_output_exists(note))
        if needs_render:
            preprocess_wiki_links(note, notes)
            next_cache[note.slug] = current_hash
        elif skip_render:
            print(f"Skipping render for {note.slug} (--skip-render)", flush=True)
        else:
            print(f"Skipping unchanged note {note.slug}", flush=True)
        copy_output(note)
        inject_note_navigation(note, notes)
    valid_slugs = {note.slug for note in notes}
    next_cache = {slug: value for slug, value in next_cache.items() if slug in valid_slugs}
    save_cache(next_cache)
    generate_index(notes)
    print(f"Site written to {SITE_DIR}")


def serve(port: int, host: str) -> None:
    if not (SITE_DIR / "index.html").is_file():
        build()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving {SITE_DIR} at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create, build, and serve a local Quarto note cabinet.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_parser = subparsers.add_parser("new", help="create a new note from template/")
    new_parser.add_argument("slug", help="URL-friendly note folder name")
    new_parser.add_argument("--title", help="note title; defaults to a title-cased slug")
    new_parser.add_argument("--author", default="Saaleh Poovathumkadavil", help="author name")
    new_parser.add_argument("--force", action="store_true", help="replace an existing note with the same slug")

    build_parser = subparsers.add_parser("build", help="render notes and build site/")
    build_parser.add_argument("slugs", nargs="*", help="optional note slugs to render")
    build_parser.add_argument("--skip-render", action="store_true", help="rebuild site/ from existing rendered note output")

    serve_parser = subparsers.add_parser("serve", help="serve site/ locally")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--host", default="127.0.0.1")

    list_parser = subparsers.add_parser("list", help="list available raw notes")
    list_parser.set_defaults(command="list")

    args = parser.parse_args(argv)
    if args.command == "new":
        note_path = normalize_note_path(args.slug)
        title = args.title or PurePosixPath(note_path).name.replace("-", " ").title()
        target = copy_template(note_path, title, args.author, args.force)
        print(f"Created {target.relative_to(ROOT)}")
    elif args.command == "build":
        build(args.slugs, args.skip_render)
    elif args.command == "serve":
        serve(args.port, args.host)
    elif args.command == "list":
        for note in discover_notes():
            print(f"{note.slug}\t{note.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
