"""Plain-text helpers — linkification, mention highlighting, accent
color extraction. Stateless; imported by `bubble.py` and reused
wherever a message text run needs to render with the same affordances.
"""

import re

from gi.repository import Adw

_URL_RE = re.compile(
    r"(https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+|[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    re.IGNORECASE,
)


def _linkify(text: str) -> tuple:
    """Return (pango_markup, has_links).
    Wraps URLs and email addresses in <a href="..."> tags."""
    parts = _URL_RE.split(text)
    if len(parts) == 1:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), False)

    out = []
    for i, part in enumerate(parts):
        safe = part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if i % 2 == 0:
            out.append(safe)
        else:
            if part.lower().startswith("www."):
                href = "http://" + part
            elif "@" in part and not part.lower().startswith("http"):
                href = "mailto:" + part
            else:
                href = part
            href_safe = href.replace("&", "&amp;").replace('"', "&quot;")
            out.append(f'<a href="{href_safe}">{safe}</a>')
    return ("".join(out), True)


def _mention_ranges(mentions_att, text_len):
    """Return a sorted, merged list of (start, end) for the mention loci
    in `mentions_att`, clamped to `text_len`. Defensive against bad
    input."""
    if not mentions_att:
        return []
    raw = mentions_att.get("loci") or []
    out = []
    for entry in raw:
        try:
            s, length = int(entry[0]), int(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if length <= 0 or s < 0 or s >= text_len:
            continue
        out.append((s, min(s + length, text_len)))
    out.sort()
    # Merge overlaps so we don't emit nested <span> tags
    merged = []
    for r in out:
        if merged and r[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], r[1]))
        else:
            merged.append(r)
    return merged


def _accent_hex() -> str:
    """Current libadwaita accent color as #RRGGBB. Pango markup can't
    reference @accent_color directly, so we resolve it at render time
    and inject the literal hex — keeps mentions in sync with the user's
    theme accent across light/dark and accent changes."""
    try:
        rgba = Adw.StyleManager.get_default().get_accent_color_rgba()
        r = int(rgba.red   * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue  * 255)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        # libadwaita < 1.6: fall back to the GNOME default blue
        return "#3584e4"


def _build_text_markup(text: str, mentions_att, is_mine: bool = False) -> tuple:
    """Return (pango_markup, use_markup) for `text`, applying both URL
    linkification and mention highlighting. Mention spans are rendered
    as bold runs; URLs in non-mention regions are still turned into
    clickable links.

    On outgoing (own) bubbles, the bubble background is the libadwaita
    accent color — the same blue we'd use for mentions on other
    bubbles. So we render mentions on our own bubbles in white +
    underline to keep them visible against the accent background."""
    ranges = _mention_ranges(mentions_att, len(text))
    if not ranges:
        return _linkify(text)

    if is_mine:
        open_tag = '<span weight="bold" underline="single">'
    else:
        open_tag = f'<span weight="bold" foreground="{_accent_hex()}">'

    parts  = []
    cursor = 0
    for s, e in ranges:
        if cursor < s:
            seg_markup, _ = _linkify(text[cursor:s])
            parts.append(seg_markup)
        mention_text = text[s:e]
        escaped = (mention_text
                   .replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;"))
        parts.append(f'{open_tag}{escaped}</span>')
        cursor = e
    if cursor < len(text):
        seg_markup, _ = _linkify(text[cursor:])
        parts.append(seg_markup)
    return ("".join(parts), True)
