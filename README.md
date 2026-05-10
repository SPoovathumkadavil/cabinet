
# Typst Cabinet

A local note cabinet where each note is a Quarto manuscript that renders to the same Tufte-inspired HTML style as the original `finrev` handout. Raw notes live in `raw/`, reusable note scaffolding lives in `template/`, and the compiled cabinet is written to `site/`.

## Requirements

- Python 3.10+
- Quarto 1.5+ available on `PATH`
- Any language runtimes used by your notes, such as R for `{r}` code chunks

## Layout

```text
.
├── cabinet.py          # CLI for creating, building, and serving notes
├── raw/                # source notes, organized as folder/folder/note
│   └── finrev/
├── template/           # copied by `cabinet.py new`
└── site/               # generated static website
```

Each note folder is a standalone Quarto project. Notes may be nested:

```text
raw/<folder>/<note>/
├── _quarto.yml
├── index.qmd
├── references.bib
├── Images/
└── _extensions/fredguth/tufte-inspired/
```

## Usage

List available notes:

```sh
python3 cabinet.py list
```

Create a new note from `template/`:

```sh
python3 cabinet.py new stats/week-01/lecture-01 --title "Lecture 01"
```

Edit the source at `raw/stats/week-01/lecture-01/index.qmd`.

Build the whole cabinet:

```sh
python3 cabinet.py build
```

Builds are incremental. `cabinet.py` hashes each note's source files and only
calls Quarto for notes that changed since the previous build. Unchanged notes
are still copied into `site/` and get refreshed navigation.

Build only selected notes:

```sh
python3 cabinet.py build finrev stats/week-01/lecture-01
```

Serve the generated cabinet locally:

```sh
python3 cabinet.py serve --port 8765
```

Then open `http://127.0.0.1:8765`.

## Linking notes

Use Obsidian-style wiki links inside `index.qmd` files:

```md
[[Final Review|finrev]]
[[Lecture 01|stats/week-01/lecture-01]]
```

The text before `|` is the visible label. The text after `|` is the note path under `raw/`, without `raw/` or `index.qmd`. Standard Obsidian order also works:

```md
[[stats/week-01/lecture-01|Lecture 01]]
```

If a note basename is unique, you can link it without the full folder path:

```md
[[lecture-01]]
```

During `python3 cabinet.py build`, `cabinet.py` temporarily preprocesses these links into normal Markdown links before calling Quarto. The source files keep the original wiki-link syntax.

## Navigation

- The home page displays notes as a folder tree and includes a search bar.
- The home page includes a wiki-link graph view with adjustable center, repel, link, and link-distance forces.
- Notes directly under `raw/` are grouped under `Unmarked`.
- Each rendered note gets a Home button.
- Each rendered note gets a compact Contents-style notes list for the current note folder. On smaller screens, the list moves into the floating Notes menu beside Home.

## Examples

The repository includes a few sample nested notes for testing:

- `raw/statistics/probability/bayes-review`
- `raw/statistics/probability/random-variables`
- `raw/statistics/regression/linear-models`
- `raw/computing/r/vector-basics`

These notes include abstracts and cross-links so the tree, note sidebars, and
wiki-link preprocessor can be checked against realistic content.

## Notes

- `template/` intentionally matches the `finrev` Quarto setup, including the bundled `fredguth/tufte-inspired` extension.
- `site/`, Quarto caches, and rendered manuscript output are generated and ignored by git.
- If a note contains executable chunks, Quarto controls execution and freezing through the note metadata.
