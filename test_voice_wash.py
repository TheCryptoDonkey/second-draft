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

    def test_list_breaks_block_without_blank_line(self):
        # regression: a prose run-on into a numbered list must not become one
        # washable block — the model merged the items into prose when it was
        text = ('A prose paragraph with plenty of words to qualify as washable\n'
                'and then more of the same to be safe here today\n'
                '3. **Voice pass** - a human reads the result aloud\n'
                '4. **Publish** - only after the earlier steps, in order\n')
        blocks = vw.split_blocks(text)
        washable = [t for w, t in blocks if w]
        self.assertEqual(len(washable), 1)
        self.assertNotIn('Voice pass', washable[0])
        self.assertNotIn('Publish', washable[0])

    def test_numbered_and_bulleted_lists_not_washable(self):
        for item in ('1. First item with plenty of words in it to pass the floor easily',
                     '- bullet item with plenty of words in it to pass the floor easily',
                     '* star item with plenty of words in it to pass the floor easily',
                     '+ plus item with plenty of words in it to pass the floor easily'):
            blocks = vw.split_blocks(item)
            self.assertEqual(blocks, [(False, item)], item)

    def test_indented_continuation_paragraph_not_washable(self):
        text = ('   That recipe shows the measured ceiling for the whole setup;\n'
                '   check the bench table below for the full details today.')
        blocks = vw.split_blocks(text)
        self.assertTrue(all(not w for w, _ in blocks))


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


class TestOutputPath(unittest.TestCase):
    """The default output name must never equal the input (silent overwrite)."""

    def _default_dest(self, path):
        stem, dot, ext = path.rpartition('.')
        return f"{stem}.washed.{ext}" if dot else path + '.washed'

    def test_markdown(self):
        self.assertEqual(self._default_dest('draft.md'), 'draft.washed.md')

    def test_other_extension(self):
        self.assertEqual(self._default_dest('notes.txt'), 'notes.washed.txt')

    def test_extensionless(self):
        dest = self._default_dest('README')
        self.assertNotEqual(dest, 'README')


class TestVerifyMeaning(unittest.TestCase):
    """The entailment stage: candidates the backend says drifted must drop."""

    def setUp(self):
        self._orig_gen = vw._generate_once
        vw._backend_checked = True  # skip availability probe; we mock the backend

    def tearDown(self):
        vw._generate_once = self._orig_gen

    def test_yes_no_parsing(self):
        vw._generate_once = lambda *a, **k: 'YES'
        self.assertTrue(vw.verify_meaning('a', 'b', 'm', 'ollama'))
        vw._generate_once = lambda *a, **k: 'No, it changes a claim.'
        self.assertFalse(vw.verify_meaning('a', 'b', 'm', 'ollama'))

    def test_unparseable_drops(self):
        vw._generate_once = lambda *a, **k: '<think> hmm'
        self.assertFalse(vw.verify_meaning('a', 'b', 'm', 'ollama'))

    def test_backend_error_keeps_candidate(self):
        def boom(*a, **k):
            raise RuntimeError('connection refused')
        vw._generate_once = boom
        self.assertTrue(vw.verify_meaning('a', 'b', 'm', 'ollama'))

    def test_paraphrase_drops_inverted_candidate(self):
        src = ('Destroy statistical AI watermarks in text and code comments by '
               'paraphrasing through a local model on the machine itself today.')
        inverted = ('Destroy uses statistical AI to add watermarks in text and '
                    'code comments by paraphrasing through a local model today.')

        def fake_gen(masked, model, budget, backend, temperature=None, prompt=None):
            return inverted if prompt is None else 'NO'  # verify says: drifted

        vw._generate_once = fake_gen
        out = vw.paraphrase(src, 'm', verify=True)
        self.assertEqual(out, '')  # all candidates failed verify -> keep original

    def test_paraphrase_keeps_verified_candidate(self):
        src = ('Destroy statistical AI watermarks in text and code comments by '
               'paraphrasing through a local model on the machine itself today.')
        good = ('Destroy statistical AI watermarks from text and code comments by '
                'paraphrasing through a local model right on the machine itself.')

        def fake_gen(masked, model, budget, backend, temperature=None, prompt=None):
            return good if prompt is None else 'YES'

        vw._generate_once = fake_gen
        out = vw.paraphrase(src, 'm', verify=True)
        self.assertTrue(out)  # verify passed -> candidate returned


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
