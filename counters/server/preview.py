"""ord-style preview wrappers for untrusted inscription content.

Mirrors ord's model (see ord's `src/inscriptions/media.rs`,
`src/templates/iframe.rs`, and `src/subcommand/server/server_config.rs`):

  * Untrusted bytes are served from ``/content/<n>`` under a confining CSP and
    are embedded **only** inside a sandboxed iframe (``sandbox=allow-scripts``
    => scripts run, but the frame is an opaque origin with no same-origin
    access, so it can never touch the explorer).
  * ``/preview/<n>`` returns, per media type, either the **raw content**
    (``text/html`` / ``image/svg+xml`` — ord's ``Media::Iframe``) or a small,
    same-origin **wrapper page** that loads ``/content/<n>`` with a native
    element (``<img>``, ``<audio>``, ``<video>``, …). Each wrapper ships a
    tailored ``Content-Security-Policy`` matching ord's per-media policies.

This module is pure: it only classifies a content type and builds the wrapper
HTML/CSP. The server (``app.py``) owns store access and response writing.
"""

from __future__ import annotations

import html

# Media kinds, mirroring ord's `Media` enum. "iframe" means the raw content is
# rendered directly inside the sandboxed iframe (HTML/SVG); every other kind
# gets a wrapper page built below.
IFRAME = "iframe"


def classify(content_type: str | None) -> tuple[str, str | None]:
    """Map a content type to ``(kind, extra)``.

    ``extra`` carries the image-rendering mode for images and the language for
    code, mirroring ord's table. Unknown types fall back to a download page.
    """
    ct = (content_type or "").split(";")[0].strip().lower()

    # Active content: rendered as a raw document inside the sandboxed iframe.
    if ct in ("text/html", "image/svg+xml"):
        return (IFRAME, None)

    # Source code / structured text: shown verbatim (server-side escaped).
    if ct in ("text/javascript", "application/javascript", "application/x-javascript"):
        return ("code", "javascript")
    if ct == "text/css":
        return ("code", "css")
    if ct == "application/json":
        return ("code", "json")
    if ct in ("text/x-python", "application/x-python"):
        return ("code", "python")
    if ct in ("application/yaml", "text/yaml", "text/x-yaml"):
        return ("code", "yaml")
    if ct in ("text/markdown",):
        return ("markdown", None)

    if ct == "application/pdf":
        return ("pdf", None)

    if ct.startswith("image/"):
        pixelated = ct in (
            "image/png", "image/gif", "image/jpeg", "image/webp", "image/apng",
        )
        return ("image", "pixelated" if pixelated else "auto")
    if ct.startswith("audio/"):
        return ("audio", None)
    if ct.startswith("video/"):
        return ("video", None)
    if ct.startswith("font/"):
        return ("font", None)
    if ct.startswith("model/"):
        return ("model", None)
    if ct.startswith("text/"):
        return ("text", None)

    return ("unknown", None)


# Per-kind Content-Security-Policy for the wrapper pages, adapted from ord's
# `preview_content_security_policy`. The wrappers are self-contained (no CDN
# fetches except the 3D model viewer), so most need only same-origin access.
_CSP = {
    "audio": "default-src 'self'",
    "code": "default-src 'self'",
    "font": "default-src 'self'; style-src 'self' 'unsafe-inline'",
    "image": "default-src 'self'",
    "markdown": "default-src 'self'",
    "model": "default-src 'self'; script-src 'self' https://ajax.googleapis.com; style-src 'self' 'unsafe-inline'",
    "pdf": "default-src 'self'",
    "text": "default-src 'self'",
    "unknown": "default-src 'self'",
    "video": "default-src 'self'",
}

_MODEL_VIEWER = (
    "https://ajax.googleapis.com/ajax/libs/model-viewer/3.5.0/model-viewer.min.js"
)


def csp_for(kind: str) -> str:
    return _CSP.get(kind, "default-src 'self'")


def _doc(number: int, body: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Counter {number} preview</title>"
        "<link rel=stylesheet href=/preview.css></head>"
        f"{body}</html>"
    )


def wrapper(kind: str, number: int, content_type: str, extra: str | None,
            text: str | None = None, src: str | None = None) -> str:
    """Build the same-origin wrapper page for a non-iframe media ``kind``.

    The page loads ``/content/<number>`` via a native element. ``text`` is the
    decoded body for text/code/markdown kinds (rendered server-side, escaped).
    ``src`` overrides the content URL (used for stamp-like counters, whose
    displayable image is served decoded from ``/stamp/<number>``).
    """
    src = src or f"/content/{number}"

    if kind == "image":
        cls = "preview image" + (" pixelated" if extra == "pixelated" else "")
        body = f"<body class='{cls}'><img src={src} alt='counter {number}'></body>"
    elif kind == "audio":
        body = f"<body class='preview audio'><audio controls src={src}></audio></body>"
    elif kind == "video":
        body = f"<body class='preview video'><video controls playsinline src={src}></video></body>"
    elif kind == "pdf":
        body = (
            "<body class='preview pdf'>"
            f"<iframe title='counter {number}' src={src}></iframe></body>"
        )
    elif kind == "font":
        body = (
            "<body class='preview font'>"
            f"<style>@font-face{{font-family:inscribed;src:url({src})}}"
            ".sample{font-family:inscribed}</style>"
            "<div class=sample>ABCDEFGHIJKLM<br>nopqrstuvwxyz<br>0123456789</div>"
            "</body>"
        )
    elif kind in ("text", "code", "markdown"):
        body = f"<body class='preview text'><pre>{html.escape(text or '')}</pre></body>"
    elif kind == "model":
        body = (
            "<body class='preview model'>"
            f"<script type=module src='{_MODEL_VIEWER}'></script>"
            f"<model-viewer src={src} camera-controls auto-rotate ar "
            "shadow-intensity=1></model-viewer></body>"
        )
    else:  # unknown
        body = (
            "<body class='preview unknown'><div>"
            f"<p>{html.escape(content_type)}</p>"
            f"<a href={src} download>Download</a></div></body>"
        )

    return _doc(number, body)
