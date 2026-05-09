# AGENTS.md

Repo-local instructions for the `image-to-raw` project.

## 0. Scope

- This file applies only to `Q:\Projects\image-to-raw`.
- It supplements the global and workspace-level `AGENTS.md` files without replacing their safety and execution rules.
- Keep this instruction file in a single language. Do not duplicate every rule bilingually inside `AGENTS.md`.
- `AGENTS.zh-tw.md` is the Traditional Chinese original source manuscript for these repo-local instructions.
- `AGENTS.md` remains the English canonical baseline and the file most agents are expected to auto-load.
- When updating these instructions, edit `AGENTS.zh-tw.md` first, then update this English file to match.

## 1. Documentation Language And i18n

- For important human-facing project documentation, author the original source document in Traditional Chinese first.
- Treat `zh-TW` documents as the original source manuscripts for important project documentation.
- English documents may still be maintained as canonical baselines for public exchange, GitHub presentation, ecosystem integration, and external collaboration.
- When both Traditional Chinese and English versions exist, the English version should be derived from the Traditional Chinese source manuscript unless a user explicitly requests a different workflow.
- Do not force every document to have paired language files. Create or maintain paired files only when a translated/public version is needed, especially when a Traditional Chinese source already exists.
- Keep translated documentation in an i18n-aware layout instead of mixing full language versions in one file.

Recommended placement:

```text
README.zh-tw.md            # Traditional Chinese original source manuscript
README.md                  # English canonical exchange/public baseline
docs/i18n/zh-TW/<slug>.md  # Traditional Chinese original source manuscript
docs/i18n/en/<slug>.md     # English canonical translation
docs/i18n/<locale>/<slug>.md
```

- Root-level `README.md` may remain the English canonical exchange version for GitHub and external collaboration, but it should be translated from `README.zh-tw.md`.
- If an English-only document later becomes important project documentation, create the Traditional Chinese source manuscript before continuing substantial edits.
- If an existing document is already English and actively used externally, keep it stable while backfilling the Traditional Chinese source manuscript.
- If a Traditional Chinese source exists and an English/public counterpart is useful, keep the two files paired and update both intentionally.

## 2. AI Development Records

- Documents whose primary purpose is AI development process logging, review, confirmation, handoff, or discussion should be written in English by default.
- Examples include AI-generated status reports, implementation notes, review notes, handoff notes, and planning discussion artifacts.
- These AI development records do not require paired Traditional Chinese files unless they are promoted into user-facing or project-defining documentation.
- If an AI development record becomes user-facing or project-defining documentation, promote it into the i18n documentation structure described above.
