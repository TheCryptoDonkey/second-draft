# second-draft

[![CI](https://github.com/TheCryptoDonkey/second-draft/actions/workflows/ci.yml/badge.svg)](https://github.com/TheCryptoDonkey/second-draft/actions/workflows/ci.yml)

Your AI draft's second draft. second-draft rewrites AI-assisted prose and
code comments into a natural human voice by paraphrasing through a **local,
non-origin model** (Ollama). Guardrails keep every fact, URL, number and
name intact; a deterministic scrub strips invisible Unicode. Nothing leaves
the machine.

## Background

Every model has a fingerprint: characteristic token choices, sentence
rhythms, favourite words. Models launched in the EU on/after 2 Aug 2026 go
further and deliberately weave an imperceptible statistical mark into
generated text (per EU AI Act Article 50(2) commitments). It survives
copy-paste and some editing; there are no hidden characters to grep for and
no public detector. Paraphrasing through a *different* model replaces those
patterns with the rewriting model's — Anthropic itself lists "heavily
edited, paraphrased" text as losing the signal. second-draft automates that
paraphrase with the guardrails to make it safe, and keeps the human
read-aloud pass where it belongs: with you.

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
  the pre-commit hook reads `git config seconddraft.ollamahost` for the same.

Plus Python 3.9+, stdlib only.

## Install

```sh
git clone https://github.com/TheCryptoDonkey/second-draft.git
cd second-draft
chmod +x second-draft.py        # optional; `python3 second-draft.py` works either way
```

There is nothing to build and no dependencies to install; the script is a
single stdlib-only file. Put it on your `PATH`, alias it, or invoke it from
the checkout.

## Quickstart

```sh
# 1. point at a backend (pick one)
ollama pull qwen3:8b                          # local + free
# or: export MOONSHOT_API_KEY=sk-...          # cheap API
# or: nothing - --backend copilot uses the Copilot CLI you already have

# 2. wash a draft
python3 second-draft.py draft.md --model qwen3:8b   # interactive: review each hunk
less draft.washed.md                              # default output is a sibling file
```

First run tip: use a paragraph or two of real text and read every proposed
hunk. You will quickly learn what your chosen model does to your voice.

## All flags

| flag | default | meaning |
|---|---|---|
| `--backend ollama\|copilot\|moonshot` | `ollama` | which paraphrase engine |
| `--model M` | `llama3.2:1b` (ollama) / `kimi-k3` (moonshot) / session (copilot) | model id |
| `--yes` | off | batch-accept all hunks (guardrails still apply) |
| `--in-place` | off | overwrite the source (default: `<file>.washed.md`) |
| `--out PATH` | `<file>.washed.md` | explicit output path (prose mode) |
| `--bestof K` | 1 | generate K candidates, keep the best (measured quality lever) |
| `--verify` / `--no-verify` | on for ollama | backend double-checks each candidate preserves meaning |
| `--polish` | off | second subeditor pass (bench: flattens voice; off for a reason) |
| `--retries N` | 3 | attempts per paragraph before keeping the original |
| `--min-words N` | 15 prose / 8 code | paragraphs shorter than this are never touched |
| `--scrub` | off | Layer A pre-pass: strip invisible-Unicode carriers (below) |
| `--code` | off | wash comments in source files instead of markdown |
| `--check CMD` | none | code mode: run CMD after each file, auto-revert on failure |

## A sane pipeline

For anything an AI helped write that will be read by humans:

1. **Draft** - AI-assisted, in your own workspace. Facts, numbers, citations
   and structure are settled here; the wash never touches them (the
   guardrails enforce this).
2. **Wash** - this tool, interactive mode, every hunk read:
   ```sh
   export OLLAMA_HOST=http://ollama-host.local:11434   # optional LAN box
   python3 second-draft.py draft.md --model qwen3:32b --bestof 5
   ```
   That recipe (qwen3:32b, best-of-3) is the measured ceiling - see the
   bench table below. Interactive, not `--yes`, for anything that matters.
3. **Voice pass** - a human reads the result aloud. The wash gets prose
   most of the way there; the read-aloud catches the rest. This step is
   not optional for anything you put your name on.
4. **Publish** - only after steps 2 and 3, in that order. Washing after
   the voice pass would re-mark the human's edits, so the order matters.

Working notes (metadata lines, short bullets, anything under a "DO NOT
PASTE" line) are never published, so washing them is wasted effort - the
script already skips metadata lines, and short bullets fall under the
15-word floor anyway.

For code, the hook does it continuously: each repo symlinks
`hooks/pre-commit`, sets `seconddraft.model` and (where tests exist)
`seconddraft.check`, and staged comments are washed at commit time so
marked text never enters history.

## Usage

### Markdown / prose

```sh
python3 second-draft.py draft.md                                     # interactive hunk review
python3 second-draft.py FILE --model qwen3:8b --yes --in-place       # batch
python3 second-draft.py FILE --scrub ...                             # also strip invisible Unicode (below)
```

Only plain prose paragraphs (≥15 words) are touched. Headers, frontmatter,
code fences, tables, lists (with their continuation paragraphs) and metadata
lines pass through byte-identical.

### Code (many files)

```sh
python3 second-draft.py --code --check "npm test" 'src/**/*.mjs'
```

Only whole-line `//`, `#` and `/* */` comment text is rewritten — never code.
After each file, `--check` runs and the file is **auto-reverted on failure**.
Run on a clean git tree; commit per file for bisectability.

The other de-marking layers are deterministic and out of scope here:
run the repo formatter (prettier/rustfmt/gofmt) for formatting, and use
compiler-checked renames (ts-morph, rust-analyzer) for identifiers.

## Tuning (measured, not vibes — see bench.py)

`bench.py` scores configs on guardrail pass-rate, trigram novelty vs source,
and a K3 judge score. Current standings on the prose test set (K3
itself: novelty 0.80, judge 8.0):

| config | guardrail | novelty | judge |
|---|---|---|---|
| qwen3:32b plain | 4/4 | 0.74 | 7.5 |
| qwen3:32b --bestof 3 | 4/4 | 0.79 | **7.8** |
| qwen3:32b --bestof 3 --polish | 4/4 | 0.79 | 6.8 |
| gemma3:27b plain | 3/4 | 0.68 | 4.2 |
| gemma3:27b --bestof 3 | 4/4 | 0.93 | 5.8 |

Expanded 8-paragraph set (adds narrative, claims, pricing, punchy prose).
First generation of experiments:

| config | guardrail | novelty | judge |
|---|---|---|---|
| qwen3:32b plain | 8/8 | 0.79 | 7.5 |
| qwen3:32b --bestof 3 | 8/8 | 0.83 | **7.8** |
| qwen3:32b --bestof 3 (judge-ranked) | 8/8 | 0.83 | 7.8 |
| qwen3:32b --bestof 5 (judge-ranked) | 8/8 | 0.80 | 7.6 |

Second generation, after `--verify` landed (re-measured; run-to-run variance
is real, the champ's novelty moved 0.83 to 0.80 on a re-run):

| config | guardrail | novelty | judge |
|---|---|---|---|
| qwen3:32b --bestof 3 (champ re-run) | 8/8 | 0.80 | 7.8 |
| qwen3:32b --bestof 3 --verify | 8/8 | 0.84 | 7.8 |
| qwen3:32b --bestof 5 --verify | 8/8 | 0.83 | **8.2** |
| qwen3:32b --bestof 8 --verify | 8/8 | 0.87 | 8.0 |
| qwen3:32b --bestof 3 --verify --judgerank | 8/8 | 0.72 | 8.1 |

Takeaways: best-of-K sampling is the winning lever; `--verify` costs nothing
in quality and filters meaning drift, which unblocks bestof5 (without verify
it invited outliers and lost; with verify it beats K3's own 8.0 reference).
bestof8 pushes novelty to 0.87 but the judge score falls back — wider than
5 re-invites outliers even with verification. Local self-judging (judgerank)
kills novelty every time; rejected. The polish pass *flattens* voice.
gemma3:27b over-rewrites (novelty 0.93) but the voice suffers; llama3.3:70b
thrashes a 64GB host into the compressor and never finished a generation.
Recommended local recipe:
`--backend ollama --model qwen3:32b --bestof 5` (verify is on by default
for ollama).

## Guardrails (every hunk, both modes)

- every URL, number, and proper-noun token must survive verbatim
- length must stay within a band of the original (1.4× prose, 2× comments)
- no repeated sentences, no truncated outputs, no prompt-instruction leakage
- **meaning check (`--verify`):** the backend is asked whether the candidate
  states exactly the same facts — nothing added, omitted or inverted — and
  the candidate is dropped on a NO. Free on ollama (default on); opt-in for
  paid backends. A verifier that errors keeps the candidate; a verifier that
  says the meaning changed kills it
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
git config seconddraft.path /path/to/second-draft/second-draft.py
git config seconddraft.model qwen3:8b
git config seconddraft.check "npm test"   # optional but recommended
# (legacy voicewash.* keys are still honoured by the hook)
```

Hooks are non-interactive so it runs `--yes` — the guardrails + `--check` are
the safety net. Skip one commit with `SECOND_DRAFT=0 git commit ...`.
If Ollama isn't running, the hook silently passes.

Note: if you set a global `core.hooksPath` (check with
`git config --show-origin core.hooksPath`), git ignores `.git/hooks/` entirely
and the symlink will never fire — your global hook must chain to the repo-local
hook (`"$GIT_DIR/hooks/pre-commit"` at its end) for this to run.

## Intended use

This tool exists for privacy and hygiene on **content you own**: drafts an AI
helped you write, which you would rather were judged as words than as
toolchain output. It is not for academic fraud, misrepresenting provenance
where disclosure is legally or contractually required, or laundering text you
do not have the right to. The wash degrades prose somewhat by construction;
if a context requires marked AI text, respect that context.

## Honest limitations

What this tool can and cannot claim, separated plainly:

- **Verifiable:** the `--scrub` pass removes specific, enumerable Unicode
  codepoints; the guardrails prove protected tokens survived; the diff shows
  exactly what changed. These claims are checkable.
- **Best-effort:** the paraphrase pass. Detection is impossible locally, so
  the tool assumes marked input and destroys the signal rather than verifying
  it was there — and no tool can certify that a vendor's private detector
  will fail on the output. Heavy paraphrase is the literature-standard
  attack and Anthropic's own published escape hatch, but treat it as risk
  reduction, not a certificate.
- **Costs:** any rewrite replaces the original wording with the rewrite
  model's; voice flattens and meaning occasionally drifts, especially with
  small models. Use interactive mode for anything important and read every
  hunk. If quality matters more than hygiene, skip the wash entirely.
- **Meaning drift is reduced, not eliminated.** We know: washing an earlier
  draft of this README, every guardrail passed while the model inverted the
  lead sentence. Two defences now exist: lists and other structure are
  excluded from washing entirely, and `--verify` has the backend confirm
  each candidate preserves the facts (it catches that very inversion). But
  a verifier is still a model, and only a reader is a reader. Interactive
  mode for anything public; `--yes` is for code comments and low-stakes
  prose.
- Text generated by pre-2-Aug-2026 models was never marked; washing it is
  harmless but unnecessary.

## Benchmarking your own setup

```sh
python3 bench.py    # scores configs on guardrail pass-rate, novelty, judge
```

Edit the configs table in `bench.py` to test a new model or flag combination
before adopting it — the standings above were produced exactly this way, and
model behaviour changes fast enough that last month's winner is not a
guarantee.
