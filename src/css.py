"""Banter — application CSS stylesheet."""

# ─────────────────────────── CSS ─────────────────────────────────

APP_CSS = """
/* ── Message bubbles ── */
.msg-bubble {
    border-radius: 18px;
    padding: 8px 14px;
}
.msg-bubble.mine {
    background-color: @accent_bg_color;
    color: @accent_fg_color;
    border-bottom-right-radius: 4px;
}
.msg-bubble.theirs {
    background-color: @card_bg_color;
    border-bottom-left-radius: 4px;
}
/* In-conversation search: bubbles whose text contains the active query
 * are wrapped in a subtle yellow tint so the matched messages stand
 * out at a glance while scrolling through results. */
.search-match .msg-bubble {
    box-shadow: inset 0 0 0 2px alpha(#f5c211, 0.85);
}
/* Optimistic UI for unsent / failed messages. .pending fades the
 * bubble while the API call is in flight. .failed marks the bubble
 * with a red error border + a Retry/Discard action row appended
 * inside it. */
.msg-bubble.pending {
    opacity: 0.6;
}
.msg-bubble.failed {
    box-shadow: inset 0 0 0 2px alpha(@error_color, 0.6);
}

/* ── Reaction pills ── */
.reaction-pill {
    border-radius: 999px;
    padding: 0 8px;
    min-height: 32px;
    font-size: 0.85em;
}
.reaction-pill-mine {
    background-color: alpha(@accent_bg_color, 0.18);
    color: @accent_fg_color;
}

/* ── Add-reaction button: smiley + tiny "+" badge ── */
.reaction-add-btn {
    min-width: 0;
    min-height: 0;
    padding: 3px 4px;
    border-radius: 999px;
}
.reaction-add-plus {
    /* Fixed color rather than @accent_bg_color so the + looks the same
       on every device — the user's theme accent varies wildly
       (blue on GNOME, orange on FuriOS, green on OnePlus Droidian). */
    color: #3584e4;
    font-weight: bold;
    font-size: 0.85em;
    margin-left: 1px;
}

/* ── Reactions sheet picker buttons ── */
.reaction-picker-btn {
    border-radius: 10px;
    padding: 2px;
    font-size: 1.3em;
}
.reaction-picker-mine {
    background-color: alpha(@accent_bg_color, 0.25);
}

/* Bottom category navigator — thin divider above */
.reaction-category-nav {
    border-top: 1px solid alpha(@borders, 0.5);
}

/* ── New-messages banner ── */
.new-msg-bar {
    border-radius: 999px;
    padding: 4px 16px;
    font-size: 0.85em;
}

/* ── Date separators ── */
.date-separator {
    margin-top: 10px;
    margin-bottom: 6px;
}
.date-separator-label {
    background-color: alpha(@card_fg_color, 0.08);
    border-radius: 999px;
    padding: 2px 14px;
    font-size: 0.78em;
    font-weight: 600;
    color: @dim_label_color;
}

/* ── Album photo grid ── */
.album-thumb {
    border-radius: 8px;
}
.album-thumb:hover {
    background-color: alpha(@accent_bg_color, 0.12);
}
.album-thumb picture {
    border-radius: 8px;
}

/* ── Input bar ── */
.compose-bar {
    border-top: 1px solid alpha(@borders, 0.5);
    padding: 8px;
}
.compose-btn {
    min-width: 44px;
    min-height: 44px;
}

/* ── Sidebar ── */
.group-list-row {
    padding: 4px 0;
    min-height: 60px;
}

/* ── Status badges ── */
.count-badge {
    background-color: @accent_bg_color;
    color: @accent_fg_color;
    border-radius: 999px;
    padding: 1px 7px;
    font-size: 0.78em;
    font-weight: bold;
}

/* ── Unread indicator — blue dot or count pill ── */
.unread-dot {
    background-color: #3584e4;
    border-radius: 999px;
    min-width: 8px;
    min-height: 8px;
    padding: 0;
}
.unread-count {
    background-color: #3584e4;
    color: white;
    border-radius: 999px;
    padding: 1px 6px;
    font-size: 0.72em;
    font-weight: bold;
    min-width: 18px;
}
/* Keep old classes for DM rows that still use them */
.unread-badge {
    background-color: @accent_bg_color;
    color: @accent_fg_color;
    border-radius: 999px;
    padding: 1px 8px;
    font-size: 0.75em;
    font-weight: bold;
    min-width: 18px;
}
.unread-badge-zero {
    color: alpha(@window_fg_color, 0.35);
    border-radius: 999px;
    padding: 1px 8px;
    font-size: 0.75em;
    min-width: 18px;
}

/* ── Muted icon ── */
.muted-icon {
    color: alpha(@window_fg_color, 0.4);
}

/* ── Conversation row time label ── */
.conv-time {
    font-size: 0.75em;
    color: alpha(@window_fg_color, 0.5);
}
.online-dot {
    background-color: #3db93d;
    border-radius: 50%;
    min-width: 10px;
    min-height: 10px;
}

/* ── Error / hint labels ── */
.error-label  { color: @error_color; }
.dim-caption  { font-size: 0.82em; }
.bold-name    { font-weight: 600; }

/* ── Image attachments ── */
.attachment-frame {
    border-radius: 12px;
}

/* ── Login page ── */
.login-card {
    background-color: @card_bg_color;
    border-radius: 16px;
    padding: 24px;
    box-shadow: 0 2px 12px alpha(black, 0.15);
}

/* ── Event card (inline in message bubble) ── */
.event-card {
    background-color: alpha(@window_bg_color, 0.5);
    border: 1px solid alpha(@borders, 0.6);
    border-radius: 10px;
    padding: 10px 12px;
    margin-top: 2px;
    margin-bottom: 2px;
}
.msg-bubble.mine .event-card {
    background-color: alpha(@accent_fg_color, 0.12);
    border-color: alpha(@accent_fg_color, 0.25);
}

/* ── Poll card (inline in message bubble) ── */
.poll-card {
    background-color: alpha(@window_bg_color, 0.5);
    border: 1px solid alpha(@borders, 0.6);
    border-radius: 10px;
    padding: 10px 12px;
    margin-top: 2px;
    margin-bottom: 2px;
}
.msg-bubble.mine .poll-card {
    background-color: alpha(@accent_fg_color, 0.12);
    border-color: alpha(@accent_fg_color, 0.25);
}
.poll-card .poll-option {
    padding: 0;
    border-radius: 6px;
}
.poll-card .poll-bar {
    min-height: 4px;
}

/* ── Reply quote block ── */
.reply-quote {
    border-left: 3px solid @accent_bg_color;
    border-radius: 4px;
    background-color: alpha(@card_bg_color, 0.6);
    padding: 4px 8px;
    margin-bottom: 2px;
}
.reply-quote-name {
    font-weight: 600;
    font-size: 0.80em;
    color: @accent_fg_color;
}
.reply-quote-text {
    font-size: 0.82em;
    color: alpha(@window_fg_color, 0.7);
}
"""


# ─────────────────────────── Reusable Widgets ────────────────────
