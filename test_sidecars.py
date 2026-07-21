"""
test_sidecars.py
~~~~~~~~~~~~~~~~
Unit tests for the pure filename-parsing helpers in sidecars.py.
Run with:  python -m unittest test_sidecars -v
"""

import unittest

from sidecars import (
    classify_art_file,
    dedupe_vobsub_pairs,
    extras_folder_type,
    is_sample_file,
    is_trailer_file,
    parse_episode_indices,
    parse_subtitle_filename,
)


class TestSubtitles(unittest.TestCase):
    def test_basic_language(self):
        sub = parse_subtitle_filename("Movie (2020)", "Movie (2020).en.srt")
        self.assertIsNotNone(sub)
        self.assertEqual(sub.language, "en")
        self.assertEqual(sub.codec, "srt")
        self.assertFalse(sub.forced)
        self.assertFalse(sub.sdh)

    def test_forced_flag(self):
        sub = parse_subtitle_filename("Movie (2020)", "Movie (2020).en.forced.srt")
        self.assertIsNotNone(sub)
        self.assertEqual(sub.language, "en")
        self.assertTrue(sub.forced)

    def test_three_letter_and_sdh(self):
        sub = parse_subtitle_filename("Show.S01E01", "Show.S01E01.eng.sdh.ass")
        self.assertIsNotNone(sub)
        self.assertEqual(sub.language, "eng")
        self.assertTrue(sub.sdh)
        self.assertEqual(sub.codec, "ass")

    def test_region_tag(self):
        sub = parse_subtitle_filename("Movie (2020)", "Movie (2020).pt-BR.srt")
        self.assertIsNotNone(sub)
        self.assertEqual(sub.language, "pt-br")

    def test_hi_alias_for_sdh(self):
        sub = parse_subtitle_filename("Movie (2020)", "Movie (2020).de.hi.vtt")
        self.assertIsNotNone(sub)
        self.assertEqual(sub.language, "de")
        self.assertTrue(sub.sdh)
        self.assertEqual(sub.codec, "vtt")

    def test_no_language_token_returns_none(self):
        self.assertIsNone(parse_subtitle_filename("Movie (2020)", "Movie (2020).srt"))

    def test_unknown_token_returns_none(self):
        self.assertIsNone(parse_subtitle_filename("Movie (2020)", "Movie (2020).commentary.srt"))

    def test_other_video_returns_none(self):
        self.assertIsNone(parse_subtitle_filename("Movie (2020)", "Other Movie.en.srt"))

    def test_not_a_subtitle_extension(self):
        self.assertIsNone(parse_subtitle_filename("Movie (2020)", "Movie (2020).en.txt"))

    def test_idx_and_sub(self):
        idx = parse_subtitle_filename("Movie (2020)", "Movie (2020).en.idx")
        sub = parse_subtitle_filename("Movie (2020)", "Movie (2020).en.sub")
        self.assertIsNotNone(idx)
        self.assertIsNotNone(sub)
        self.assertEqual(idx.codec, "vobsub")
        self.assertEqual(sub.codec, "vobsub")

    def test_vobsub_pair_dedupes(self):
        idx = parse_subtitle_filename("Movie (2020)", "Movie (2020).en.idx")
        sub = parse_subtitle_filename("Movie (2020)", "Movie (2020).en.sub")
        srt = parse_subtitle_filename("Movie (2020)", "Movie (2020).fr.srt")
        deduped = dedupe_vobsub_pairs([idx, sub, srt])
        self.assertEqual(len(deduped), 2)
        self.assertEqual({s.filename for s in deduped},
                         {"Movie (2020).en.idx", "Movie (2020).fr.srt"})


