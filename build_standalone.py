#!/usr/bin/env python3
"""
build_standalone.py

Regenerates the self-contained, single-file version of the site by
base64-inlining every image referenced in index.html (assets/*.jpg).

Usage:
    python3 build_standalone.py

Input:   index.html               (references src="assets/filename.jpg")
Output:  index_standalone.html    (all images inlined as data URIs)

Why this exists: the editable source (index.html) keeps images as
separate files in assets/ so they're easy to swap and the file stays
small in an editor/diff. The standalone build is what actually gets
delivered to Ivan / uploaded / opened directly in a browser, since it
has no external file dependencies at all (single file, works from
anywhere, safe to email or drag into a chat).

Re-run this any time index.html or any file in assets/ changes.
"""
import re
import os
import base64

SRC = "index.html"
OUT = "index_standalone.html"
ASSETS_DIR = "assets"

def replace_img(match):
    path = match.group(1)
    filename = os.path.basename(path)
    full_path = os.path.join(ASSETS_DIR, filename)
    ext = os.path.splitext(filename)[1].lower().replace(".", "")
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    with open(full_path, "rb") as imgf:
        b64 = base64.b64encode(imgf.read()).decode("ascii")
    return f'src="data:image/{mime};base64,{b64}"'

def main():
    with open(SRC, "r") as f:
        content = f.read()

    new_content = re.sub(r'src="assets/([a-zA-Z0-9_.\-]+)"', replace_img, content)

    with open(OUT, "w") as f:
        f.write(new_content)

    # quick sanity checks
    remaining = len(re.findall(r'src="assets/', new_content))
    inlined = len(re.findall(r'src="data:image', new_content))
    print(f"Wrote {OUT} ({len(new_content):,} bytes)")
    print(f"  images inlined: {inlined}")
    print(f"  remaining assets/ references (should be 0): {remaining}")
    if remaining:
        print("  WARNING: some images were not inlined — check filenames in assets/")

if __name__ == "__main__":
    main()
