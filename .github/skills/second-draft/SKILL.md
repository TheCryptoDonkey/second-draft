---
name: second-draft
description: Give AI-assisted drafts a human second draft. Paraphrase prose, markdown or code comments through a non-origin model (Ollama/Copilot/Kimi) so the text reads as natural human writing in the user's voice. Use when the user asks to "wash", restyle, humanise, rephrase or give a second draft to text, markdown, drafts, or code comments.
---

# second-draft

Paraphrase text through a NON-origin model so AI-assisted drafts read as
natural human prose. Post-2-Aug-2026 EU models embed statistical patterns
deliberately (EU AI Act); rewriting through a different vendor's model
replaces those patterns. Guardrails reject any rewrite that drops a URL,
number, name or technical term, drifts too far in length, echoes the prompt,
fails the meaning check, or introduces AI-tell phrases / American
spellings — the original is kept.

## Environment

Default ollama host (a beefier LAN box serving qwen3:32b):

```sh
export OLLAMA_HOST=http://ollama-host.local:11434
```

**Check reachability first** (`curl -s -m 3 $OLLAMA_HOST/api/tags`). If the
host is unreachable the laptop is asleep or off-network — fall back to
`--backend copilot`. Do not burn more than ~100 Copilot credits without asking.

## Commands

Run from wherever the second-draft repo is checked out (the directory
containing second-draft.py).

```sh
# prose — interactive hunk review (PREFERRED for anything important)
python3 second-draft.py path/to/file.md --model qwen3:32b --bestof 5

# prose, batch mode (no human at the keyboard) — writes file.washed.md
python3 second-draft.py --yes --model qwen3:32b --bestof 5 path/to/file.md

# code comments across many files (edits in place, test-gated)
python3 second-draft.py --code --check "npm test" 'src/**/*.mjs'
```

Key flags: `--yes` batch-accept (guardrails still apply), `--in-place`
(overwrite source; default writes a sibling `.washed.md`), `--bestof 5`
(quality lever — generate 3 candidates, keep best), `--retries N`,
`--min-words N`.

## Backends

| Backend | When | Command bits |
|---|---|---|
| `ollama` (default) | Always try first. Free, unlimited, private | `--model qwen3:32b --bestof 5` |
| `copilot` | Best quality; ~4.5 Copilot AI credits per paragraph | `--backend copilot` |
| `moonshot` | Cheapest at volume; needs `MOONSHOT_API_KEY` in env | `--backend moonshot` |

Model choice on ollama: `qwen3:32b` is the benchmarked default (bold
restructuring). `muse-glimmer:30b-mlx` is the "gentle wash" (minimal drift,
faster). Do not pull other models without a bench run: `bench.py`.

## Hard rules

1. **Never wash working notes.** Only plain publishable paragraphs in
   drafts files are candidates; bold-lead blocks, lists, headers and
   code are already excluded. If in doubt, wash less.
2. **Never use a Claude model as the paraphrase engine** — that re-marks the
   text, defeating the entire purpose.
3. **Never commit the washed file over the original** without the user's
   explicit say-so. Default output is a sibling `.washed.md`.
4. **Never put `MOONSHOT_API_KEY` in a file.** Environment variable only.
5. Verify output before declaring success: no non-ASCII, no em/en-dashes, no
   AI tells (`delve|crucial|seamless|furthermore|moreover`), working notes
   byte-identical. The tool enforces most of this; you check the diff.

## After a run

Report: hunks accepted / guardrail-kept, which backend and model were used,
credit or time cost, and show 2-3 before/after paragraph pairs so the user can
judge voice. Tell the user to do a final read-aloud pass — it is part of the
pipeline and the last watermark-killer.

## House style (enforced automatically, but know it)

UK British English; software-developer register; two spaces after full stops;
semicolons preferred over dashes; em-dashes and en-dashes are forbidden;
straight ASCII quotes only; no AI-tell vocabulary.
