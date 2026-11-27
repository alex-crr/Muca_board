# Manual Documentation Guide

Any `.md` file placed in this `docs/` directory (except this `README.md`) is automatically compiled into the GitHub Pages documentation site during tag/release builds alongside the auto-generated hardware outputs (iBOM, Priced BOM calculator, renders, schematic, reports, downloads).

## How to add a manual doc page

1. Create a Markdown file in `docs/`, for example `docs/bringup.md`.
2. Optionally add frontmatter at the top to control sidebar title and order:

```markdown
---
title: Bring-up & Flashing
order: 20
---

# Bring-up & Flashing

Write your notes here.
```

3. Place images in `docs/images/` (or `docs/assets/`) and reference them as `![caption](images/photo.jpg)`.
4. To keep a draft out of the published site, add `publish: false` to its frontmatter.

## Changelog in GitHub Releases

If you add a section matching your tag in `docs/CHANGELOG.md` (e.g. `## [v1.0.0]`), the release workflow automatically includes that exact section at the top of the GitHub Release notes.
