"""Guard: a Jinja `#}` must never close a CSS comment in style.css.

Twice now (#449 attachment-fallback dead 2 weeks; #642-W19 nav fix inert), a
`/* ... #}` typo silently swallowed the following CSS rules into an unterminated
comment. This lints the shipped stylesheet so it can't recur.
"""
import pathlib


def test_style_css_has_no_jinja_comment_close():
    css = (pathlib.Path(__file__).resolve().parents[1] / "static" / "style.css").read_text()
    offenders = [
        (i + 1, line.strip())
        for i, line in enumerate(css.splitlines())
        if "#}" in line
    ]
    assert not offenders, (
        "CSS comment closed with Jinja `#}` instead of `*/` — swallows following "
        f"rules into the comment. Offending lines: {offenders}"
    )
