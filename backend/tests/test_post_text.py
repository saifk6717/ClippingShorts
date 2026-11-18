from types import SimpleNamespace

from clippingshorts.workers.ai_worker import post_text
from clippingshorts.youtube_uploader import build_body


def clip(**kw):
    base = dict(title="audio jump +174%", hashtags=[], creator_name="Streamer", source_url="https://youtu.be/x")
    base.update(kw)
    return SimpleNamespace(**base)


def test_fixed_hashtags_come_first_and_are_in_the_description():
    meta = {"title": "Big moment", "description": "Something happened.", "hashtags": ["gta6", "Funny"]}
    title, desc, tags = post_text(clip(), meta, ["GTA6", "#GTAVI"])
    assert tags[:4] == ["GTA6", "GTAVI", "Funny", "shorts"]          # de-duplicated ignoring case
    assert "#GTA6 #GTAVI #Funny #shorts" in desc
    assert desc.startswith("Something happened.") and desc.rstrip().endswith("https://youtu.be/x")
    assert title == "Big moment"


def test_failed_title_writer_never_publishes_the_trigger_text():
    title, desc, tags = post_text(clip(), {}, ["GTA6"])
    assert not title.startswith("audio jump")
    assert "#GTA6" in desc and tags[0] == "GTA6"


def test_uploader_does_not_add_a_second_hashtag_line():
    up = SimpleNamespace(tags=["GTA6", "shorts"], description="Text\n\n#GTA6 #shorts\n\nClip from X", title="t",
                         privacy="public", publish_at=0)
    assert build_body(up)["snippet"]["description"].count("#GTA6") == 1
