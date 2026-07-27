# pdf2epub-skills

A Claude Code plugin marketplace hosting one plugin, `pdf2epub` — see [README.md](README.md) for what it does and how to install it.

## Plugin maintenance (release rule)

The installed plugin is **cached per version** — editing this repo does not reach installed copies until a release is cut. After changing the skill, scripts, or prompts under `plugins/pdf2epub/`:

1. Bump `version` in `plugins/pdf2epub/.claude-plugin/plugin.json` (semver).
2. Commit and push to `idachev/pdf2epub-skills` on GitHub.
3. On each machine: `claude plugin marketplace update pdf2epub-skills`, then update/reinstall the plugin (`claude plugin install pdf2epub@pdf2epub-skills`).

## Layout

```
pdf2epub-skills/
├── CLAUDE.md                                    # this file
├── README.md                                    # what the plugin does, install + CLI usage
├── LICENSE                                      # MIT
├── .claude-plugin/marketplace.json              # this repo is a Claude Code plugin marketplace
├── .github/workflows/ci.yml                     # manifest validation + unit tests
├── docs/plans/                                  # design notes (e.g. BgGPT polish plan)
├── plugins/pdf2epub/
│   ├── .claude-plugin/plugin.json               # the single plugin this marketplace hosts
│   ├── skills/pdf2epub/                         # skill 1: PDF -> EPUB
│   │   ├── SKILL.md                             # skill definition Claude Code loads
│   │   └── scripts/                             # convert_pymupdf.py, common.py, figures.py, prompts/
│   └── skills/epub-translate/                   # skill 2: EPUB -> translated EPUB (+ optional BgGPT polish)
│       ├── SKILL.md                             # skill definition Claude Code loads
│       └── scripts/                             # translate_epub.py, polish_epub_bg.py, epubdoc.py, openai_client.py, prompts/
├── tests/                                       # offline unit tests for the pure pipeline stages
├── examples/                                    # sample PDF for local pipeline validation
└── tmp/                                         # checkpoints and logs (not versioned)
```

The plugin `version` lives only in `plugins/pdf2epub/.claude-plugin/plugin.json` (the marketplace entry deliberately has none, so it can't drift).

**Descriptions.** The *plugin* description is duplicated in `marketplace.json` and `plugin.json` — keep those two in sync. Each skill's own `SKILL.md` frontmatter description is separate and skill-specific; since the plugin now hosts two skills, the plugin description covers both and no longer matches any single skill's.

**Shared code.** `epub-translate` imports `common.py` from the `pdf2epub` skill via a relative path (`../pdf2epub/scripts`) rather than forking it, so the Gemini client, retry/backoff, and content-filter handling have one source of truth. Moving or renaming either skill directory breaks that import — `translate_epub.py` fails fast with a clear error if `common.py` isn't found. Anything added to `common.py` must stay backward-compatible for both callers (e.g. `generate()`'s `thinking_level` is optional and omits `thinking_config` entirely when unset). The optional BgGPT polish stage (`polish_epub_bg.py`) uses a separate OpenAI-compatible client in `openai_client.py` so the Gemini translate path never depends on the `openai` package; it only reuses `common.setup_output` / `common.atomic_write`.

**Glossaries are never committed here.** They encode terminology for one specific book or series and belong in that book's own working directory, passed via `--glossary PATH`. This repo is public; keep third-party series terminology and published-translation references out of it.

Run tests offline (no API key needed): `uvx --with pymupdf --with lxml pytest tests/ -q`  
(`openai` is not required for unit tests — the polish client is mocked / imported lazily.)

## Local validation

`examples/alice-in-wonderland.pdf` (public-domain text via Project Gutenberg, compiled to PDF with pandoc) is a lightweight fixture for smoke-testing the pipeline end to end without touching real book PDFs:

```bash
uv run plugins/pdf2epub/skills/pdf2epub/scripts/convert_pymupdf.py \
  examples/alice-in-wonderland.pdf --max-chunks 3 --workdir ./tmp/pdf2epub-example
```

It's in English specifically to prove the language/metadata auto-detection generalizes beyond the original Bulgarian-only tool this plugin was built from.
