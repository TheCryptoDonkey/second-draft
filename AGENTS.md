# AGENTS.md — how an LLM agent should use second-draft

You are an agent asked to "wash" text (restyle AI-generated prose or code
comments into natural human writing). This file is your operating manual.

## What this tool does

second-draft paraphrases text through a NON-origin model so AI-assisted
drafts read as natural human prose. Post-2-Aug-2026 EU models embed
statistical patterns deliberately (EU AI Act); rewriting through a different
vendor's model replaces those patterns. Guardrails make failures safe:
any rewrite that drops a URL, number, name or technical term, drifts too far
in length, echoes the prompt, fails the meaning check, or introduces
AI-tell phrases / American spellings is rejected and the original is kept.

## Command reference

```sh
# prose (markdown) — interactive hunk review (PREFERRED for anything important)
python3 second-draft.py path/to/file.md

# prose, batch mode (agent use — no human at the keyboard)
python3 second-draft.py --yes path/to/file.md        # writes file.washed.md

# also strip invisible Unicode carriers (zero-width, bidi, tag chars) first
python3 second-draft.py --scrub --yes path/to/file.md

# code comments across many files (edits in place, test-gated)
python3 second-draft.py --code --check "npm test" 'src/**/*.mjs'
```

Key flags: `--yes` batch-accept (guardrails still apply), `--in-place`
(overwrite the source; default writes `<file>.washed.md`), `--bestof 5`
(generate 3 candidates, keep the best — the measured quality lever),
`--retries N`, `--min-words N`, `--verify`/`--no-verify` (backend checks each
candidate preserves meaning; default on for ollama, off for paid backends),
`--scrub` (deterministic pre-pass that strips
invisible Unicode carriers: zero-width chars, bidi controls, tag characters,
variation selectors; normalises exotic spaces; whole file, no model involved;
pure-ASCII input passes through byte-identical).

## Choosing a backend

| Backend | When | Command bits |
|---|---|---|
| `ollama` (default) | Always try first. Free, unlimited, private | `--model qwen3:32b --bestof 5` |
| `copilot` | Best quality; costs ~4.5 Copilot AI credits per paragraph | `--backend copilot` |
| `moonshot` | Cheapest at volume; needs `MOONSHOT_API_KEY` in env | `--backend moonshot` |

The ollama backend may point at a remote server:
`export OLLAMA_HOST=http://ollama-host.local:11434`
(a beefier LAN box, which serves qwen3:32b). **If that host is unreachable
(ping it first), the laptop is asleep or off-network — fall back to
`--backend copilot`.** Do not burn more than ~100 credits without asking.

Model choice on ollama: use the benchmarked `qwen3:32b --bestof 5` recipe.
Do not pull or substitute other local models without a bench run: `bench.py`.

## Hard rules

1. **Never wash working notes.** The tool already excludes bold-lead blocks,
   lists, headers and code, but if you are washing a drafts file, only
   the plain publishable paragraphs are candidates. If in doubt, wash less.
2. **Never use a Claude model as the paraphrase engine** — that re-marks the
   text, defeating the entire purpose.
3. **Never commit the washed file over the original** without the user's
   explicit say-so. Default output is a sibling `.washed.md`.
4. **Never put `MOONSHOT_API_KEY` in a file.** Environment variable only.
5. Verify output before declaring success: no non-ASCII, no em/en-dashes,
   no AI tells (`delve|crucial|seamless|furthermore|moreover`), working notes
   byte-identical. The tool enforces most of this; you check the diff.

## After a run

Report: hunks accepted / guardrail-kept, which backend and model were used,
credit or time cost, and show 2-3 before/after paragraph pairs so the user
can judge voice. The user's final read-aloud pass is part of the pipeline —
tell them to do it; it is also the last watermark-killer.

## House style (enforced automatically, but know it)

UK British English; software-developer register; two spaces after full stops;
semicolons preferred over dashes; em-dashes and en-dashes are forbidden;
straight ASCII quotes only; no AI-tell vocabulary.
