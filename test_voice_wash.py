"""Tests for voice-wash's deterministic logic.

Everything here runs without a model or network: the paraphrase backends are
deliberately untested (they are thin HTTP/CLI adapters), while every guardrail,
scrub, block-splitting and comment-extraction rule IS tested, because those
are what make a bad rewrite safe.

Run: python3 -m pytest test_voice_wash.py   (or: python3 -m unittest)
"""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'voice_wash', Path(__file__).with_name('voice-wash.py'))
vw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vw)


class TestScrubUnicode(unittest.TestCase):
    def test_clean_ascii_passes_through_byte_identical(self):
        s = 'Plain ASCII prose, with punctuation; nothing exotic.  Two spaces.'
        cleaned, report = vw.scrub_unicode(s)
        self.assertEqual(cleaned, s)
        self.assertEqual(report, [])

    def test_zero_width_family_stripped(self):
        cleaned, report = vw.scrub_unicode('a​b‌c‍d⁠e')
        self.assertEqual(cleaned, 'abcde')
        self.assertEqual(len(report), 4)

    def test_bidi_controls_stripped(self):
        cleaned, _ = vw.scrub_unicode('x‮y‬z‎w‏')
        self.assertEqual(cleaned, 'xyzw')

    def test_tag_characters_stripped(self):
        cleaned, _ = vw.scrub_unicode('pay\U000E0061\U000E007Fload')
        self.assertEqual(cleaned, 'payload')

    def test_variation_selectors_stripped(self):
        cleaned, _ = vw.scrub_unicode('a️b\U000E0100c')
        self.assertEqual(cleaned, 'abc')

    def test_space_homoglyphs_normalised(self):
        for cp in (0x00A0, 0x1680, 0x2000, 0x2003, 0x2009, 0x202F, 0x205F, 0x3000):
            cleaned, report = vw.scrub_unicode('a' + chr(cp) + 'b')
            self.assertEqual(cleaned, 'a b', f'U+{cp:04X}')
            self.assertIn('normalised', report[0])

    def test_soft_hyphen_and_bom_stripped(self):
        cleaned, _ = vw.scrub_unicode('﻿soft­hyph')
        self.assertEqual(cleaned, 'softhyph')

    def test_visible_unicode_survives(self):
        s = 'café — naïve résumé'
        cleaned, report = vw.scrub_unicode(s)
        self.assertEqual(cleaned, s)
        self.assertEqual(report, [])


class TestSplitBlocks(unittest.TestCase):
    def test_frontmatter_and_fences_not_washable(self):
        text = ('---\ntitle: Something long enough to matter here\n---\n'
                '# Header\n\n'
                '```\ncode that is long enough to matter here\n```\n')
        blocks = vw.split_blocks(text)
        self.assertTrue(all(not w for w, _ in blocks))

    def test_long_paragraph_is_washable(self):
        text = ('This is a genuinely long paragraph of prose with more than '
                'fifteen words in it so the washer should consider it a '
                'candidate for paraphrasing today.')
        blocks = vw.split_blocks(text)
        self.assertEqual(blocks, [(True, text)])

    def test_short_paragraph_skipped(self):
        text = 'Too short.'
        blocks = vw.split_blocks(text)
        self.assertEqual(blocks, [(False, text)])

    def test_metadata_lines_skipped(self):
        text = '**Status:** this line has plenty of words but is tracker metadata and must survive untouched'
        blocks = vw.split_blocks(text)
        self.assertEqual(blocks, [(False, text)])


class TestProtectedTokens(unittest.TestCase):
    def test_urls_numbers_names(self):
        text = 'See https://example.com/x for the 42 relays run by RelaySwarm.'
        toks = vw.protected_tokens(text)
        self.assertIn('https://example.com/x', toks)
        self.assertTrue(any('42' in t for t in toks))
        self.assertTrue(any('RelaySwarm' in t for t in toks))

    def test_mask_unmask_roundtrip(self):
        text = 'RelaySwarm serves 42 segments; see https://example.com.'
        masked, mapping = vw.mask_tokens(text)
        self.assertIn('ZXQ', masked)
        self.assertEqual(vw.unmask_tokens(masked, mapping), text)


