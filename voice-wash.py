#!/usr/bin/env python3
"""voice-wash: destroy statistical AI watermarks in markdown prose by paraphrasing.

Statistical watermarks (e.g. Claude's post-2-Aug-2026 model-level text marks) live in
token/word choice. Paraphrasing through a NON-watermarking model breaks the signal.
This script uses a local Ollama model: private (drafts never leave the machine) and
unwatermarked. A human reviews every hunk, because small local models are clumsy.

Usage:
  python3 voice-wash.py draft.md
  python3 voice-wash.py FILE --model qwen3:8b --yes --out out.md

  # code mode: wash comments across many files, gated by the repo's own tests
  python3 voice-wash.py --code --check "npm test" src/**/*.mjs

Code mode only ever rewrites COMMENT text (//, /* */, #) — never code. After each
file it runs --check CMD (e.g. the test suite) and auto-reverts the file on
failure, so a bad rewrite can never silently land. Run it on a clean git tree and
commit per file for bisectability. Identifiers and formatting are NOT touched
here: use the repo formatter and compiler-checked renames for those layers.

Blocks skipped (never rewritten): frontmatter, fenced code, headers, tables,
lists (including their indented continuation paragraphs), blockquotes,
link-only lines, tracker metadata lines (**Key:** value), and anything < 15 words.
Guardrail: every URL, number and capitalised token in the source paragraph must
survive verbatim in the rewrite, and the rewrite must be within 0.6-1.4x the
original length, or the hunk is rejected (retried, then the original is kept).

Model note: 1B-class models (llama3.2:1b, qwen3.5:0.8b) frequently fail the
guardrail on citation-dense paragraphs. For real use pull something with more
headroom, e.g. `ollama pull qwen3:8b` or `ollama pull llama3.1:8b`.
"""

import argparse
import difflib
import json
import os
import re
import sys
import unicodedata
import urllib.request
from collections import Counter

# Point at a remote Ollama server (e.g. a beefier machine on the LAN) with
#   export OLLAMA_HOST=http://ollama-host.local:11434
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/api/generate"
MIN_WORDS = 15

PROMPT = """Rewrite the passage below as natural human prose by a British software developer.

Voice rules:
- British spellings: colour, organise, behaviour, programme (unless quoting code).
- Sound like a developer writing plainly: direct, technical, no marketing gloss.
- Two spaces after every full stop.
- Prefer semicolons over dashes; use them wherever a dash parenthetical would go.
- NEVER use em-dashes or en-dashes anywhere, and avoid " - " hyphen parentheticals; use a semicolon or restructure the sentence.
- Contractions are fine. Plain words over grand ones.
- Vary sentence length; mix short punchy sentences with longer ones.
- NEVER use these AI tells: delve, furthermore, moreover, additionally, crucial, pivotal, vibrant, tapestry, testament, underscore, "it's worth noting", "it is important to note", "in today's", "not only ... but also", "plays a key role", "plays a vital role", "in conclusion", "navigate the complexities", "in the ever-evolving".
- No rhetorical questions, no rule-of-three lists, no "It's not X, it's Y" constructions.
- Keep the meaning identical. Keep every URL, number, date, name and technical term EXACTLY as written. Do not add or remove facts.
- Output ONLY the rewritten passage. No preamble, no commentary.

Structure rules (this is what separates human prose from a thesaurus pass):
- Do NOT mirror the source sentence boundaries. Merge short sentences, split long ones, reorder clauses.
- Use plain spoken idioms where they fit ("out of thin air", "falls over", "the floor is").
- If a sentence in your rewrite starts the same way as the source sentence, rewrite it again.

Two examples of the target voice:

SOURCE: Peer-assisted HLS is an established technique; viewers share segments over WebRTC so the origin feeds only the crowd's edge. It needs rendezvous infrastructure, because peers cannot find each other from nothing.
REWRITE: Peer-assisted HLS is a well-established technique.  Viewers share segments with each other over WebRTC, so the origin only has to feed the edge of the crowd.  It does need rendezvous infrastructure, since peers can't find each other out of thin air.

SOURCE: The platform cuts the stream, or the audience turns up in numbers and flattens it. From the streamer's side, success and censorship look identical.
REWRITE: The platform pulls the stream, or the audience shows up in force and flattens it instead.  From where the streamer sits, success and censorship look exactly the same.

Passage:
{text}"""

URL_RE = re.compile(r'https?://[^\s)>]+|`[^`]+`')
NUM_RE = re.compile(r'\$?\d{1,3}(?:,\d{3})+|\$?\d+(?:\.\d+)?(?:[%kMBh]|M|B)?')
PROPER_RE = re.compile(r'\b[A-Z][a-zA-Z0-9]{2,}(?:[- ][A-Z][a-zA-Z0-9]+)*\b')

# words that are protected when mid-sentence (almost certainly names) but not
# when they merely start a sentence
SENT_START_WORDS = {
    'Workers', 'Viewers', 'Relays', 'Peers', 'Segments', 'Clients', 'Users',
    'Streams', 'Nodes', 'Servers', 'Requests', 'Responses', 'Events', 'Messages',
    'Packets', 'Connections', 'Channels', 'Sessions', 'Tokens', 'Keys', 'Files',
}


