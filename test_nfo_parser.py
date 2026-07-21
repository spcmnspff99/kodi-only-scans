"""
test_nfo_parser.py
~~~~~~~~~~~~~~~~~~
Unit tests for nfo_parser, especially multi-block episode NFOs.
Run with:  python -m unittest test_nfo_parser -v
"""

import unittest

from nfo_parser import parse_episode_nfo, parse_episode_nfos


SINGLE_EPISODE_NFO = """<?xml version="1.0" encoding="UTF-8"?>
<episodedetails>
  <title>Pilot</title>
  <season>1</season>
  <episode>1</episode>
  <aired>2008-01-20</aired>
</episodedetails>
"""

# Sonarr-style multi-episode NFO: concatenated documents, each with its own
# XML declaration.
MULTI_EPISODE_NFO = """<?xml version="1.0" encoding="UTF-8"?>
<episodedetails>
  <title>Pilot</title>
  <season>1</season>
  <episode>1</episode>
  <aired>2008-01-20</aired>
</episodedetails>
<?xml version="1.0" encoding="UTF-8"?>
<episodedetails>
  <title>Cat's in the Bag...</title>
  <season>1</season>
  <episode>2</episode>
  <aired>2008-01-27</aired>
</episodedetails>
"""

MULTI_EPISODE_NFO_NO_DECL = """<episodedetails>
  <title>Pilot</title>
  <season>1</season>
  <episode>1</episode>
</episodedetails>
<episodedetails>
  <title>Cat's in the Bag...</title>
  <season>1</season>
  <episode>2</episode>
</episodedetails>
"""


class TestEpisodeNfos(unittest.TestCase):
    def test_single_block(self):
        nfos = parse_episode_nfos(SINGLE_EPISODE_NFO)
        self.assertEqual(len(nfos), 1)
        self.assertEqual(nfos[0].title, "Pilot")
        self.assertEqual(nfos[0].season, "1")
        self.assertEqual(nfos[0].episode, "1")

    def test_multi_block_with_decls(self):
        nfos = parse_episode_nfos(MULTI_EPISODE_NFO)
        self.assertEqual(len(nfos), 2)
        self.assertEqual(nfos[0].title, "Pilot")
        self.assertEqual(nfos[0].episode, "1")
        self.assertEqual(nfos[1].title, "Cat's in the Bag...")
        self.assertEqual(nfos[1].episode, "2")
        self.assertEqual(nfos[1].aired, "2008-01-27")

    def test_multi_block_without_decls(self):
        nfos = parse_episode_nfos(MULTI_EPISODE_NFO_NO_DECL)
        self.assertEqual(len(nfos), 2)
        self.assertEqual([n.episode for n in nfos], ["1", "2"])

    def test_parse_episode_nfo_returns_first(self):
        nfo = parse_episode_nfo(MULTI_EPISODE_NFO)
        self.assertIsNotNone(nfo)
        self.assertEqual(nfo.title, "Pilot")

    def test_invalid_content(self):
        self.assertEqual(parse_episode_nfos("not xml at all"), [])
        self.assertEqual(parse_episode_nfos(""), [])
        self.assertEqual(parse_episode_nfos("<movie><title>X</title></movie>"), [])


if __name__ == "__main__":
    unittest.main()
