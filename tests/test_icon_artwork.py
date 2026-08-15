"""The icon generator has twice drawn the wrong mark without failing.

Both times it wrote a valid .ico and reported success. The first drew one
blank white tile — the background had moved from <rect> to <path>, so it was
flattened into the glyph, and fills had moved into style attributes, so every
colour fell back. The second kept the shape but lost the orange, because the
fills were rgb() rather than hex.

White is the colour of the largest shape in this mark, so a fill that fails
to parse looks like a design decision. Nothing downstream can notice.
"""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("PIL")

SOURCE = Path(__file__).resolve().parent.parent / "tools" / "make_icon.py"
ORANGE = (255, 92, 46, 255)
NEAR_WHITE = (245, 247, 250, 255)


@pytest.fixture(scope="module")
def make_icon():
    spec = importlib.util.spec_from_file_location("make_icon", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_mark_is_still_two_tone(make_icon):
    """The whole point of the detached foot is that it is a second colour."""
    colours = {colour for _, colour in make_icon.GLYPH}
    assert ORANGE in colours, f"the foot is not orange, got {colours}"
    assert NEAR_WHITE in colours, f"the letterform is not off-white, got {colours}"


def test_the_tile_is_not_part_of_the_glyph(make_icon):
    """The tile is drawn separately, so it must not reach the glyph list.

    When it did, it covered the whole canvas and the mark vanished behind it.

    Checked by geometry rather than by colour on purpose: FALLBACK is the
    same off-white as the letterform, so a path that failed to parse is
    indistinguishable from one that is meant to be white. That is the whole
    reason both of these got as far as a built icon.
    """
    x0, y0, x1, y1 = make_icon.BOX
    assert (x1 - x0) < 100 and (y1 - y0) < 100, "the full-canvas tile is in the glyph"


@pytest.mark.parametrize("attrs,expected", [
    ({"fill": "#FF5C2E"}, ORANGE),
    ({"style": "fill:#FF5C2E;fill-rule:nonzero;"}, ORANGE),
    ({"style": "fill:rgb(255,92,46);fill-rule:nonzero;"}, ORANGE),
    ({"style": "fill:rgb(255, 92, 46)"}, ORANGE),
])
def test_every_way_an_editor_writes_a_fill(make_icon, attrs, expected):
    """Which form appears depends on the editor, not on anything meaningful."""
    import xml.etree.ElementTree as ET

    el = ET.Element("path", attrs)
    assert make_icon._colour(el) == expected


def test_a_gradient_fill_is_recognised_as_the_tile(make_icon):
    import xml.etree.ElementTree as ET

    for attrs in ({"fill": "url(#g)"}, {"style": "fill:url(#_Linear1);"}):
        assert make_icon._fill(ET.Element("path", attrs)).startswith("url(")