def sentence_initial_words(text):
    return {m.group(1) for m in re.finditer(r'(?:^|[.!?]\s+)([A-Z][a-z]+)\b', text)}


# capitalised words that are ordinary English, not proper nouns
COMMON_CAP = {
    'The', 'This', 'That', 'These', 'Those', 'There', 'Their', 'They', 'Then',
    'When', 'Where', 'What', 'Which', 'Who', 'Why', 'How', 'Here', 'Take',
    'But', 'And', 'For', 'With', 'From', 'Into', 'After', 'Before', 'Because',
    'Viewers', 'People', 'Relays', 'Peers', 'Segments', 'On', 'In', 'At',
    'It', 'Its', 'We', 'You', 'He', 'She', 'If', 'So', 'No', 'Not', 'All',
    'Every', 'Each', 'One', 'Two', 'Three', 'First', 'Second', 'Third',
}


def split_blocks(text):
    """Split markdown into (washable, text) blocks, respecting fences/frontmatter."""
    lines = text.split('\n')
    blocks, cur, i = [], [], 0
    in_meta = False
    if lines and lines[0].strip() == '---':
        in_meta = True
        i = 1
        blocks.append((False, lines[0]))
    while i < len(lines):
        line = lines[i]
        if in_meta:
            blocks.append((False, line))
            if line.strip() == '---':
                in_meta = False
            i += 1
            continue
        if line.strip().startswith('```'):
            if cur:
                blocks.extend(_flush(cur))
                cur = []
            fence = [line]
            i += 1
            while i < len(lines):
                fence.append(lines[i])
                if lines[i].strip().startswith('```'):
                    i += 1
                    break
                i += 1
            blocks.append((False, '\n'.join(fence)))
            continue
        if line.strip() == '':
            if cur:
                blocks.extend(_flush(cur))
                cur = []
            blocks.append((False, ''))
        elif re.match(r'\s*(?:[-*+]|\d+[.)])\s', line):
            # list items start a new block even without a preceding blank line;
            # otherwise a continuation paragraph followed by list items becomes
            # one washable block and the model merges the items into prose
            if cur:
                blocks.extend(_flush(cur))
                cur = []
            cur.append(line)
        else:
            cur.append(line)
        i += 1
    if cur:
        blocks.extend(_flush(cur))
    return blocks


def _flush(lines):
    text = '\n'.join(lines)
    washable = _is_washable(text)
    return [(washable, text)]


def _is_washable(text):
    s = text.strip()
    if len(s.split()) < MIN_WORDS:
        return False
    if s.startswith(('#', '|', '```', '>')):
        return False
    # bold-lead blocks are tracker metadata or private working notes (status,
    # decisions, gates) in the working-notes convention — never wash or upload
    if s.startswith('**'):
        return False
    # list blocks are working notes in the drafts convention (Q&A,
    # checklists, gates) — publishable prose is plain paragraphs. First line
    # decides: list items here wrap onto indented continuation lines.
    if re.match(r'\s*(?:[-*+]|\d+[.)])\s', s):
        return False
    # indented first line = continuation paragraph of a list item (markdown
    # renders it as part of the item); not standalone prose, never wash
    if text[0] in ' \t':
        return False
    if re.fullmatch(r'\[.*\]\(.*\)', s):
        return False
    return True


def protected_tokens(text):
    urls = {m.group(0).rstrip('.,;:') for m in URL_RE.finditer(text)}
    # strip URLs before scanning numbers/proper nouns so URL fragments aren't double-counted
    rest = URL_RE.sub(' ', text)
    nums = {m.group(0) for m in NUM_RE.finditer(rest)}
    sentence_initial = sentence_initial_words(rest)
    props = set()
    for m in PROPER_RE.finditer(rest):
        t = m.group(0)
        if t.startswith('The '):
            t = t[4:]
        if not t or t in COMMON_CAP:
            continue
        # a generic word seen only at sentence starts isn't a name
        if t in SENT_START_WORDS:
            stripped = re.sub(r'(?:^|[.!?]\s+)' + re.escape(t) + r'\b', ' ', rest)
            if not re.search(r'\b' + re.escape(t) + r'\b', stripped):
                continue
        props.add(t)
    toks = urls | nums | props
    toks = {t for t in toks if len(t) > 1}
    # drop tokens that are substrings of other protected tokens
    return {t for t in toks if not any(t != o and t in o for o in toks)}


PROMPT_LEAK_WORDS = ('placeholder', 'opaque', 'unchanged', 'grammatical',
                     'passage', 'rewrite', 'rewritten', 'as an ai', 'zxq')

# common AI-writing tells (matched case-insensitively, word-boundaried)
AI_TELLS = re.compile(r'\b('
    r'delve|delving|furthermore|moreover|crucial|crucially|pivotal|vibrant|'
    r'tapestry|testament to|underscore[sd]?|showcas(?:e|ing)|boasts?|'
    r'ever-evolving|fast-paced|cutting-edge|state-of-the-art|game-chang\w+|'
    r'seamless(?:ly)?|robust(?:ly)?|leverag(?:e|ing)|harness(?:ing)?|'
    r'elevate[sd]?|empower(?:s|ing)?|unlock(?:s|ing)?|foster(?:s|ing)?'
    r')\b|'
    r"it['’]s worth noting|it is important to note|in today['’]s|"
    r'not only\b[^.!?]{1,80}\bbut also|plays? a (?:key|vital|crucial) role|'
    r'navigat\w+ the complex|in conclusion,', re.I)