class TestGuardrails(unittest.TestCase):
    SRC = ('RelaySwarm cuts origin bandwidth by 72 percent according to the '
           'benchmarks published at https://example.com/bench last Tuesday.')

    def test_good_rewrite_passes(self):
        dst = ('According to the benchmarks over at https://example.com/bench, '
               'RelaySwarm reduces origin bandwidth by 72 percent, published '
               'last Tuesday.')
        self.assertEqual(vw.check_guardrail(self.SRC, dst), [])

    def test_dropped_url_fails(self):
        dst = ('RelaySwarm reduces origin bandwidth by 72 percent according '
               'to published benchmarks from last Tuesday.')
        self.assertTrue(any('https://example.com' in m for m in
                            vw.check_guardrail(self.SRC, dst)))

    def test_verbatim_copy_fails(self):
        self.assertIn('<verbatim copy — no wash>', vw.check_guardrail(self.SRC, self.SRC))

    def test_length_explosion_fails(self):
        dst = ' '.join([self.SRC] * 5)
        self.assertTrue(any('length ratio' in m for m in
                            vw.check_guardrail(self.SRC, dst)))

    def test_ai_tell_fails(self):
        dst = ('Crucially, RelaySwarm cuts origin bandwidth by 72 percent; '
               'see https://example.com/bench from last Tuesday.')
        self.assertTrue(any('style' in m for m in vw.check_guardrail(self.SRC, dst)))

    def test_americanism_fails(self):
        dst = ('RelaySwarm cuts origin bandwidth by 72 percent and the color '
               'data is at https://example.com/bench since last Tuesday.')
        self.assertTrue(any('color' in m for m in vw.check_guardrail(self.SRC, dst)))

    def test_empty_output_fails(self):
        self.assertEqual(vw.check_guardrail(self.SRC, ''), ['<empty output>'])


class TestNormalizeStyle(unittest.TestCase):
    def test_dashes_become_semicolons(self):
        self.assertEqual(vw.normalize_style('this — that – other'), 'this; that; other')

    def test_spaced_hyphen_parenthetical_becomes_semicolon(self):
        self.assertEqual(vw.normalize_style('a result - surprisingly - held'),
                         'a result; surprisingly; held')

    def test_two_spaces_after_full_stop(self):
        self.assertEqual(vw.normalize_style('One. Two.'), 'One.  Two.')

    def test_word_hyphens_and_ranges_untouched(self):
        self.assertEqual(vw.normalize_style('M1-M3 took 0.75-0.82s'),
                         'M1-M3 took 0.75-0.82s')

    def test_curly_quotes_straightened(self):
        self.assertEqual(vw.normalize_style('“it’s”'), '"it\'s"')


class TestCommentExtraction(unittest.TestCase):
    def test_line_comment_block(self):
        src = ('// This is a fairly long comment block explaining things\n'
               '// in some detail for the reader of this code today\n'
               'const x = 1;\n')
        spans = vw.extract_comment_blocks(src, '.js')
        self.assertEqual(len(spans), 1)
        start, end, rebuild, ctext = spans[0]
        self.assertIn('fairly long comment', ctext)
        rebuilt = rebuild('A replacement comment with enough words in it now')
        self.assertIn('// A replacement comment', rebuilt)

    def test_code_never_inside_spans(self):
        src = 'const secret = compute();\n// a comment line that is long enough to matter here\n'
        spans = vw.extract_comment_blocks(src, '.js')
        self.assertEqual(len(spans), 1)
        self.assertNotIn('compute', spans[0][3])


class TestHelpers(unittest.TestCase):
    def test_trigram_novelty_bounds(self):
        a = 'the quick brown fox jumps over the lazy dog again today'
        self.assertEqual(vw.trigram_novelty(a, a), 0.0)
        b = 'completely different words with nothing shared at all here'
        self.assertGreater(vw.trigram_novelty(a, b), 0.9)

    def test_reflow_matches_source_width(self):
        src = '\n'.join(['x ' * 35] * 3).strip()
        long_text = 'word ' * 60
        out = vw.reflow_like(long_text, src)
        self.assertTrue(all(len(l) <= 72 for l in out.split('\n')))

    def test_word_diff_marks_changes(self):
        diff = vw.word_diff('the cat sat', 'the dog sat')
        self.assertIn('[-cat-]{+dog+}', diff)


if __name__ == '__main__':
    unittest.main()
