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
├── plugins/pdf2epub/
│   ├── .claude-plugin/plugin.json               # the single plugin this marketplace hosts
│   └── skills/pdf2epub/
│       ├── SKILL.md                             # skill definition Claude Code loads
│       └── scripts/                             # convert_pymupdf.py, common.py, prompts/
├── tests/                                       # offline unit tests for the pure pipeline stages
├── examples/                                    # sample PDF for local pipeline validation
└── tmp/                                         # checkpoints and logs (not versioned)
```

The plugin `version` lives only in `plugins/pdf2epub/.claude-plugin/plugin.json` (the marketplace entry deliberately has none, so it can't drift). The plugin description is duplicated in marketplace.json, plugin.json, and SKILL.md frontmatter — keep all three in sync when changing it.

Run tests offline (no API key needed): `uvx pytest tests/ -q`

## Local validation

`examples/alice-in-wonderland.pdf` (public-domain text via Project Gutenberg, compiled to PDF with pandoc) is a lightweight fixture for smoke-testing the pipeline end to end without touching real book PDFs:

```bash
uv run plugins/pdf2epub/skills/pdf2epub/scripts/convert_pymupdf.py \
  examples/alice-in-wonderland.pdf --max-chunks 3 --workdir ./tmp/pdf2epub-example
```

It's in English specifically to prove the language/metadata auto-detection generalizes beyond the original Bulgarian-only tool this plugin was built from.
