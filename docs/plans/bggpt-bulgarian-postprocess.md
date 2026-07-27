# Plan: BgGPT post-processing for Bulgarian EPUB translations

Status: implemented (v0.8.0) · Scope: design + shipped sibling script  
Audience: operators of `epub-translate`  
Related code: `plugins/pdf2epub/skills/epub-translate/scripts/{polish_epub_bg.py,openai_client.py,translate_epub.py,epubdoc.py,prompts/polish_bg_chunk.md}`

**Shipped behavior notes (vs. original design):**

- Sibling script `polish_epub_bg.py` (Option B) with workdir + `--input-epub` modes.
- Prompt explicitly detects leftover English (Gemini refusals) and **retranslates** those units to Bulgarian, not only in-language polish.
- Default still opt-in (not wired into `translate_epub.py -t bg`).

## 1. Problem

The current `epub-translate` pipeline translates EN → Bulgarian with Gemini
(`gemini-3.6-flash` by default). Chunk inspection on full runs (e.g. *Hunters of
Dune*) shows the output is readable but frequently **not native literary
Bulgarian**. Typical residual defects after Gemini:

| Class | Examples seen in current chunks |
|---|---|
| Name / transliteration drift | `Хербърт` vs `Хърбърт`; `Кевин` → `Калин`; `Андерсън` vs `Андърсън` |
| Leftover source tokens | Mixed Latin in titles (`Бог Е Imperator на Дюна`) |
| Calques and stiff syntax | Word-for-word English order; unnatural aspect/case |
| Register slips | Dialogue that should sound spoken stays formal (or vice versa) |
| Series terminology drift | Same invented term rendered differently across chunks |
| Placeholder-safe but ugly | Markup is correct; prose quality is the failure mode |

Gemini already preserves structure via placeholders (`[[n]]…[[/n]]`). The
remaining gap is **Bulgarian language quality**, which is exactly what a
Bulgarian-adapted model is trained for.

**Goal of this plan:** add an optional **post-processing pass** that runs
**after** Gemini translation and **before** (or as a re-pass over) the rebuilt
EPUB units, using **BgGPT** to polish Bulgarian prose without breaking
structure, placeholders, or glossaries.

## 2. Research findings: BgGPT access options (2026)

INSAIT’s BgGPT 3.0 is a Bulgarian-adapted multimodal Gemma 3 family (4B / 12B /
27B). For this pipeline the relevant surfaces are:

### 2.1 Hosted OpenAI-compatible API (primary recommendation)

Source: [models.bggpt.ai/docs](https://models.bggpt.ai/docs)

| Item | Value |
|---|---|
| Base URL | `https://api.bggpt.ai/v1` |
| Model id (hosted) | `bggpt-gemma-3-27b-fp8` |
| Auth | `Authorization: Bearer <API_KEY>` |
| Context | up to **65 536** tokens (API); base model supports up to 131k |
| Quantization | FP8 dynamic on the hosted endpoint |
| Protocol | **Fully OpenAI Chat Completions compatible** (Python `openai` SDK, curl, LangChain, etc.) |
| Streaming | `stream=True` supported |
| Tools | OpenAI-style function calling supported (not required for polish) |
| Vision | Supported; **not needed** for text polish |
| API key | Request form: [bggpt.ai/contact?upgrade=true](https://bggpt.ai/contact?upgrade=true); manage at [bggpt.ai/settings](https://bggpt.ai/settings) |

Minimal client sketch (reference only):

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.bggpt.ai/v1",
    api_key=os.environ["BGGPT_API_KEY"],
)
resp = client.chat.completions.create(
    model="bggpt-gemma-3-27b-fp8",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": chunk_text},
    ],
    max_tokens=16384,
    temperature=0.2,  # docs examples use 0.2; prefer low for fidelity
)
text = resp.choices[0].message.content
```

### 2.2 Self-hosted / local (fallback when no API key or offline)

| Path | Notes |
|---|---|
| Hugging Face | `INSAIT-Institute/BgGPT-Gemma-3-27B-IT` (also 12B / 4B) |
| vLLM OpenAI server | `vllm serve INSAIT-Institute/BgGPT-Gemma-3-27B-IT [--quantization fp8]` then point the same OpenAI client at `http://localhost:8000/v1` |
| Ollama community tags | Unofficial ports exist (e.g. Gemma-based BgGPT tags); quality/version may lag official 3.0 |
| GGUF / llama.cpp | GGUF builds exist for lighter hardware; still OpenAI-compatible if served behind a local proxy |