class TestArtClassification(unittest.TestCase):
    def test_folder_level_types(self):
        cases = {
            "poster.jpg": "poster",
            "folder.jpg": "poster",
            "fanart.jpg": "fanart",
            "backdrop.jpg": "fanart",
            "banner.jpg": "banner",
            "landscape.jpg": "landscape",
            "thumb.jpg": "landscape",
            "logo.png": "clearlogo",
            "clearlogo.png": "clearlogo",
            "clearart.png": "clearart",
            "characterart.png": "characterart",
            "disc.png": "discart",
            "discart.png": "discart",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                art = classify_art_file(filename)
                self.assertIsNotNone(art)
                self.assertEqual(art.art_type, expected)
                self.assertIsNone(art.season)

    def test_stem_prefixed_types(self):
        stem = "Movie (2020)"
        cases = {
            "Movie (2020)-poster.jpg": "poster",
            "Movie (2020)-fanart.jpg": "fanart",
            "Movie (2020)-clearlogo.png": "clearlogo",
            "Movie (2020)-landscape.jpg": "landscape",
            "Movie (2020)-discart.png": "discart",
            "Movie (2020)-thumb.jpg": "thumb",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                art = classify_art_file(filename, video_stem=stem)
                self.assertIsNotNone(art)
                self.assertEqual(art.art_type, expected)

    def test_tbn_is_thumb(self):
        art = classify_art_file("Show.S01E01.tbn", video_stem="Show.S01E01")
        self.assertIsNotNone(art)
        self.assertEqual(art.art_type, "thumb")

    def test_folder_level_art_with_video_stem(self):
        # logo.png / landscape.jpg next to a video must classify even when a
        # video_stem is given (movie-folder scanning).
        logo = classify_art_file("logo.png", video_stem="A Quiet Place (2018) Bluray-1080p")
        self.assertIsNotNone(logo)
        self.assertEqual(logo.art_type, "clearlogo")
        land = classify_art_file("landscape.jpg", video_stem="A Quiet Place (2018) Bluray-1080p")
        self.assertIsNotNone(land)
        self.assertEqual(land.art_type, "landscape")

    def test_stem_prefixed_wrong_stem_returns_none(self):
        self.assertIsNone(classify_art_file("Other-poster.jpg", video_stem="Movie (2020)"))

    def test_season_art(self):
        cases = {
            "season01-poster.jpg": ("poster", 1),
            "season02-fanart.jpg": ("fanart", 2),
            "season1-banner.jpg": ("banner", 1),
            "season-all-poster.jpg": ("poster", -1),
            "season-specials-poster.jpg": ("poster", 0),
            "season03-landscape.jpg": ("landscape", 3),
            "season01-clearlogo.png": ("clearlogo", 1),
        }
        for filename, (expected_type, expected_season) in cases.items():
            with self.subTest(filename=filename):
                art = classify_art_file(filename)
                self.assertIsNotNone(art)
                self.assertEqual(art.art_type, expected_type)
                self.assertEqual(art.season, expected_season)

    def test_non_image_returns_none(self):
        self.assertIsNone(classify_art_file("poster.txt"))
        self.assertIsNone(classify_art_file("movie.nfo"))
        self.assertIsNone(classify_art_file("random.jpg"))


class TestTrailerSample(unittest.TestCase):
    def test_trailers(self):
        self.assertTrue(is_trailer_file("Movie (2020)-trailer.mkv"))
        self.assertTrue(is_trailer_file("movie.trailer.mp4"))
        self.assertTrue(is_trailer_file("trailer.mkv"))
        self.assertTrue(is_trailer_file("Movie (2020) - trailer.avi"))

    def test_non_trailers(self):
        self.assertFalse(is_trailer_file("Movie (2020).mkv"))
        self.assertFalse(is_trailer_file("Trailers (2020).mkv"))
        # Name-based check is extension-agnostic; the walker only flags video
        # files, so a '-trailer.srt' subtitle never reaches the DB as a video.
        self.assertTrue(is_trailer_file("Movie (2020)-trailer.srt"))

    def test_samples(self):
        self.assertTrue(is_sample_file("Movie (2020)-sample.mkv"))
        self.assertTrue(is_sample_file("movie.sample.mkv"))
        self.assertTrue(is_sample_file("sample.mkv"))

    def test_non_samples(self):
        self.assertFalse(is_sample_file("The Sampler (2020).mkv"))
        self.assertFalse(is_sample_file("Samples of Life (2021).mkv"))


class TestEpisodeIndices(unittest.TestCase):
    def test_single_episode(self):
        self.assertEqual(parse_episode_indices("Show.S01E05.mkv"), (1, [5]))
        self.assertEqual(parse_episode_indices("show.s02e12.mkv"), (2, [12]))

    def test_multi_episode(self):
        self.assertEqual(parse_episode_indices("Show.S01E01E02.mkv"), (1, [1, 2]))
        self.assertEqual(parse_episode_indices("Show.S03E01E02E03.mkv"), (3, [1, 2, 3]))

    def test_no_episode_pattern(self):
        self.assertIsNone(parse_episode_indices("Movie (2020).mkv"))
        self.assertIsNone(parse_episode_indices("Show Season 1.mkv"))


class TestExtrasFolders(unittest.TestCase):
    def test_known_folders(self):
        self.assertEqual(extras_folder_type("extras"), "Other")
        self.assertEqual(extras_folder_type("Behind The Scenes"), "Behind The Scenes")
        self.assertEqual(extras_folder_type("deleted scenes"), "Deleted Scenes")
        self.assertEqual(extras_folder_type("Featurettes"), "Featurettes")
        self.assertEqual(extras_folder_type("trailers"), "Trailer")
        self.assertEqual(extras_folder_type("Shorts"), "Shorts")

    def test_unknown_folders(self):
        self.assertIsNone(extras_folder_type("Season 1"))
        self.assertIsNone(extras_folder_type("Movie (2020)"))


if __name__ == "__main__":
    unittest.main()
