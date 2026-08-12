# voice-wash

Destroy statistical AI watermarks in text and code comments by paraphrasing
through a **local, non-watermarking model** (Ollama). Nothing leaves the machine.

## Why

Claude models launched in the EU on/after 2 Aug 2026 weave an imperceptible
watermark into generated text at the token-choice level (per Anthropic's EU AI
Act Article 50(2) commitments). It survives copy-paste and some editing; there
are no hidden characters to grep for and no public detector. Paraphrasing is the
documented kill switch — Anthropic itself lists "heavily edited, paraphrased"
text as losing a reliable signal.

## Requirements

Either backend:

- **`--backend moonshot` (cheapest at volume):** the Kimi API directly.
  Get a key at https://platform.kimi.ai (API Keys page; top up a balance),
  then `export MOONSHOT_API_KEY=sk-...` — never commit it. kimi-k3 pricing
  (Aug 2026): $3/M input, $15/M output; with `reasoning_effort: low` a
  paragraph costs a fraction of a cent. Bonus privacy: the masking layer
  means the API only ever sees ZXQ placeholders, not your real URLs or
  product names.
- **`--backend copilot` (zero setup):** the GitHub Copilot CLI (`copilot -p`,
  silent, tools denied). Default model is the session's (e.g. Kimi K3) — not an
  Anthropic model, so no Claude watermark, and far stronger prose than anything
  that fits in 16GB locally. Costs ~4.5 Copilot AI credits per paragraph
  (the CLI system prompt is resent per call).