**Design implication:** treat the provider as an **OpenAI-compatible endpoint +
model name + API key**, not a Gemini-specific client. The polish stage should
not depend on `google-genai`.

### 2.3 Chat UI only (out of scope)

[bggpt.ai](https://bggpt.ai) and the mobile apps are interactive chat, not a
batch pipeline. Do not scrape the chat UI.

### 2.4 Capability fit for literary polish

BgGPT 3.0 is marketed for improved instruction-following, multi-turn
conversation, and Bulgarian generative tasks vs prior versions. It is **not**
advertised as a dedicated MT system. That matches our intended role:

- **Gemini** = cross-language translation (EN → BG) + placeholder discipline  
- **BgGPT** = **in-language revision** of already-Bulgarian text (grammar,
  idiom, register, consistency), with strict “do not re-translate from
  English / do not invent content” constraints

## 3. Goals

1. Improve fluency, idiom, and literary register of Bulgarian units after
   Gemini translation.
2. Keep the existing **placeholder contract** intact (`[[n]]` multisets must
   match; no new / dropped / renumbered placeholders).
3. Respect the user **glossary** as authoritative for series terms and names.
4. Be **optional and resumable**: polish is a separate checkpointed stage so a
   finished Gemini run can be polished without re-paying for translation.
5. Work against either:
   - the checkpointed translated unit map (`chunks/*.json`), or  
   - a fully rebuilt Bulgarian EPUB (re-extract units → polish → rebuild).
6. Prefer the hosted BgGPT API; allow a local OpenAI-compatible base URL for
   self-hosting.
7. Stay offline-testable for pure EPUB/unit logic (mock the LLM layer).

## 4. Non-goals (this iteration)

- Replacing Gemini as the primary translator with BgGPT (BgGPT is weaker as a
  general EN→BG MT system than Gemini 3.x for long literary books; evaluate
  later if desired).
- Full human post-edit / CAT workflow, TM databases, or commercial MT APIs.
- Polishing non-Bulgarian targets (gate on `-t bg` / BCP-47 `bg*`).
- Translating `alt`/`title` attributes, scripts, styles, or images.
- Automatic glossary induction from the book (may be a later enhancement).
- Guaranteeing zero leftover English proper nouns that the glossary never
  covered (only reduce them; names without glossary entries remain best-effort).

## 5. Recommended architecture

### 5.1 Placement in the pipeline

```
Source EPUB
    │
    ▼
[existing] unpack → units → Gemini translate → chunks/*.json
    │
    ▼
[new] BgGPT polish stage  ──▶  chunks_polished/*.json  (or overwrite with new hash key)
    │
    ▼
[existing] rebuild XHTML from placeholders → TOC (optional re-polish labels) → repack EPUB
```

**Preferred integration shape:** a second CLI entry (or subcommand / flag) so
polish can run on an already-translated workdir without re-running Gemini:

```text
# Option A — flag on the existing script
translate_epub.py book.epub -t bg --polish-bggpt [--polish-only]

# Option B — sibling script (cleaner dependency split)
polish_epub_bg.py --workdir tmp/epub-translate/<book-id> -o book.bg.polished.epub
```

Recommendation: **Option B as the first ship**, calling into `epubdoc.py` for
unit extract/rebuild and sharing only retry/logging patterns with `common.py`.
Reason: polish needs the `openai` package and `BGGPT_API_KEY`, not
`google-genai` / `GEMINI_API_KEY`. A separate script avoids coupling two
providers in one process and matches the “post-process an existing translation”
workflow.

Option A (`--polish-bggpt` / `--polish-only`) can wrap the same core module
later for one-shot UX.

### 5.2 What gets sent to BgGPT

Same unit serialization as translation:

- Input: placeholder-ized Bulgarian strings per unit, chunked similarly
  (`--target-words`, default can be **lower** than translate — polish benefits
  from slightly smaller chunks for careful rewriting, e.g. 800–1200 words).
- Format: reuse `<<<n>>>` markers so parsing stays identical to
  `_translate_units` / `UNIT_MARKER_RE`.
- Optionally include a **short source English side** for ambiguous units only
  (see §6.3). Default: **BG-only polish** to minimize tokens and avoid
  re-translation drift.

### 5.3 What must not change

Validation before accepting a polished unit (mirror translate acceptance):

1. Placeholder multiset equality vs input unit (`epubdoc.placeholder_ids`).
2. Non-empty output for non-empty input.
3. Still predominantly Cyrillic for long units (`CYRILLIC_FLOOR` or slightly
   higher, e.g. 0.50).
4. No unit merge/split/reorder (same `<<<n>>>` set).
5. Length sanity: reject if output length is wildly shorter/longer than input
   (e.g. outside ~0.4×–2.5× character ratio) — catches summarization or
   runaway expansion.
6. Optional: glossary term presence check for high-priority terms that appear
   in the **source English** unit (if English is available in the checkpoint
   metadata) — or that appear in the pre-polish BG unit.

On validation failure: **keep the pre-polish Gemini text** and warn (same
degrade-not-abort philosophy as translation).

### 5.4 Checkpoints

- Content-addressed keys over: polish prompt version, model id, temperature,
  glossary hash, and **pre-polish unit text** (not the English source alone).
- Store under e.g. `workdir/<book-id>/polish/<chunk_hash>.json` mapping
  `unit_index → polished_text`.
- Resume: skip chunks whose polish checkpoint is present and valid.
- Never mutate Gemini `chunks/*.json` in place; keep the raw translation as
  the recoverable baseline.

### 5.5 Rebuild path

Two supported inputs:

1. **From workdir** (best): load pre-polish maps + polish maps → apply to a
   freshly unpacked source EPUB (same as translate does today) → repack.
2. **From Bulgarian EPUB only**: unpack the already-translated EPUB, treat
   units as “source” for polish, rebuild. Useful when Gemini checkpoints were
   discarded. Slightly riskier (no English fallback).

## 6. Prompt design (polish stage)

New file (proposed):  
`plugins/pdf2epub/skills/epub-translate/scripts/prompts/polish_bg_chunk.md`

### 6.1 Role

Instruct BgGPT as a **Bulgarian literary editor / post-editor**, not as a
translator:

- Input language: Bulgarian (machine-translated).
- Output language: Bulgarian only.
- Task: fix grammar, agreement, aspect, word order, calques, register, and
  awkward phrasing while **preserving meaning, plot facts, and dialogue
  intent**.

### 6.2 Hard rules (must be explicit)

1. Keep every `<<<n>>>` marker; never merge/split/reorder units.
2. Carry every `[[n]]` / `[[n/]]` / `[[/n]]` placeholder unchanged in identity;
   may move pairs with the words they emphasize.
3. Do **not** summarize, expand with explanations, add footnotes, or censor.
4. Do **not** re-translate from imagined English; edit the given Bulgarian.
5. Glossary terms (if supplied) are authoritative; inflect as needed for case
   but do not substitute synonyms.
6. Normalize inconsistent transliterations of the **same** name within the
   chunk toward the glossary form when present; otherwise toward the dominant
   form already in the chunk.
7. Leave intentional non-native speech (e.g. Futars’ broken speech) broken if
   the source style is clearly non-fluent — prefer a light touch over
   “fixing” dialect into standard Bulgarian.
8. Output only markers + text (same discipline as `translate_chunk.md`).

### 6.3 Optional EN reference mode

For hard units (validation failures, or user `--polish-with-source`):

```
<<<n>>>
[BG] …current Bulgarian…
[EN] …original English unit…
```

BgGPT may use EN only to resolve meaning errors; output remains BG-only under
the same marker. Default off to control cost and avoid EN leakage.

### 6.4 Temperature / decoding

- Default `temperature=0.2` (matches BgGPT docs examples; favors stable edits).
- `max_tokens` sized to ~1.5–2× estimated input tokens for the chunk (API allows
  up to 16384).
- No need for Gemini-style `thinking_level`.

## 7. Client / config design

### 7.1 Environment

| Variable | Purpose |
|---|---|
| `BGGPT_API_KEY` | Bearer token for hosted API (required unless local server allows a dummy key) |
| `BGGPT_BASE_URL` | Default `https://api.bggpt.ai/v1`; override for vLLM/Ollama local |
| `BGGPT_MODEL` | Default `bggpt-gemma-3-27b-fp8` |

CLI flags mirror these for one-off runs (`--bggpt-base-url`, `--bggpt-model`).

### 7.2 Dependencies

- Add `openai>=1.40` (or current stable) via PEP 723 for the polish script.
- Do **not** force `openai` onto the Gemini-only `translate_epub.py` path.
- Retry/backoff: implement a small OpenAI-compatible retry helper (429 / 5xx /
  timeouts). Prefer extracting a thin `openai_client.py` next to the polish
  script rather than bloating `common.py` with a second provider — unless a
  shared interface is introduced carefully for both skills.

### 7.3 Concurrency

- Start with `--concurrency 2` default (unknown hosted rate limits).
- Document that users should lower concurrency on 429 storms.
- Reuse the same thread-pool + per-worker backoff pattern as translation.

### 7.4 Cost / runtime expectations (order of magnitude)

Unknown public pricing for BgGPT API as of research time — treat as:

- Same order of book-sized token volume as the Gemini translation pass (roughly
  one full BG book in + one full BG book out).
- Always smoke-test with `--max-chunks 2` / `--doc-filter` before a full polish.
- Log token usage if the API returns `usage` fields.

## 8. CLI sketch

```bash
uv run plugins/pdf2epub/skills/epub-translate/scripts/polish_epub_bg.py \
  --workdir tmp/epub-translate/<book-id> \
  --source-epub /path/to/original.en.epub \
  -o /path/to/book.bg.polished.epub \
  --glossary /path/to/glossary.json \
  --model bggpt-gemma-3-27b-fp8 \
  --concurrency 2 \
  --target-words 1000
```

Alternate input:

```bash
# Polish an already-emitted BG EPUB (no Gemini checkpoints)
uv run .../polish_epub_bg.py \
  --input-epub /path/to/book.bg.epub \
  -o /path/to/book.bg.polished.epub \
  --glossary ...
```

Flags of note:

| Flag | Meaning |
|---|---|
| `--polish-only` / default behavior | Do not call Gemini |
| `--with-source-en` | Attach English originals from Gemini checkpoints when available |
| `--max-chunks` / `--doc-filter` | Same smoke-test controls as translate |
| `--force` | Ignore polish checkpoints |
| `--dry-run-sample N` | Print N before/after unit pairs to stdout for human QA |

## 9. Quality evaluation plan (before declaring success)

Do **not** merge polish as default-on until a small eval set is reviewed.

1. **Smoke set:** 20–40 units sampled from current Hunters/Sandworms runs —
   include names, dialogue, epigraphs, invented terms, and placeholder-heavy
   lines.
2. **Side-by-side review:** Gemini-only vs Gemini+BgGPT (human or dual-LLM
   judge with a fixed rubric: fidelity, fluency, name consistency, markup).
3. **Automatic gates:**
   - 0 placeholder mismatches accepted
   - polish acceptance rate ≥ X% of units (target: ≥95% on literary prose)
   - glossary hit-rate non-decreasing for terms present in both EN and BG
4. **Regression tests (offline):**
   - pure functions: parse markers, reject bad placeholder edits, length ratio
   - mock OpenAI client returning fixed strings
   - round-trip: polish that returns identical text is a no-op rebuild

## 10. Implementation phases

### Phase 0 — Access & probe (manual, ~1 hour)

1. Obtain `BGGPT_API_KEY` via the INSAIT form / account settings.
2. Probe with a 5–10 unit real chunk from `tmp/epub-translate/.../chunks/`.
3. Confirm latency, max throughput, and whether long outputs truncate.
4. Record any rate-limit headers or error shapes in this doc’s appendix after
   the probe.

### Phase 1 — Offline scaffolding

1. Add `polish_epub_bg.py` + `prompts/polish_bg_chunk.md`.
2. Reuse `epubdoc` for unit extract / rebuild / pack.
3. Checkpoint layout + acceptance validators.
4. Unit tests with mocked completions.

### Phase 2 — Live polish path on workdir

1. Read Gemini chunk maps; write polish maps; rebuild EPUB.
2. Wire glossary injection.
3. Logging summary: polished / kept-as-is / failed validation.

### Phase 3 — Skill docs & UX

1. Extend `epub-translate/SKILL.md` with a “Bulgarian polish (BgGPT)” section.
2. README table row / CLI examples.
3. Env var checklist (`BGGPT_API_KEY`, optional base URL).
4. Version bump of the plugin when shipping (per CLAUDE.md release rule).

### Phase 4 — Optional hardening (later)

1. `--with-source-en` mode.
2. TOC label polish pass (nav documents).
3. Whole-book name consistency pass (extract proper-noun candidates → normalize
   via one BgGPT call + glossary merge).
4. Evaluate whether a smaller local model (12B/4B) is “good enough” for polish
   cost/latency tradeoffs.
5. Consider dual-pass: Gemini translate → BgGPT polish as default for `-t bg`
   only after eval metrics look solid.

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| BgGPT “improves” by rewriting plot or soft-censoring | Strict fidelity prompt; length-ratio guard; keep Gemini text on failure |
| Placeholders corrupted | Same multiset check as translate; reject unit |
| Hosted API access delayed / quotas | OpenAI-compatible local vLLM path; document both |
| Double cost (Gemini + BgGPT) | Optional stage; resume; lower concurrency; smoke flags |
| Over-correcting dialect / alien speech | Prompt carve-out; human sample review |
| Glossary ignored | Inject glossary block; optional post-check for key terms |
| Model returns preamble / fences | Strip fences; require marker parse; retry once with stricter reminder |
| API model id changes | `--model` / `BGGPT_MODEL` override; pin default in one constant |

## 12. Decisions to confirm before coding

1. **Ship as sibling script vs flag on `translate_epub.py`?**  
   Recommendation: sibling script first (§5.1).
2. **Default on for `-t bg` or opt-in forever?**  
   Recommendation: **opt-in** until Phase 0–2 eval passes.
3. **Hosted API only for v1, or local base URL in the first PR?**  
   Recommendation: support both from day one (trivial if OpenAI client is used).
4. **Is EN side-channel needed in v1?**  
   Recommendation: no — BG-only polish first.
5. **Plugin versioning:** polish is a new capability → minor version bump when
   released; docs-only plan does not require a bump.

## 13. Success criteria

- A full book that was already translated with Gemini can be polished via
  BgGPT without re-calling Gemini.
- Placeholder and structure guarantees remain identical to today’s translator.
- Spot checks on previously awkward units (names, calques, leftover English)
  show clear improvement without factual drift.
- Offline tests cover acceptance logic without network.
- Operators can point at either `https://api.bggpt.ai/v1` or a local vLLM
  server with the same flags.

## 14. References (research snapshot)

- BgGPT API docs: https://models.bggpt.ai/docs  
  - Base `https://api.bggpt.ai/v1`, model `bggpt-gemma-3-27b-fp8`, OpenAI SDK
  - API key request: https://bggpt.ai/contact?upgrade=true  
  - Key management: https://bggpt.ai/settings  
- Model cards: https://huggingface.co/INSAIT-Institute/BgGPT-Gemma-3-27B-IT  
  (vLLM serve + FP8 notes; Gemma Terms of Use)  
- Product hub: https://models.bggpt.ai/  
- Existing pipeline: `plugins/pdf2epub/skills/epub-translate/`  
  (`translate_epub.py`, `epubdoc.py`, `prompts/translate_chunk.md`)

---

## Appendix A — Observed defect samples (motivation, not exhaustive)

From `tmp/epub-translate/Hunters_of_Dune_…/chunks/` (Gemini output):

- Title page: author rendered as **„Калин Дж. Андърсън“** (should be Кевин /
  Kevin J. Anderson under a proper glossary).
- Series list: **„Бог Е Imperator на Дюна“** — mixed Latin leftover.
- Name oscillation: **Хербърт / Хърбърт** across nearby units.
- Stiff dialogue calques in non-native speech lines (some intentional; polish
  must not “fix” them into perfect Bulgarian).

These are exactly the class of fixes a Bulgarian-native post-editor model is
for — provided the prompt forbids content invention and the validators protect
markup.
