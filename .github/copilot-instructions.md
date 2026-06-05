# Copilot instructions for `cabinet`

## Project overview

This repository is a small Python CLI for managing a local Quarto note cabinet.

- Source notes live under the configured root directory as `<folder>/<note>/index.qmd`.
- `template/` is copied when creating new notes.
- `site/` is generated output.
- `cabinet.py` owns the full workflow: note discovery, wiki-link rewriting, Quarto rendering, home-page generation, and note navigation injection.

The cabinet is built from Quarto manuscript projects using the bundled Tufte-inspired format. `build` rewrites wiki links only for rendering, then restores the source files.

## Commands

Use Python 3.10+ and Quarto 1.5+ on `PATH`.
All commands accept `--root` and default to `.`.

```sh
python3 cabinet.py list
python3 cabinet.py new stats/week-01/lecture-01 --title "Lecture 01"
python3 cabinet.py build
python3 cabinet.py build finrev stats/week-01/lecture-01
python3 cabinet.py build --skip-render
python3 cabinet.py serve --port 8765
```

- `list` prints `slug<TAB>title` for discovered notes.
- `new` copies `template/` into the configured root and fills `{{ title }}` / `{{ author }}` in `index.qmd`.
- `build <slug ...>` renders only selected notes.
- `build --skip-render` rebuilds `site/` from existing note output.
- `serve` serves the generated `site/` directory locally.

There is no separate repo test or lint command defined; the practical validation path is `python3 cabinet.py build` or a targeted `python3 cabinet.py build <slug>`.

## High-level architecture

- `cabinet.py` is both CLI and renderer.
- Note metadata is extracted from the YAML front matter in each `index.qmd`.
- Incremental builds hash all source files in a note directory and cache results in `site/.cabinet-cache.json`.
- Quarto renders into each note’s `_manuscript/` directory, then the rendered output is copied into `site/<slug>/`.
- The homepage is generated HTML, not a Quarto page. It shows the note tree, recent notes, search, and a wiki-link graph.
- After rendering, the CLI injects extra note navigation and a floating Home/Notes control into each rendered note page.

## Conventions

- Treat note slugs as normalized paths: lowercase, hyphen-separated segments, no `index.qmd` or `.html` suffix.
- Wiki links in source use Obsidian style, including `[[label|slug]]`, `[[slug|label]]`, and bare unique basenames.
- Edit source notes in the configured root; do not hand-edit generated files in `site/` or `_manuscript/`.
- New notes should match the template Quarto structure and front matter in `template/index.qmd` and `template/_quarto.yml`.
- Generated directories and render artifacts are ignored by git: `site/`, `.quarto/`, `_freeze/`, `_manuscript/`, `index.html`, and `index-meca.zip`.
