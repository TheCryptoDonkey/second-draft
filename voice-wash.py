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
import re
import sys
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/generate"
MIN_WORDS = 15

PROMPT = """Rewrite the following passage so it reads as natural human prose in British English, first person where the original is first person. Vary sentence length and structure; do not start consecutive sentences the same way. Keep the meaning identical. Keep every URL, number, date, name and technical term EXACTLY as written. Do not add or remove facts. Output ONLY the rewritten passage, no preamble.

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
    if s.startswith('**') and ':**' in s.split('\n')[0]:
        return False  # tracker metadata lines
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


def check_guardrail(src, dst, max_ratio=1.4):
    missing = [t for t in protected_tokens(src) if t not in dst]
    ratio = len(dst.split()) / max(1, len(src.split()))
    if not 0.6 <= ratio <= max_ratio:
        missing.append(f'<length ratio {ratio:.2f} outside 0.6-{max_ratio}>')
    low_src, low_dst = src.lower(), dst.lower()
    leaked = [w for w in PROMPT_LEAK_WORDS if w in low_dst and w not in low_src]
    if leaked:
        missing.append(f'<prompt leak: {leaked}>')
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


def paraphrase(text, model, temperature=0.4, max_ratio=1.4):
    masked, mapping = mask_tokens(text)
    note = ("\n\nTokens like ZXQ000QXZ are opaque placeholders: copy EVERY one through "
            "UNCHANGED, in the same position's grammatical slot. Never alter, "
            "translate, drop or duplicate one. Output must be roughly the same "
            "length as the input.") if mapping else ""
    budget = int(len(masked.split()) * max_ratio * 1.6) + 40
    payload = json.dumps({
        "model": model,
        "prompt": PROMPT.format(text=masked) + note,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "num_predict": budget},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.loads(r.read())
    out = resp["response"].strip()
    if resp.get("done_reason") == "length":
        return ""  # truncated by token budget — treat as guardrail failure
    if out.startswith('"') and out.endswith('"'):
        out = out[1:-1]
    return unmask_tokens(out, mapping)


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


def wash_code_file(path, model, retries, yes, check_cmd, min_words):
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
                cand = paraphrase(ctext, model)
            except Exception as e:
                print(f"  ollama error: {e}", file=sys.stderr)
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
    ap.add_argument('--model', default='llama3.2:1b')
    ap.add_argument('--yes', action='store_true', help='accept all hunks without prompting')
    ap.add_argument('--out', help='output path (default: <file>.washed.md; markdown mode only)')
    ap.add_argument('--in-place', action='store_true')
    ap.add_argument('--min-words', type=int, default=None)
    ap.add_argument('--retries', type=int, default=3,
                    help='paraphrase attempts per paragraph before keeping original')
    args = ap.parse_args()

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
                                 args.check, args.min_words or CODE_MIN_WORDS)] += 1
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
                cand = paraphrase(orig, args.model)
            except Exception as e:
                print(f"[{n}/{len(washable)}] ollama error: {e}", file=sys.stderr)
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
