#!/usr/bin/env python3
"""
editor_server.py

Local-only backend for the visual section editor (editor.html). Lets you
reorder, edit, and add top-level <section> blocks in index.html and
index-summary.html through a browser UI, then "Build" writes new,
complete files to editor/output/ — it NEVER touches the real index.html
/ index-summary.html at the repo root. Review the output, then copy it
over the repo files yourself (or ask Claude to do it) when you're happy.

Usage:
    python3 editor/editor_server.py [port]   # default port 8000
    Then open http://localhost:<port>/ in a browser.

How it works: each source file is split into three parts —
  1. PREAMBLE  — everything up through the end of the mobile nav menu
                 (<!DOCTYPE>, <head> with all CSS, <header>, mobile menu).
                 Fixed. Not shown as an editable "section".
  2. SECTIONS  — the sibling <section>...</section> blocks that make up
                 the page body (Hero, Overview, Property, Market, ...).
                 This is the reorderable/editable/addable list.
  3. POSTAMBLE — the <footer> and the big closing <script> block, plus
                 </body></html>. Fixed.

Splitting is done with plain string scanning (depth-counted <section>/
</section> matching), not an HTML parser/formatter, so untouched content
is passed through byte-for-byte — this matters because the real files
have multi-hundred-KB single lines (inlined base64 images) that a
reformatting parser could easily mangle.
"""
import http.server
import json
import os
import re
import socketserver
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
EDITOR_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "editor.html")

SOURCES = {
    "index": os.path.join(ROOT, "index.html"),
    "summary": os.path.join(ROOT, "index-summary.html"),
}
OUTPUT_NAMES = {
    "index": "index.html",
    "summary": "index-summary.html",
}

SECTION_OPEN_RE = re.compile(r"<section\b", re.IGNORECASE)
ANY_SECTION_TAG_RE = re.compile(r"<(/?)section\b", re.IGNORECASE)


def _extend_back_for_comment(text, idx):
    """If a `<!-- ... -->` comment sits immediately (modulo whitespace)
    before text[idx:], return the index of that comment's start instead,
    so it travels with the section it's labeling. Otherwise return idx.

    Uses plain backward string scanning (not regex search) on purpose:
    a regex search anchored only at the end (\\Z) finds the *leftmost*
    valid match, which — thanks to DOTALL backtracking — can jump all
    the way back to the first comment in the whole document instead of
    the nearest one. Scanning backward from idx avoids that trap.
    """
    j = idx
    while j > 0 and text[j - 1] in " \t\n\r":
        j -= 1
    if j >= 3 and text[j - 3:j] == "-->":
        comment_start = text.rfind("<!--", 0, j)
        if comment_start != -1:
            return comment_start
    return idx


def split_sections(html):
    """Split html into (preamble, [section_html, ...], postamble).

    Finds the first top-level <section and the matching close of the
    last top-level </section> (by depth-counting all <section>/</section>
    occurrences in between), then further splits the middle into
    individual top-level section blocks the same way. Any `<!-- comment
    -->` immediately preceding a section tag travels with that section.
    """
    first = SECTION_OPEN_RE.search(html)
    if not first:
        raise ValueError("No <section> found in file")
    start = _extend_back_for_comment(html, first.start())

    # Walk forward from the first <section, depth-counting, to find where
    # the top-level section run ends (depth returns to 0 after the LAST
    # top-level </section> immediately followed by no sibling <section>).
    pos = start
    depth = 0
    last_section_end = None
    for m in ANY_SECTION_TAG_RE.finditer(html, start):
        if m.group(1) == "":  # <section
            depth += 1
        else:  # </section>
            depth -= 1
            if depth == 0:
                tag_end = html.index(">", m.end()) + 1
                last_section_end = tag_end
                # peek ahead: is the next non-whitespace a new <section ?
                rest = html[last_section_end:]
                stripped = rest.lstrip()
                if not re.match(r"<!--.*?-->\s*<section\b", stripped, re.DOTALL) and \
                   not stripped.startswith("<section"):
                    break
    if last_section_end is None:
        raise ValueError("Unbalanced <section> tags")

    preamble = html[:start]
    middle = html[start:last_section_end]
    postamble = html[last_section_end:]

    # Now split `middle` into individual top-level sections.
    sections = []
    depth = 0
    seg_start = None
    for m in ANY_SECTION_TAG_RE.finditer(middle):
        if m.group(1) == "":
            if depth == 0:
                seg_start = _extend_back_for_comment(middle, m.start())
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                tag_end = middle.index(">", m.end()) + 1
                sections.append(middle[seg_start:tag_end])
                seg_start = None

    return preamble, sections, postamble


