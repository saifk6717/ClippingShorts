import time

import numpy as np

from clippingshorts.audio_analyzer import SR, AudioAnalyzer
from clippingshorts.chat_analyzer import ChatAnalyzer, ChatEvent
from clippingshorts.gemini_judge import combine, lines_from_words
from clippingshorts.highlight_detector import HighlightDetector


def tone(amp, seconds=1):
    t = np.arange(int(SR * seconds)) / SR
    x = amp * np.sin(2 * np.pi * 220 * t) + 0.002 * np.random.randn(len(t))
    return (x * 32767).astype(np.int16).tobytes()


def test_audio_jump_is_relative_to_baseline():
    a = AudioAnalyzer()
    for _ in range(90):
        a.feed(tone(0.02))
    quiet = a.excitement()
    for _ in range(3):
        a.feed(tone(0.4))
    assert a.last["z"] > 3 and a.last["jump_pct"] > 300
    assert a.excitement() > 55 > quiet


def test_audio_always_loud_streamer_is_not_exciting():
    a = AudioAnalyzer()
    for _ in range(120):
        a.feed(tone(0.4))
    assert a.excitement() < 20


def test_audio_memory_is_bounded():
    a = AudioAnalyzer(history_seconds=100)
    for _ in range(500):
        a.feed(tone(0.1, 0.05))
    assert len(a.db) == 100


def _ev(user, text="lol", emotes=0):
    return ChatEvent("twitch", "s", time.time(), user, user, text, emotes)


def test_chat_explosion_of_many_people_scores_high():
    c = ChatAnalyzer()
    for sec in range(0, 170):
        c.add(_ev(f"u{sec % 7}", "hello"), sec)
    for sec in range(170, 180):
        for i in range(12):
            c.add(_ev(f"viewer{sec}-{i}", "OMEGALUL no way", 1), sec)
    score, stats = c.excitement(179)
    assert stats["unique_users"] >= 100
    assert score > 60


def test_single_user_spam_is_discounted():
    c = ChatAnalyzer()
    for sec in range(0, 170):
        c.add(_ev(f"u{sec % 7}", "hello"), sec)
    for sec in range(170, 180):
        for _ in range(30):
            c.add(_ev("spammer", "BUY FOLLOWERS"), sec)
    score, stats = c.excitement(179)
    assert stats["top_user_share"] > 0.9
    assert score < 20


def test_chat_window_is_bounded():
    c = ChatAnalyzer(window_seconds=60)
    for sec in range(0, 1000):
        c.add(_ev("a"), sec)
    assert len(c.sec) <= 67


def test_detector_cooldown_and_cap():
    d = HighlightDetector({"candidate_cooldown_seconds": 30, "max_candidates_per_hour": 2,
                           "ai_eval_interval_seconds": 0})
    fired = [d.step(t, 90, 0, {}, True) for t in range(100, 200)]
    hits = [c for c in fired if c]
    assert len(hits) == 2                      # cooldown spaced them, then the hourly cap stopped more
    assert hits[1].t - hits[0].t >= 30


def test_detector_keyword_trigger():
    d = HighlightDetector({"ai_eval_interval_seconds": 0})
    d.on_words([{"w": "oh", "s": 50, "e": 50.2}, {"w": "clip", "s": 50.3, "e": 50.5}, {"w": "that", "s": 50.6,
                                                                                       "e": 50.8}])
    c = d.step(55, 0, 0, {}, True)
    assert c and "clip" in c.trigger and abs(c.t - 50.3) < 0.01


def test_judges_must_agree():
    t = {"found": True, "start": 10, "end": 40, "score": 75}
    r = {"found": True, "start": 30, "end": 45, "score": 70}
    assert combine(t, r, 60)[0]
    assert not combine(t, {"found": False, "score": 0}, 60)[0]
    assert not combine({"found": True, "start": 10, "end": 20, "score": 75},
                       {"found": True, "start": 200, "end": 210, "score": 70}, 60)[0]
    assert combine({"found": True, "start": 10, "end": 20, "score": 95}, {"found": False, "score": 0}, 70)[0]
    # no speech to judge (live transcription off): a clear reaction alone decides, a weak one does not
    silent = {"found": False, "score": 0, "reason": "not enough speech"}
    assert combine(silent, {"found": True, "start": 5, "end": 15, "score": 75}, 60)[0]
    assert not combine(silent, {"found": True, "start": 5, "end": 15, "score": 50}, 60)[0]


def test_lines_grouping():
    words = [{"w": f"w{i}", "s": i * 0.3, "e": i * 0.3 + 0.2} for i in range(10)] + \
            [{"w": "late", "s": 20, "e": 20.3}]
    lines = lines_from_words(words)
    assert len(lines) == 2 and lines[1]["text"] == "late"


def test_late_batched_chat_is_placed_when_written():
    c = ChatAnalyzer(window_seconds=60)
    for sec in (100, 90, 95, 101, 30, 102):       # out of order, as batched YouTube chat arrives
        c.add(_ev(f"u{sec}"), sec)
    for sec in range(103, 300):
        c.add(_ev("x"), sec)
    assert min(c.sec) >= 299 - 66 and len(c.sec) <= 67   # old seconds are still pruned