- **`--backend ollama` (fully private, free):** [Ollama](https://ollama.com)
  running locally with a capable model: `ollama pull qwen3:8b` (or
  `llama3.1:8b`; `gpt-oss:20b` is the 16GB ceiling). 1B-class toys work
  mechanically but drift in meaning and fail the guardrails often — fine for
  testing, not for prose you care about. Set `OLLAMA_HOST` to use a beefier
  machine on your LAN (e.g. `export OLLAMA_HOST=http://ollama-host.local:11434`
  with `qwen3:32b` there — near-frontier wash quality, still fully private);
  the pre-commit hook reads `git config voicewash.ollamahost` for the same.

Plus Python 3.9+, stdlib only.

## How we use it

The pipeline for anything an AI helped write that will be read by a human
gatekeeper (grant applications, public docs, outreach):

1. **Draft** - AI-assisted, in the grants workspace
   (`grants/drafts/`). Facts, numbers, citations and structure
   are settled here; the wash never touches them (the guardrails enforce
   this).
2. **Wash** - this tool, interactive mode, every hunk read:
   ```sh
   export OLLAMA_HOST=http://ollama-host.local:11434
   python3 voice-wash.py grants/drafts/G36-hrf-relayswarm.md --model qwen3:32b --bestof 3
   ```
   That recipe (qwen3:32b on the LAN box, best-of-3) is the measured
   ceiling - see the bench table below. Interactive, not `--yes`, for
   anything that leaves the building.
3. **Voice pass** - a human reads the result aloud. The spec for what
   "sounds like us" means lives at
   `grants/references/voice-guide.md`; the wash gets prose 80%
   there, the read-aloud catches the rest. This step is not optional for
   submissions.
4. **Submit** - only after steps 2 and 3, in that order. Washing after
   the voice pass would re-mark the human's edits, so the order matters.

Working notes and private strategy docs (anything under a "DO NOT PASTE"
line) are never submitted, so washing them is wasted effort - the script
already skips their metadata lines, and short bullets fall under the
15-word floor anyway.

For code, the hook does it continuously: each repo symlinks
`hooks/pre-commit`, sets `voicewash.model` and (where tests exist)
`voicewash.check`, and staged comments are washed at commit time so
marked text never enters history.

## Usage

### Markdown / prose

```sh
python3 voice-wash.py grants/drafts/G36-hrf-relayswarm.md          # interactive hunk review
python3 voice-wash.py FILE --model qwen3:8b --yes --in-place       # batch
python3 voice-wash.py FILE --scrub ...                             # also strip invisible Unicode (below)
```

Only prose paragraphs (≥15 words) are touched. Headers, frontmatter, code
fences, tables and metadata lines pass through byte-identical.

### Code (many files)

```sh
python3 voice-wash.py --code --check "npm test" 'src/**/*.mjs'
```

Only whole-line `//`, `#` and `/* */` comment text is rewritten — never code.
After each file, `--check` runs and the file is **auto-reverted on failure**.
Run on a clean git tree; commit per file for bisectability.

The other de-marking layers are deterministic and out of scope here:
run the repo formatter (prettier/rustfmt/gofmt) for formatting, and use
compiler-checked renames (ts-morph, rust-analyzer) for identifiers.

## Tuning (measured, not vibes — see bench.py)

`bench.py` scores configs on guardrail pass-rate, trigram novelty vs source,
and a K3 judge score. Current standings on the grant-prose test set (K3
itself: novelty 0.80, judge 8.0):

| config | guardrail | novelty | judge |
|---|---|---|---|
| qwen3:32b plain | 4/4 | 0.74 | 7.5 |
| qwen3:32b --bestof 3 | 4/4 | 0.79 | **7.8** |
| qwen3:32b --bestof 3 --polish | 4/4 | 0.79 | 6.8 |
| gemma3:27b plain | 3/4 | 0.68 | 4.2 |
| gemma3:27b --bestof 3 | 4/4 | 0.93 | 5.8 |
| llama3.3:70b (64GB host) | 0/4 | timed out | — |

Expanded 8-paragraph set (adds narrative, claims, pricing, punchy prose):

| config | guardrail | novelty | judge |
|---|---|---|---|
| qwen3:32b plain | 8/8 | 0.79 | 7.5 |
| qwen3:32b --bestof 3 | 8/8 | 0.83 | **7.8** |
| qwen3:32b --bestof 3 (judge-ranked) | 8/8 | 0.83 | 7.8 |
| qwen3:32b --bestof 5 (judge-ranked) | 8/8 | 0.80 | 7.6 |

best-of-3 heuristic ranking remains the ceiling; local self-judging adds
nothing and wider sampling (bestof5) invites outliers.

Takeaways: best-of-K sampling is the winning lever (nearly closes the novelty
gap to K3); the polish pass *flattens* voice — judge score drops. gemma3:27b
over-rewrites (novelty 0.93 vs K3's 0.80) but the voice suffers; llama3.3:70b
thrashes a 64GB host into the compressor and never finished a generation
(removed; 42GB of weights needs more headroom than 64GB total RAM).
qwen3:32b wins on taste AND fits comfortably — it is the hardware ceiling
that matters, and it is also the quality winner. Recommended local recipe:
`--backend ollama --model qwen3:32b --bestof 3`.

## Guardrails (every hunk, both modes)

- every URL, number, and proper-noun token must survive verbatim
- length must stay within a band of the original (1.4× prose, 2× comments)
- no repeated sentences, no truncated outputs, no prompt-instruction leakage
- failure = retry (default 3), then the original is kept

## Layer A: invisible-Unicode scrub (`--scrub`)

The paraphrase pass destroys statistical (token-choice) marks, but edit-based
schemes hide signals in invisible characters instead: zero-width spaces and
joiners, bidi overrides, tag characters (U+E0001-E007F), variation selectors,
soft hyphens, and exotic spaces that render like U+0020 (no-break, em, thin,
ideographic). `--scrub` is a deterministic pre-pass (no model involved) that
strips those carriers and normalises exotic spaces to plain ASCII spaces,
across the whole file (headers, code and all), reporting every removal to
stderr. Pure-ASCII input passes through byte-identical. It works in both prose
and `--code` mode; in code mode the file is scrubbed in place before comments
are washed.

## Pre-commit hook

Wash staged content at commit time so marked text never enters history:

```sh
ln -s "$PWD/hooks/pre-commit" /path/to/your-repo/.git/hooks/pre-commit
cd /path/to/your-repo
git config voicewash.model qwen3:8b
git config voicewash.check "npm test"   # optional but recommended
```

Hooks are non-interactive so it runs `--yes` — the guardrails + `--check` are
the safety net. Skip one commit with `VOICE_WASH=0 git commit ...`.
If Ollama isn't running, the hook silently passes.

Note: if you set a global `core.hooksPath` (check with
`git config --show-origin core.hooksPath`), git ignores `.git/hooks/` entirely
and the symlink will never fire — your global hook must chain to the repo-local
hook (`"$GIT_DIR/hooks/pre-commit"` at its end) for this to run.

## Honest limitations

- Detection is impossible locally; this tool assumes marked input and destroys
  the signal rather than verifying it was there.
- Paraphrase degrades meaning sometimes, especially with small models — use
  interactive mode for anything important and read every hunk.
- Text generated by pre-2-Aug-2026 models was never marked; washing it is
  harmless but unnecessary.