# unambiguous American spellings (Oxford -ize is legitimate British, so excluded)
AMERICANISMS = {
    'color', 'colors', 'colored', 'coloring', 'favor', 'favors', 'favored',
    'favorite', 'behavior', 'behaviors', 'honor', 'honors', 'honored',
    'neighbor', 'neighbors', 'center', 'centers', 'centered', 'theater',
    'theaters', 'meter', 'meters', 'liter', 'liters', 'defense', 'offense',
    'traveling', 'traveled', 'traveler', 'travelers', 'canceled', 'canceling',
    'gray', 'grays', 'fulfill', 'fulfillment', 'enrollment', 'jewelry',
    'maneuver', 'maneuvers', 'pajamas', 'skeptic', 'skeptical',
    'labor', 'labors', 'rumor', 'rumors', 'humor', 'humors', 'odor', 'odors',
    'catalog', 'catalogs', 'plow', 'plows', 'aluminum', 'mom', 'math',
    'sidewalk', 'sidewalks', 'apartment', 'apartments', 'vacation', 'vacations',
    'gotten', 'zucchini', 'eggplant', 'sneakers', 'candy', 'drugstore',
}


def style_violations(dst, src=''):
    """AI tells and American spellings in dst that were not present in src."""
    problems = [m.group(0) for m in AI_TELLS.finditer(dst)
                if m.group(0).lower() not in src.lower()]
    words = re.findall(r"[a-z]+", dst.lower())
    src_words = set(re.findall(r"[a-z]+", src.lower()))
    problems += [w for w in words if w in AMERICANISMS and w not in src_words]
    return problems


# house style: semicolons instead of dashes. Spaced and unspaced em/en-dashes
# become "; "; so do spaced-hyphen parentheticals (" - " mid-sentence), while
# hyphens inside words/ranges (M1-M3, 0.75-0.82s) and line-leading list
# markers are left alone.
DASH_RE = re.compile(r'\s*[—–―−]\s*')
SPACED_HYPHEN_RE = re.compile(r'(?<=\S) - (?=\S)')
# sentence-final punctuation followed by space(s); the lookbehind keeps the
# final dot of an ellipsis ("... next") from being treated as a sentence end
SENTENCE_END_RE = re.compile(r'(?<![.!?])([.!?]) +(?=\S)')


def reflow_like(text, source):
    """Hard-wrap washed text to match the source block's house style: same max
    line width as the source, continuation lines indented two spaces."""
    import textwrap
    lines = [l for l in source.split('\n') if l.strip()]
    width = max((len(l) for l in lines), default=0)
    if width < 60:  # source wasn't hard-wrapped
        return text
    width = min(width, 120)
    flat = ' '.join(text.split())
    out = textwrap.wrap(flat, width=width, initial_indent='',
                        subsequent_indent='  ', break_long_words=False,
                        break_on_hyphens=False)
    return '\n'.join(out)


def normalize_style(text):
    """Author's house style, applied deterministically so it never depends on
    the model obeying: semicolons instead of dashes/hyphen parentheticals,
    two spaces after sentence ends."""
    text = DASH_RE.sub('; ', text)
    text = SPACED_HYPHEN_RE.sub('; ', text)
    text = SENTENCE_END_RE.sub(r'\1  ', text)
    # ASCII only: straight quotes/apostrophes (the drafts are 100% ASCII)
    for a, b in (('’', "'"), ('‘', "'"), ('“', '"'), ('”', '"')):
        text = text.replace(a, b)
    return text


# --- Layer A: invisible-Unicode scrub --------------------------------------
# Statistical watermarks are the headline threat, but edit-based schemes hide
# signals in invisible/format characters too: zero-width spaces and joiners,
# bidi overrides, tag characters, variation selectors, exotic space
# homoglyphs. These codepoints are all standard Unicode (categories Cf/Mn);
# none of them belong in ASCII prose. --scrub removes or normalises them in a
# deterministic pre-pass before any model is involved.

# invisible format controls (Unicode category Cf and friends)
SCRUB_STRIP = frozenset({
    0x00AD,                        # soft hyphen
    0x034F,                        # combining grapheme joiner
    0x061C,                        # Arabic letter mark
    0x115F, 0x1160,                # Hangul fillers
    0x17B4, 0x17B5,                # Khmer vowel inherents
    0x180B, 0x180C, 0x180D,        # Mongolian free variation selectors
    0x180E,                        # Mongolian vowel separator
    0x200B, 0x200C, 0x200D,        # ZWSP, ZWNJ, ZWJ
    0x200E, 0x200F,                # LRM, RLM
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # bidi embedding/override
    0x2060, 0x2061, 0x2062, 0x2063, 0x2064,  # word joiner, invisible operators
    0x2066, 0x2067, 0x2068, 0x2069,          # bidi isolates
    0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F,  # deprecated format chars
    0xFEFF,                        # BOM / zero-width no-break space
    *range(0xFE00, 0xFE10),        # variation selectors 1-16
    0xFFF9, 0xFFFA, 0xFFFB,        # interlinear annotations
})

