"""Ranking search hits.

The upstream order is not an answer: asking YouTube for "<artist> <song>"
returns the live cut, three uploads of the wrong song and a reaction video as
readily as it returns the record. These tests pin the properties that make the
record win, using the real result sets that made them necessary.
"""

from __future__ import annotations

from app.youtube import SearchCandidate, rank_candidates, score_candidate


def hit(title: str, **kwargs) -> SearchCandidate:
    """A candidate with plausible defaults; tests set what they are about."""
    defaults = {
        "youtube_id": f"{abs(hash(title)):011d}"[:11],
        "duration_s": 240,
        "view_count": 1_000_000,
    }
    return SearchCandidate(title=title, **{**defaults, **kwargs})


def ordered(query: str, candidates: list[SearchCandidate], **kwargs) -> list[str]:
    return [c.title for c in rank_candidates(query, candidates, len(candidates), **kwargs)]


# --- Relevance --------------------------------------------------------------
def test_the_asked_for_song_beats_the_rest_of_the_artists_catalogue():
    """The complaint that started this: a search for one song returned one live
    version and a pile of other songs by the same band."""
    catalogue = [
        hit("Antilopen Gang - Enkeltrick", channel="Antilopen Gang"),
        hit("Antilopen Gang - Baggersee", channel="Antilopen Gang"),
        hit("Pizza (Live)", channel="Antilopen Gang"),
        hit("Antilopen Gang - Pizza", channel="Antilopen Gang", view_count=9_000_000),
        hit("Antilopen Gang - Beate Zschäpe hört U2", channel="Antilopen Gang"),
    ]

    assert ordered("antilopen gang pizza", catalogue)[0] == "Antilopen Gang - Pizza"


def test_a_missing_query_word_sinks_a_hit_below_every_complete_one():
    complete = hit("Antilopen Gang - Pizza", channel="Antilopen Gang")
    # Same artist, hugely more popular, but not the song that was asked for.
    partial = hit("Antilopen Gang - Enkeltrick", channel="Antilopen Gang", view_count=90_000_000)

    assert score_candidate("antilopen gang pizza", complete) > score_candidate(
        "antilopen gang pizza", partial
    )


def test_the_artist_may_be_named_by_the_channel_alone():
    """Music uploads routinely title only the song and leave the artist to the
    channel, which still answers "<artist> <song>" exactly."""
    by_channel = hit("Pizza", channel="Antilopen Gang")
    by_nobody = hit("Pizza", channel="Cooking With Marco")

    assert score_candidate("antilopen gang pizza", by_channel) > score_candidate(
        "antilopen gang pizza", by_nobody
    )


def test_accents_and_punctuation_do_not_decide_anything():
    """Nobody reaches for ä on the way to a song."""
    accented = hit("Beate Zschäpe hört U2", channel="Antilopen Gang")
    plain = hit("Beate Zschape hort U2", channel="Antilopen Gang")

    query = "antilopen gang beate zschape hort u2"
    assert score_candidate(query, accented) == score_candidate(query, plain)


# --- Which recording --------------------------------------------------------
def test_the_studio_version_outranks_other_recordings_of_it():
    studio = hit("Antilopen Gang - Pizza", channel="Antilopen Gang")
    others = [
        hit("Antilopen Gang - Pizza (Live)", channel="Antilopen Gang"),
        hit("Antilopen Gang - Pizza (Acoustic Cover)", channel="Some Guy"),
        hit("Antilopen Gang - Pizza KARAOKE", channel="Karaoke World"),
        hit("Antilopen Gang - Pizza (sped up)", channel="tiktok audios"),
        hit("Antilopen Gang - Pizza REACTION!!", channel="Reaction Channel"),
    ]

    assert ordered("antilopen gang pizza", [*others, studio])[0] == studio.title


def test_a_variant_asked_for_is_not_penalised():
    """Wanting the live version is a legitimate thing to search for."""
    live = hit("Antilopen Gang - Pizza (Live)", channel="Antilopen Gang")
    studio = hit("Antilopen Gang - Pizza", channel="Antilopen Gang")

    assert ordered("antilopen gang pizza live", [studio, live])[0] == live.title


def test_a_gig_dated_in_its_title_is_still_a_live_recording():
    record = hit("Antilopen Gang - Pizza", channel="Antilopen Gang")
    bootleg = hit("Antilopen Gang - Pizza - Expo Plaza Hannover - 03.06.2023", channel="Olli")

    assert ordered("antilopen gang pizza", [bootleg, record])[0] == record.title


def test_an_hour_long_upload_does_not_answer_a_song_query():
    song = hit("Queen - Bohemian Rhapsody (Official Video)", channel="Queen Official")
    album = hit("Queen - Bohemian Rhapsody FULL ALBUM", channel="Uploads", duration_s=3200)

    assert ordered("bohemian rhapsody", [album, song])[0] == song.title


# --- Provenance -------------------------------------------------------------
def test_the_original_outranks_a_cover_that_names_itself_no_differently():
    """Neither title says "cover"; the difference is who released it and how
    many people played it."""
    original = hit(
        "Queen – Bohemian Rhapsody (Official Video Remastered)",
        channel="Queen Official",
        view_count=1_700_000_000,
        verified=True,
    )
    cover = hit(
        "Pentatonix - Bohemian Rhapsody (Official Video)",
        channel="Pentatonix",
        view_count=400_000_000,
        verified=True,
    )

    assert ordered("bohemian rhapsody", [cover, original])[0] == original.title


def test_being_in_the_music_catalogue_counts_for_something():
    song = hit("Pizza", channel="Antilopen Gang", catalog=True)
    video = hit("Pizza", channel="Antilopen Gang", catalog=False)

    assert score_candidate("antilopen gang pizza", song) > score_candidate(
        "antilopen gang pizza", video
    )


def test_popularity_only_breaks_ties():
    """A billion plays cannot make the wrong song the right one."""
    wrong = hit("Antilopen Gang - Enkeltrick", channel="Antilopen Gang", view_count=10**9)
    right = hit("Antilopen Gang - Pizza", channel="Antilopen Gang", view_count=1000)

    assert ordered("antilopen gang pizza", [wrong, right])[0] == right.title


# --- Playability ------------------------------------------------------------
def test_a_track_the_room_would_refuse_is_shown_but_never_first():
    """Demoted rather than hidden: the room says why it cannot be added."""
    long_hit = hit("Bohemian Rhapsody (Extended)", duration_s=1800, view_count=10**8)
    short_hit = hit("Bohemian Rhapsody", duration_s=355)

    order = ordered("bohemian rhapsody", [long_hit, short_hit], max_duration_s=900)
    assert order == [short_hit.title, long_hit.title]


def test_a_clip_is_not_the_song():
    clip = hit("bad guy", duration_s=30, view_count=10**8)
    song = hit("bad guy", duration_s=194)

    assert ordered("bad guy", [clip, song])[0] == song.title


# --- Determinism ------------------------------------------------------------
def test_equal_hits_keep_upstream_order_so_a_repeated_search_looks_the_same():
    first = hit("Song A", youtube_id="aaaaaaaaaaa")
    second = hit("Song A", youtube_id="bbbbbbbbbbb")

    assert rank_candidates("song a", [first, second], 2) == [first, second]
    assert rank_candidates("song a", [second, first], 2) == [second, first]


def test_the_limit_is_respected():
    hits = [hit(f"Song {n}") for n in range(20)]

    assert len(rank_candidates("song", hits, 8)) == 8


def test_an_empty_query_scores_nothing_rather_than_dividing_by_zero():
    assert score_candidate("", hit("Anything")) == 0.0