ID_RE = re.compile(r'<section\b[^>]*\bid="([^"]*)"', re.IGNORECASE | re.DOTALL)
H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
EYEBROW_RE = re.compile(r'<span class="eyebrow"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL)
CAPTION_RE = re.compile(r"<caption>(.*?)</caption>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(s):
    return re.sub(r"\s+", " ", TAG_RE.sub("", s)).strip()


def extract_label(section_html):
    id_m = ID_RE.search(section_html[:400])
    sid = id_m.group(1) if id_m else None

    h2 = H2_RE.search(section_html)
    if h2:
        title = strip_tags(h2.group(1))
        if title:
            return sid, title[:80]

    eyebrow = EYEBROW_RE.search(section_html)
    if eyebrow:
        title = strip_tags(eyebrow.group(1))
        if title:
            return sid, title[:80]

    cap = CAPTION_RE.search(section_html)
    if cap:
        title = strip_tags(cap.group(1))
        if title:
            return sid, title[:80]

    return sid, (sid or "Untitled section")


def load_sections(file_key):
    path = SOURCES[file_key]
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    preamble, sections, postamble = split_sections(html)
    out = []
    for i, s in enumerate(sections):
        sid, label = extract_label(s)
        out.append({
            "uid": f"{file_key}-{i}-{sid or 'section'}",
            "id": sid or "",
            "label": label,
            "html": s,
        })
    return out


def get_chrome(file_key):
    """Return the <head>...</head> contents, for building preview iframes."""
    path = SOURCES[file_key]
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"<head[^>]*>(.*?)</head>", html, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else ""


NEW_SECTION_TEMPLATE = """<!-- New section -->
<section class="pad" id="new-section">
  <div class="wrap">
    <div class="section-head reveal">
      <span class="eyebrow">New section</span>
      <h2>Untitled heading.</h2>
      <p>Edit this section's HTML in the panel on the right, then Build.</p>
    </div>
  </div>
</section>"""


def build_file(file_key, sections_payload):
    path = SOURCES[file_key]
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    preamble, _orig_sections, postamble = split_sections(html)

    pieces = [s["html"].rstrip("\n") for s in sections_payload]
    middle = "\n\n".join(pieces)

    new_html = preamble.rstrip("\n") + "\n\n" + middle + "\n\n" + postamble.lstrip("\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_NAMES[file_key])
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(new_html)
    return out_path, len(new_html)


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/":
            return self._send_html(EDITOR_HTML)

        if parsed.path == "/api/sections":
            file_key = qs.get("file", ["index"])[0]
            if file_key not in SOURCES:
                return self._send_json({"error": "unknown file"}, 400)
            try:
                sections = load_sections(file_key)
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
            return self._send_json({"sections": sections})

        if parsed.path == "/api/chrome":
            file_key = qs.get("file", ["index"])[0]
            if file_key not in SOURCES:
                return self._send_json({"error": "unknown file"}, 400)
            return self._send_json({"head": get_chrome(file_key)})

        if parsed.path == "/api/new-section-template":
            return self._send_json({"html": NEW_SECTION_TEMPLATE})

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._send_json({"error": "bad json"}, 400)

        if parsed.path == "/api/build":
            file_key = payload.get("file")
            sections = payload.get("sections")
            if file_key not in SOURCES or not isinstance(sections, list):
                return self._send_json({"error": "bad request"}, 400)
            try:
                out_path, nbytes = build_file(file_key, sections)
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)
            return self._send_json({
                "ok": True,
                "path": os.path.relpath(out_path, ROOT),
                "bytes": nbytes,
            })

        self.send_response(404)
        self.end_headers()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    # A single slow/kept-open connection must not block every other request —
    # plain socketserver.TCPServer handles one request at a time, which made
    # the editor UI look like it hung whenever a browser left a connection
    # idle (e.g. favicon retry, a stalled preview iframe).
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with ThreadingHTTPServer(("0.0.0.0", port), Handler) as httpd:
        print(f"Section editor running at http://localhost:{port}/")
        print(f"Source files: {SOURCES['index']}, {SOURCES['summary']}")
        print(f"Output written to: {OUTPUT_DIR}/")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
