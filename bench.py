#!/usr/bin/env python3
"""bench: measure how close a wash config gets to K3-quality, objectively.

Runs a fixed set of real grant paragraphs through candidate configurations,
then scores each output on:
  - guardrail pass (facts/citations/style rules)        [hard gate]
  - novelty: 1 - trigram Jaccard vs source              [restructuring boldness]
  - judge: K3 (via copilot backend) scores voice 1-10   [the target itself]

Usage:
  OLLAMA_HOST=http://ollama-host.local:11434 python3 bench.py
  python3 bench.py --no-judge          # skip K3 scoring (saves credits)
"""

import importlib.util
import re
import sys

spec = importlib.util.spec_from_file_location('vw', 'voice-wash.py')
vw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vw)

PARAS = [
    # technical with protected tokens
    ("Peer-assisted HLS is an established technique; viewers share segments over WebRTC so the origin feeds only the crowd's "
     "edge. It needs rendezvous infrastructure, because peers cannot find each other from nothing, and the widely "
     "deployed engines get it from WebTorrent-compatible trackers - P2P Media Loader and PeerTube both do."),
    # narrative
    "The platform cuts the stream, or the audience turns up in numbers and flattens it. From the streamer's side, success and censorship look identical.",
    # hedged claim
    ("The privacy is narrower than it sounds. Swarm events carry no social npub and use independent per-session keys; "
     "that is the real gift, and it is pseudonymity rather than anonymity."),
    # with numbers
    ("Viewers who can't do WebRTC fall back to plain HLS; the floor is today's behaviour, held as a measured target "
     "rather than a guarantee - M3's demo page measures stalls, startup latency and battery cost over 45 segments."),
]

JUDGE_PROMPT = """Score this paraphrase 1-10 for sounding like a British software developer wrote it naturally (not an AI). Criteria: sentence rhythm variety, plain direct register, no AI-tell phrasing, no grammar slips. Reply with ONLY the integer.

Source: {src}

Paraphrase: {dst}"""


def judge(src, dst):
    import subprocess
    r = subprocess.run(['copilot', '-p', JUDGE_PROMPT.format(src=src, dst=dst),
                        '--deny-tool', '-s'], capture_output=True, text=True, timeout=600)
    m = re.search(r'\d+', r.stdout)
    return int(m.group(0)) if m else 0


def main():
    use_judge = '--no-judge' not in sys.argv
    configs = [
        ('qwen3:32b plain', dict(model='qwen3:32b')),
        ('qwen3:32b bestof3', dict(model='qwen3:32b', bestof=3)),
        ('qwen3:32b bestof3+polish', dict(model='qwen3:32b', bestof=3, polish=True)),
    ]
    print(f"{'config':<28} {'guard':>6} {'novelty':>8} {'judge':>6}")
    for name, kw in configs:
        passes, novs, scores = 0, [], []
        for p in PARAS:
            out = vw.paraphrase(p, backend='ollama', **kw)
            if out and not vw.check_guardrail(p, out):
                passes += 1
                novs.append(vw.trigram_novelty(p, out))
                if use_judge:
                    scores.append(judge(p, out))
            else:
                novs.append(0.0)
                if use_judge:
                    scores.append(0)
        nov = sum(novs) / len(novs)
        j = f"{sum(scores)/len(scores):.1f}" if use_judge else '-'
        print(f"{name:<28} {passes}/{len(PARAS)}   {nov:>7.2f} {j:>6}")


if __name__ == '__main__':
    main()
