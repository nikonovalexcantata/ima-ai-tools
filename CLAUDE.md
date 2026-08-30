# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A Claude Code plugin marketplace plus its catalog page. There is no application code, no build, and no test suite. Each marketplace entry references a skill in someone else's public GitHub repository — no skill code is vendored here.

- `.claude-plugin/marketplace.json` — the marketplace manifest and single source of truth. One entry = one skill = one card on the catalog page.
- `docs/index.html` — the catalog page: one static file, inline CSS/JS, no framework, no build step. Cards render at runtime from the `SKILLS` JS constant, which mirrors the `plugins` array of `marketplace.json`. **After editing `marketplace.json`, mirror the change in `SKILLS`** (only `name`, `description`, `metadata` are used there).

## Commands

```
claude plugin validate .    # run after every marketplace.json edit
```

Validation does not catch load failures. Live-test any new or changed entry:

```
claude plugin marketplace add <absolute path to this checkout>
claude plugin install <name>@ima-ai-tools
claude plugin list                          # status must be "enabled", not "failed to load"
claude plugin details <name>@ima-ai-tools   # skill inventory must match the entry exactly
claude plugin uninstall <name>
claude plugin marketplace remove ima-ai-tools   # frees the name for the GitHub-sourced registration
```

A successful `install` is NOT proof the entry works — manifest conflicts surface only in `list`/`details`.

## Writing marketplace entries

Rules established by live testing (Claude Code 2.1.247):

1. **Single skill from a third-party repo**: use `source: {"source": "git-subdir", "url": "<repo>.git", "path": "<folder above the skill>"}` + `"strict": false` + `"skills": ["./<skill>"]`. Choose `path` so the resulting plugin root contains **neither** a `.claude-plugin/plugin.json` **nor** a `skills/` subdirectory. Otherwise:
   - upstream `plugin.json` + `strict: false` → "conflicting manifests", the plugin installs but fails to load;
   - a `skills/` directory at the plugin root → auto-discovery registers ALL skills in it and the `skills` filter is ignored.
2. **Whole upstream plugin** (has its own `.claude-plugin/plugin.json`): keep default strict mode, point `github` or `git-subdir` at the folder that holds `.claude-plugin/`, and add no component fields.
3. **`metadata`** is free-form: Claude Code ignores it, the catalog page reads it. Fields in use: `upstream` (link for the card title), `whenToUse[]` (bulleted situations — must answer "when to reach for it", never restate what the skill does), `badges[]` (`{kind, text}`, kind one of `mcp|skin|ext|attention`), `note` (small print under the install command — must list every post-install step: required CLIs, servers to run, session restart for MCP; a user must never need the upstream repo to get the plugin working), `featured` (ribbon).
4. No `ref` pinning — entries follow the upstream default branch so updates arrive on `claude plugin marketplace update`.

## Conventions

- Content is English; `README.md` follows ASD-STE100 Simplified Technical English (short sentences, active voice, one instruction per sentence). The one exception is `LICENSE` — verbatim MIT.
- The catalog page uses a fixed token system (oklch, Inter + IBM Plex Mono) with paired light and dark palettes. Every color goes through a `--token` defined on bare `:root`; never define one only inside the dark-theme blocks.
