#!/usr/bin/env python3
"""voice-wash: destroy statistical AI watermarks in markdown prose by paraphrasing.

Statistical watermarks (e.g. Claude's post-2-Aug-2026 model-level text marks) live in
token/word choice. Paraphrasing through a NON-watermarking model breaks the signal.
This script uses a local Ollama model: private (drafts never leave the machine) and
unwatermarked. A human reviews every hunk, because small local models are clumsy.

Usage:
  python3 scripts/voice-wash.py grants/drafts/G36-hrf-relayswarm.md
  python3 scripts/voice-wash.py FILE --model qwen3.5:0.8b --yes --out out.md

  # code mode: wash comments across many files, gated by the repo's own tests
  python3 scripts/voice-wash.py --code --check "npm test" src/**/*.mjs

Code mode only ever rewrites COMMENT text (//, /* */, #) — never code. After each
file it runs --check CMD (e.g. the test suite) and auto-reverts the file on
failure, so a bad rewrite can never silently land. Run it on a clean git tree and
commit per file for bisectability. Identifiers and formatting are NOT touched
here: use the repo formatter and compiler-checked renames for those layers.

Blocks skipped (never rewritten): frontmatter, fenced code, headers, tables,
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
import urllib.request

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
    # decisions, gates) in the grants/drafts convention — never wash or upload
    if s.startswith('**'):
        return False
    # list blocks are working notes in the grants/drafts convention (Q&A,
    # checklists, gates) — submission prose is plain paragraphs. First line
    # decides: list items here wrap onto indented continuation lines.
    if re.match(r'\s*(?:[-*]|\d+[.)])\s', s):
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


def _paraphrase_ollama(masked, model, budget, prompt=None):
    payload = json.dumps({
        "model": model,
        "prompt": (prompt or PROMPT).format(text=masked),
        "stream": False,
        "think": False,
        "options": {"temperature": 0.4, "num_predict": budget},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.loads(r.read())
    if resp.get("done_reason") == "length":
        return ""  # truncated by token budget — treat as guardrail failure
    return resp["response"].strip()


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


def paraphrase(text, model, max_ratio=1.4, backend='ollama', polish=False):
    masked, mapping = mask_tokens(text)
    budget = int(len(masked.split()) * max_ratio * 1.6) + 40
    if backend == 'copilot':
        out = _paraphrase_copilot(masked, model)
    elif backend == 'moonshot':
        out = _paraphrase_moonshot(masked, model, budget)
    else:
        out = _paraphrase_ollama(masked, model, budget)
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


def wash_code_file(path, model, retries, yes, check_cmd, min_words, backend='ollama', polish=False):
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
                cand = paraphrase(ctext, model, max_ratio=2.0, backend=backend, polish=polish)
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
                    help='second subeditor pass per hunk (slower, better voice)')
    ap.add_argument('--retries', type=int, default=3,
                    help='paraphrase attempts per paragraph before keeping original')
    args = ap.parse_args()

    if args.model is None:
        args.model = 'llama3.2:1b' if args.backend == 'ollama' else ''

    if args.min_words is not None:
        global MIN_WORDS
        MIN_WORDS = args.min_words

    if args.code:
        import glob as globmod, os
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
            stats[wash_code_file(p, args.model, args.retries, args.yes,
                                 args.check, args.min_words or CODE_MIN_WORDS,
                                 args.backend, args.polish)] += 1
        print(f"\n{stats}", file=sys.stderr)
        return

    src = open(args.files[0]).read()
    blocks = split_blocks(src)
    washable = [i for i, (w, _) in enumerate(blocks) if w]
    print(f"{len(blocks)} blocks, {len(washable)} washable paragraphs", file=sys.stderr)

    accepted = rejected = kept = 0
    for n, i in enumerate(washable, 1):
        orig = blocks[i][1]
        dst = None
        for attempt in range(args.retries):
            try:
                cand = paraphrase(orig, args.model, backend=args.backend, polish=args.polish)
            except Exception as e:
                print(f"[{n}/{len(washable)}] paraphrase error: {e}", file=sys.stderr)
                break
            missing = check_guardrail(orig, cand)
            if not missing:
                dst = cand
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
            import os, subprocess, tempfile
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
        dest = args.out or args.files[0].replace('.md', '.washed.md')
        open(dest, 'w').write(out)
    print(f"\naccepted={accepted} rejected={rejected} guardrail-kept={kept}", file=sys.stderr)
    print(f"wrote {dest}", file=sys.stderr)


if __name__ == '__main__':
    main()