# spaces that render like U+0020 but aren't
SCRUB_SPACES = {
    0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000,
}


def _is_scrub_strip(cp):
    return (cp in SCRUB_STRIP
            or 0xE0001 <= cp <= 0xE007F      # tag characters
            or 0xE0100 <= cp <= 0xE01EF)     # variation selectors 17-256


def scrub_unicode(text):
    """Strip invisible Unicode carriers and normalise exotic spaces.
    Returns (cleaned_text, report_lines). Pure-ASCII input passes through
    byte-identical with an empty report."""
    removed, replaced = Counter(), Counter()
    out = []
    for ch in text:
        cp = ord(ch)
        if _is_scrub_strip(cp) or unicodedata.category(ch) == 'Cf':
            removed[f"U+{cp:04X} {unicodedata.name(ch, '<unnamed>')}"] += 1
        elif cp in SCRUB_SPACES:
            replaced[f"U+{cp:04X} {unicodedata.name(ch, '<unnamed>')}"] += 1
            out.append(' ')
        else:
            out.append(ch)
    report = [f"  scrub: stripped {n}x {label}" for label, n in sorted(removed.items())]
    report += [f"  scrub: normalised {n}x {label} -> space"
               for label, n in sorted(replaced.items())]
    return ''.join(out), report


def check_guardrail(src, dst, max_ratio=1.4):
    if not dst:
        return ['<empty output>']
    norm = lambda s: re.sub(r'\s+', ' ', s).strip()
    if norm(dst) == norm(src):
        return ['<verbatim copy — no wash>']
    missing = [t for t in protected_tokens(src) if t not in dst]
    ratio = len(dst.split()) / max(1, len(src.split()))
    if not 0.6 <= ratio <= max_ratio:
        missing.append(f'<length ratio {ratio:.2f} outside 0.6-{max_ratio}>')
    low_src, low_dst = src.lower(), dst.lower()
    leaked = [w for w in PROMPT_LEAK_WORDS if w in low_dst and w not in low_src]
    if leaked:
        missing.append(f'<prompt leak: {leaked}>')
    style = style_violations(dst, src)
    if style:
        missing.append(f'<style: {sorted(set(style))[:6]}>')
    return missing


def mask_tokens(text):
    """Replace protected tokens with placeholders small models can carry through."""
    mapping = {}
    masked = text
    for i, tok in enumerate(sorted(protected_tokens(text), key=len, reverse=True)):
        ph = f"ZXQ{i:03d}QXZ"
        if tok in masked:
            masked = masked.replace(tok, ph)
            mapping[ph] = tok
    return masked, mapping


def unmask_tokens(text, mapping):
    for ph, tok in mapping.items():
        text = text.replace(ph, tok)
    return text


def _paraphrase_ollama(masked, model, budget, prompt=None, temperature=None):
    # some models burn tokens on priming before emitting; escalate the budget
    # until we get a real stop, up to 4 attempts
    for attempt in range(4):
        payload = json.dumps({
            "model": model,
            "prompt": (prompt or PROMPT).format(text=masked),
            "stream": False,
            "think": False,
            "options": {"temperature": 0.4 if temperature is None else temperature,
                        "num_predict": budget},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.loads(r.read())
        if resp.get("done_reason") != "length":
            return resp["response"].strip()
        if resp["response"].strip():
            return ""  # truncated mid-text — treat as guardrail failure
        budget *= 3
    return ""


def _paraphrase_copilot(masked, model, prompt=None):
    """Kimi K3 (or any configured model) via the Copilot CLI: not an Anthropic
    model, so no EU-AI-Act Claude watermark; far stronger than local 1-8B."""
    import subprocess
    cmd = ["copilot", "-p", (prompt or PROMPT).format(text=masked), "--deny-tool", "-s"]
    if model:
        cmd += ["--model", model]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"copilot failed: {r.stderr[:200]}")
    return r.stdout.strip()


def _paraphrase_moonshot(masked, model, budget, prompt=None):
    """Direct Moonshot/Kimi API. Key from https://platform.kimi.ai (API keys page),
    passed via MOONSHOT_API_KEY env var — never hardcode it. Base URL overridable
    via MOONSHOT_BASE_URL (used by tests)."""
    import os
    key = os.environ.get('MOONSHOT_API_KEY')
    if not key:
        raise RuntimeError("MOONSHOT_API_KEY not set — create a key at https://platform.kimi.ai")
    base = os.environ.get('MOONSHOT_BASE_URL', 'https://api.moonshot.ai/v1')
    payload = json.dumps({
        "model": model or "kimi-k3",
        "messages": [{"role": "user", "content": (prompt or PROMPT).format(text=masked)}],
        "temperature": 0.4,
        "max_tokens": budget * 2,  # headroom: reasoning tokens count too
        "reasoning_effort": "low",  # paraphrase needs prose skill, not deep thought
    }).encode()
    req = urllib.request.Request(
        base.rstrip('/') + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.loads(r.read())
    choice = resp["choices"][0]
    if choice.get("finish_reason") == "length":
        return ""  # truncated — guardrail failure
    return (choice["message"].get("content") or "").strip()


POLISH_PROMPT = """You are a subeditor with a red pen.  The passage below is a machine paraphrase.  Fix anything that sounds translated, flat, stiff or machine-written: grammar slips, wrong articles, lifeless word order.  Keep every fact, URL, number, name and technical term EXACTLY.  British English, software developer's voice, two spaces after full stops, semicolons preferred over dashes, no em-dashes, no AI-tell phrases.  Output ONLY the polished passage.

Passage:
{text}"""


def _polish(text, model, backend):
    masked, mapping = mask_tokens(text)
    if backend == 'copilot':
        out = _paraphrase_copilot(masked, model, prompt=POLISH_PROMPT)
    elif backend == 'moonshot':
        out = _paraphrase_moonshot(masked, model, len(text.split()) * 4 + 40,
                                   prompt=POLISH_PROMPT)
    else:
        out = _paraphrase_ollama(masked, model, len(text.split()) * 4 + 40,
                                 prompt=POLISH_PROMPT)
    return unmask_tokens(out, mapping)


def trigram_novelty(src, dst):
    """1 - Jaccard overlap of word trigrams: how far the rewrite moved from the
    source's phrasing. 0 = identical wording, 1 = no shared trigrams."""
    def tri(s):
        w = re.findall(r"[a-z']+", s.lower())
        return {tuple(w[i:i+3]) for i in range(len(w) - 2)} or {tuple(w)}
    a, b = tri(src), tri(dst)
    return 1 - len(a & b) / len(a | b)


def rank_candidates(src, cands):
    """Heuristic ranking for best-of-K: reward structural novelty and house-style
    markers, penalise length drift. Guardrail failures are filtered by caller."""
    best, best_score = None, -1e9
    for c in cands:
        score = 2.0 * trigram_novelty(src, c)
        score -= abs(len(c.split()) / max(1, len(src.split())) - 1.0)
        score += 0.1 * c.count(';')
        best, best_score = (c, score) if score > best_score else (best, best_score)
    return best


LOCAL_JUDGE_PROMPT = """Score this paraphrase 1-10 for sounding like a British software developer wrote it naturally. Criteria: sentence rhythm variety, plain direct register, no AI-tell phrasing, no grammar slips, meaning preserved. Reply with ONLY the integer.

Source: {src}

Paraphrase: {dst}"""


def rank_candidates_judged(src, cands, model):
    """Rank best-of-K candidates using the local model as judge (free on ollama).
    Falls back to heuristic ranking for any candidate the judge won't score."""
    best, best_score = None, -1
    for c in cands:
        payload = json.dumps({
            "model": model,
            "prompt": LOCAL_JUDGE_PROMPT.format(src=src, dst=c),
            "stream": False, "think": False,
            "options": {"temperature": 0, "num_predict": 8},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read())
            m = re.search(r'\d+', resp["response"])
            score = int(m.group(0)) if m else -1
        except Exception:
            score = -1
        if score > best_score:
            best, best_score = c, score
    return best if best_score > 0 else rank_candidates(src, cands)


_backend_checked = False


def check_backend_available(backend, model):
    """Fail fast with an actionable message instead of per-hunk HTTP tracebacks.
    Called lazily on the first paraphrase so model-free runs (e.g. a file with
    no washable paragraphs, or --scrub-only cleanups) never demand a backend."""
    if backend == 'ollama':
        base = OLLAMA_URL.rsplit('/api/', 1)[0]
        try:
            with urllib.request.urlopen(base + '/api/tags', timeout=5) as r:
                tags = {m.get('name', '') for m in json.loads(r.read()).get('models', [])}
        except Exception:
            return (f"cannot reach ollama at {base} — start it (`ollama serve`) or "
                    "set OLLAMA_HOST, or use --backend copilot / --backend moonshot")
        if model and not any(t == model or t == model + ':latest' for t in tags):
            return f"model '{model}' not pulled — run: ollama pull {model}"
    elif backend == 'copilot':
        import shutil
        if not shutil.which('copilot'):
            return "copilot CLI not found — install GitHub Copilot CLI or use --backend ollama"
    elif backend == 'moonshot':
        if not os.environ.get('MOONSHOT_API_KEY'):
            return "MOONSHOT_API_KEY not set — create a key at https://platform.kimi.ai"
    return None


def _generate_once(masked, model, budget, backend, temperature=None, prompt=None):
    if backend == 'copilot':
        return _paraphrase_copilot(masked, model, prompt=prompt)
    if backend == 'moonshot':
        return _paraphrase_moonshot(masked, model, budget, prompt=prompt)
    return _paraphrase_ollama(masked, model, budget, prompt=prompt, temperature=temperature)


VERIFY_PROMPT = """Passage A:
{a}

Passage B:
{b}

Does Passage B state exactly the same facts as Passage A: same meaning, nothing added, nothing omitted, no claim reversed or inverted? Answer YES or NO and nothing else."""


def verify_meaning(src, dst, model, backend):
    """Entailment check: the backend judges whether dst preserves src's meaning.
    A wrong answer drops the candidate (guardrail failure); a backend ERROR keeps
    it (availability beats caution — the token/length/style guardrails still ran).
    """
    q = VERIFY_PROMPT.format(a=src, b=dst)
    try:
        out = _generate_once(q, model, 8, backend, prompt='{text}')
    except Exception as e:
        print(f"  verify error (keeping candidate): {e}", file=sys.stderr)
        return True
    m = re.search(r'\b(yes|no)\b', out.strip().lower())
    if not m:
        print(f"  verify unparseable ({out.strip()[:40]!r}) — dropping candidate",
              file=sys.stderr)
        return False
    return m.group(1) == 'yes'


def paraphrase(text, model, max_ratio=1.4, backend='ollama', polish=False, bestof=1,
               judge_rank=False, verify=False):
    global _backend_checked
    if not _backend_checked:
        _backend_checked = True
        problem = check_backend_available(backend, model)
        if problem:
            raise RuntimeError(problem)
    masked, mapping = mask_tokens(text)
    budget = int(len(masked.split()) * max_ratio * 1.6) + 40
    temps = [None] if bestof <= 1 else [0.4, 0.7, 0.9, 1.0, 0.55][:bestof]
    cands = []
    for t in temps:
        out = _generate_once(masked, model, budget, backend, temperature=t)
        if out.startswith('"') and out.endswith('"'):
            out = out[1:-1]
        out = unmask_tokens(out, mapping)
        # small models sometimes echo the passage verbatim then ramble: cut there
        norm = re.sub(r'\s+', ' ', text).strip()
        norm_out = re.sub(r'\s+', ' ', out)
        pos = norm_out.find(norm)
        if pos >= 0:
            end = pos + len(norm)
            suffix = norm_out[end:].strip()
            if len(suffix.split()) > 5:
                out = norm_out[:end]
        out = normalize_style(out)
        if out and not check_guardrail(text, out, max_ratio=max_ratio):
            cands.append(out)
    if verify:
        cands = [c for c in cands if verify_meaning(text, c, model, backend)]
    if not cands:
        out = ''
    elif judge_rank and backend == 'ollama' and len(cands) > 1:
        out = rank_candidates_judged(text, cands, model)
    else:
        out = rank_candidates(text, cands)
    if polish and out:
        cand = normalize_style(_polish(out, model, backend))
        if cand and not check_guardrail(text, cand, max_ratio=max_ratio):
            out = cand
    return out


# --- code mode: comment extraction ----------------------------------------

LINE_COMMENT_RE = {
    '.py': re.compile(r'^(?P<indent>[ \t]*)#(?:!/)?[ \t]?(?P<body>.*)$'),
    '.js': re.compile(r'^(?P<indent>[ \t]*)//[ \t]?(?P<body>.*)$'),
    '.mjs': re.compile(r'^(?P<indent>[ \t]*)//[ \t]?(?P<body>.*)$'),
    '.ts': re.compile(r'^(?P<indent>[ \t]*)//[ \t]?(?P<body>.*)$'),
    '.go': re.compile(r'^(?P<indent>[ \t]*)//[ \t]?(?P<body>.*)$'),
    '.rs': re.compile(r'^(?P<indent>[ \t]*)//[ /!]?[ \t]?(?P<body>.*)$'),
}
BLOCK_COMMENT_RE = re.compile(r'/\*(?P<body>.*?)\*/', re.S)
PY_DOCSTRING_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?P<q>"""|\'\'\')(?P<body>.*?)(?P=q)[ \t]*$',
    re.S | re.M)
CODE_MIN_WORDS = 8


def extract_comment_blocks(text, ext):
    """Return list of (start, end, rebuild_fn, comment_text) spans to wash.

    Only whole-line comments are considered (inline trailing comments left alone).
    rebuild_fn(new_text) returns the replacement for the span.
    """
    spans = []
    line_re = LINE_COMMENT_RE.get(ext)
    if line_re:
        lines = text.split('\n')
        run, run_start, offset = [], None, 0
        for idx, line in enumerate(lines):
            m = line_re.match(line)
            if m and m.group('body').strip():
                if run_start is None:
                    run_start = offset
                run.append((m.group('indent'), m.group('body'), line))
            else:
                if run:
                    spans.append(_line_span(run, run_start, offset, text))
                    run, run_start = [], None
            offset += len(line) + 1
        if run:
            spans.append(_line_span(run, run_start, offset, text))
    if ext in ('.js', '.mjs', '.ts', '.go', '.rs'):
        for m in BLOCK_COMMENT_RE.finditer(text):
            body = m.group('body')
            # skip if overlapping an existing line-comment span
            if any(s <= m.start() < e for s, e, _, _ in spans):
                continue
            prefix = text[m.start():m.end()]
            spans.append((m.start(), m.end(),
                          lambda new, p=prefix: _rebuild_block(p, new),
                          _block_body_text(body)))
    if ext == '.py':
        for m in PY_DOCSTRING_RE.finditer(text):
            if any(s <= m.start() < e for s, e, _, _ in spans):
                continue
            indent, q = m.group('indent'), m.group('q')
            body = m.group('body').strip()
            if not body or len(body.split()) < 4:
                continue

            def _rebuild_doc(new, i=indent, q=q):
                inner = '\n'.join(f"{i}  {l}" for l in new.split('\n'))
                return f"{i}{q}\n{inner}\n{i}{q}"

            spans.append((m.start(), m.end(), _rebuild_doc, body))
    spans.sort(key=lambda s: s[0])
    return spans


def _line_span(run, start, end, full_text):
    indent = run[0][0]
    text = '\n'.join(body for _, body, _ in run)
    prefix = '#' if run[0][2].lstrip().startswith('#') else '//'
    raw = full_text[start:end]
    trailing = raw[len(raw.rstrip('\n')):]  # preserve newline(s) after the run

    def rebuild(new):
        return '\n'.join(f"{indent}{prefix} {l}".rstrip() for l in new.split('\n')) + trailing

    return (start, end, rebuild, text)


def _block_body_text(body):
    lines = [re.sub(r'^\s*\* ?', '', l) for l in body.split('\n')]
    return '\n'.join(lines).strip()


def _rebuild_block(orig, new):
    lines = new.split('\n')
    if '\n' not in orig:
        return f"/* {' '.join(l.strip() for l in lines)} */"
    # multi-line block: keep /* and */ exactly where they were
    inner = '\n'.join(' * ' + l.strip() for l in lines)
    return f"/*\n{inner}\n */"


def wash_code_file(path, model, retries, yes, check_cmd, min_words, backend='ollama', polish=False, bestof=1, verify=False):
    """Wash comment blocks in one source file. Returns 'washed'|'unchanged'|'reverted'."""
    ext = '.' + path.rsplit('.', 1)[-1] if '.' in path else ''
    src = open(path).read()
    spans = extract_comment_blocks(src, ext)
    spans = [s for s in spans if len(s[3].split()) >= min_words]
    if not spans:
        return 'unchanged'
    out, last = [], 0
    for start, end, rebuild, ctext in spans:
        dst = None
        for attempt in range(retries):
            try:
                cand = paraphrase(ctext, model, max_ratio=2.0, backend=backend, polish=polish, bestof=bestof, verify=verify)
            except Exception as e:
                print(f"  paraphrase error: {e}", file=sys.stderr)
                break
            # comments tolerate more drift than citations: length band 2.0x
            missing = check_guardrail(ctext, cand, max_ratio=2.0)
            sentences = [s.strip() for s in re.split(r'[.!?]', cand) if s.strip()]
            if len(sentences) != len(set(sentences)):
                missing.append('<repeated sentences>')
            if not missing:
                dst = cand
                break
            print(f"  guardrail @ char {start}: {missing[:4]}"
                  f"{' — retrying' if attempt < retries - 1 else ' — keeping original'}",
                  file=sys.stderr)
        if dst is not None and not yes:
            print(f"\n--- {path} comment @ char {start} ---")
            print(word_diff(ctext, dst))
            c = input("[y] accept / [n] keep > ").strip().lower()
            if c != 'y':
                dst = None
        out.append(src[last:start])
        out.append(rebuild(dst) if dst is not None else src[start:end])
        last = end
    out.append(src[last:])
    new = ''.join(out)
    if new == src:
        return 'unchanged'
    open(path, 'w').write(new)
    if check_cmd:
        import subprocess
        r = subprocess.run(check_cmd, shell=True, capture_output=True)
        if r.returncode != 0:
            open(path, 'w').write(src)
            print(f"  CHECK FAILED for {path} — reverted", file=sys.stderr)
            return 'reverted'
    return 'washed'


def word_diff(a, b):
    sm = difflib.SequenceMatcher(None, a.split(), b.split())
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            out.append(' '.join(a.split()[i1:i2]))
        elif op == 'delete':
            out.append('[-' + ' '.join(a.split()[i1:i2]) + '-]')
        elif op == 'insert':
            out.append('{+' + ' '.join(b.split()[j1:j2]) + '+}')
        else:
            out.append('[-' + ' '.join(a.split()[i1:i2]) + '-]{+' +
                       ' '.join(b.split()[j1:j2]) + '+}')
    return ' '.join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('files', nargs='+', help='markdown file, or source files/globs with --code')
    ap.add_argument('--code', action='store_true',
                    help='code mode: wash comments only, editing files in place')
    ap.add_argument('--check', help='command run after each file in code mode; file reverted on failure')
    ap.add_argument('--backend', choices=['ollama', 'copilot', 'moonshot'], default='ollama',
                    help="ollama = fully local; copilot = via Copilot CLI (uses AI credits); "
                         "moonshot = direct Kimi API, cheapest at volume (MOONSHOT_API_KEY)")
    ap.add_argument('--model', default=None,
                    help='ollama: e.g. qwen3:8b (default llama3.2:1b); copilot: optional '
                         'model id; moonshot: default kimi-k3')
    ap.add_argument('--yes', action='store_true', help='accept all hunks without prompting')
    ap.add_argument('--out', help='output path (default: <file>.washed.md; markdown mode only)')
    ap.add_argument('--in-place', action='store_true')
    ap.add_argument('--min-words', type=int, default=None)
    ap.add_argument('--polish', action='store_true',
                    help='second subeditor pass per hunk (bench: lowers judge score — off by default)')
    ap.add_argument('--bestof', type=int, default=1,
                    help='generate K candidates at varied temperatures, keep the best (bench: best config)')
    ap.add_argument('--retries', type=int, default=3,
                    help='paraphrase attempts per paragraph before keeping original')
    ap.add_argument('--scrub', action='store_true',
                    help='deterministic pre-pass: strip invisible Unicode carriers '
                         '(zero-width chars, bidi controls, tag chars, variation '
                         'selectors) and normalise exotic spaces before washing')
    ap.add_argument('--verify', dest='verify', action='store_true', default=None,
                    help='backend double-checks each candidate preserves meaning '
                         '(default: on for ollama — free; off for paid backends)')
    ap.add_argument('--no-verify', dest='verify', action='store_false')
    args = ap.parse_args()

    if args.model is None:
        args.model = 'llama3.2:1b' if args.backend == 'ollama' else ''

    if args.min_words is not None:
        global MIN_WORDS
        MIN_WORDS = args.min_words

    verify = args.verify if args.verify is not None else args.backend == 'ollama'

    if args.code:
        import glob as globmod
        supported = set(LINE_COMMENT_RE) | {'.c', '.h', '.cpp', '.java', '.sh'}
        paths = []
        for f in args.files:
            for p in (globmod.glob(f, recursive=True) or [f]):
                if os.path.isfile(p) and os.path.splitext(p)[1] in supported:
                    paths.append(p)
                elif os.path.isfile(p):
                    print(f"skipping {p}: unsupported extension", file=sys.stderr)
        stats = {'washed': 0, 'unchanged': 0, 'reverted': 0}
        for p in paths:
            print(f"washing {p} ...", file=sys.stderr)
            if args.scrub:
                raw = open(p).read()
                cleaned, report = scrub_unicode(raw)
                if report:
                    open(p, 'w').write(cleaned)
                    print('\n'.join(report), file=sys.stderr)
            stats[wash_code_file(p, args.model, args.retries, args.yes,
                                 args.check, args.min_words or CODE_MIN_WORDS,
                                 args.backend, args.polish, args.bestof, verify)] += 1
        print(f"\n{stats}", file=sys.stderr)
        return

    src = open(args.files[0]).read()
    if args.scrub:
        src, report = scrub_unicode(src)
        if report:
            print('\n'.join(report), file=sys.stderr)
        else:
            print("scrub: no invisible Unicode found", file=sys.stderr)
    blocks = split_blocks(src)
    washable = [i for i, (w, _) in enumerate(blocks) if w]
    print(f"{len(blocks)} blocks, {len(washable)} washable paragraphs", file=sys.stderr)

    accepted = rejected = kept = 0
    for n, i in enumerate(washable, 1):
        orig = blocks[i][1]
        dst = None
        for attempt in range(args.retries):
            try:
                cand = paraphrase(orig, args.model, backend=args.backend, polish=args.polish, bestof=args.bestof, verify=verify)
            except Exception as e:
                print(f"[{n}/{len(washable)}] paraphrase error: {e}", file=sys.stderr)
                break
            missing = check_guardrail(orig, cand)
            if not missing:
                dst = reflow_like(cand, orig)  # match the file's hard-wrap style
                break
            retrying = attempt < args.retries - 1
            print(f"[{n}/{len(washable)}] guardrail: dropped {missing[:6]}"
                  f"{' — retrying' if retrying else ' — keeping original'}",
                  file=sys.stderr)
        if dst is None:
            kept += 1
            continue
        if args.yes:
            blocks[i] = (False, dst)
            accepted += 1
            continue
        print(f"\n===== hunk {n}/{len(washable)} =====")
        print(word_diff(orig, dst))
        while True:
            c = input("[y] accept / [n] keep original / [e] edit in $EDITOR / [q] save & quit > ").strip().lower()
            if c in ('y', 'n', 'e', 'q'):
                break
        if c == 'y':
            blocks[i] = (False, dst)
            accepted += 1
        elif c == 'n':
            rejected += 1
        elif c == 'e':
            import subprocess, tempfile
            with tempfile.NamedTemporaryFile('w+', suffix='.md', delete=False) as tf:
                tf.write(dst)
                path = tf.name
            subprocess.run([os.environ.get('EDITOR', 'vi'), path])
            blocks[i] = (False, open(path).read().strip())
            os.unlink(path)
            accepted += 1
        else:
            print("Saving and quitting.", file=sys.stderr)
            break

    out = '\n'.join(t for _, t in blocks)
    if args.in_place:
        open(args.files[0], 'w').write(out)
        dest = args.files[0]
    else:
        stem, dot, ext = args.files[0].rpartition('.')
        # never let the default output equal the input (e.g. an extensionless file)
        default = f"{stem}.washed.{ext}" if dot else args.files[0] + '.washed'
        dest = args.out or default
        if os.path.abspath(dest) == os.path.abspath(args.files[0]):
            print(f"refusing to overwrite {dest}: pass --in-place if you mean it",
                  file=sys.stderr)
            return 1
        open(dest, 'w').write(out)
    print(f"\naccepted={accepted} rejected={rejected} guardrail-kept={kept}", file=sys.stderr)
    print(f"wrote {dest}", file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main() or 0)
