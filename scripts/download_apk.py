#!/usr/bin/env python3
"""
Download a clean APK from APKMirror using curl-cffi's TLS impersonation to
bypass Cloudflare's JS challenge.

Strategy mirrors the one used in RookieEnough/Morphe-AutoBuilds (their
`src/__init__.py` does `requests.Session(impersonate=DEFAULT_CHROME)`).
We impersonate firefox133 because in our testing, current Chrome profiles are
still Cloudflare-challenged on apkmirror.com while firefox133 is not.

Flow (APKMirror 3-step download dance):
  1. Release page:  /apk/{org}/{repo}/{repo}-{version-dashed}-release/
       — find a row whose text contains "APK", arch, and dpi strings.
       — follow the row's `<a class="accent_color">` to get the variant page.
  2. Variant page:  .../{repo}-{version-dashed}[-{N}]-android-apk-download/
       — find `<a class="downloadButton">` and follow it.
  3. Final page:    /wp-content/themes/APKMirror/apk-new.php?id=...
       — find `<a id="download-link">` whose href is the actual download URL.
  4. Stream the URL returned by step 3 to a file.

Usage:
    download_apk.py <org> <repo> <version> <arch> <dpi> <outfile>

Example:
    download_apk.py google-inc youtube 20.51.39 arm64-v8a nodpi clean_youtube.apk
"""
import re
import sys
from urllib.parse import urljoin
from curl_cffi import requests
from bs4 import BeautifulSoup

BASE = "https://www.apkmirror.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
IMPERSONATE = "firefox133"  # see module docstring for why


def main() -> int:
    if len(sys.argv) != 7:
        print(
            f"usage: {sys.argv[0]} <org> <repo> <version> <arch> <dpi> <outfile>",
            file=sys.stderr,
        )
        return 2

    org, repo, version, want_arch, want_dpi, outfile = sys.argv[1:7]
    version_dashed = version.replace(".", "-")

    import time
    session = requests.Session(impersonate=IMPERSONATE)
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    def get(url):
        # Small delay between requests — APKMirror rate-limits aggressively
        # even with curl-cffi impersonation. In CI on fresh IPs this rarely
        # triggers, but the delays keep us under any per-IP throttling.
        time.sleep(1.0)
        return session.get(url, timeout=30)

    # ----------------------------------------------------------------------
    # Step 1: Release page → variant page.
    # ----------------------------------------------------------------------
    release_url = f"{BASE}/apk/{org}/{repo}/{repo}-{version_dashed}-release/"
    print(f"📄 Release: {release_url}")
    r = get(release_url)
    if r.status_code != 200:
        print(f"❌ Release page returned {r.status_code}", file=sys.stderr)
        return 1
    release_soup = BeautifulSoup(r.content, "html.parser")

    # APKMirror lists variants as rows in a table; each row is a
    # `<div class="table-row headerFont">` and contains text like:
    # "APK   arm64-v8a + armeabi-v7a   213-640dpi   Android 9.0+"
    rows = release_soup.find_all("div", class_="table-row headerFont")
    # The download-button href we want has class "accent_color"
    variant_page = None
    matched_row_text = None
    for row in rows:
        text = row.get_text(" ", strip=True)
        if "APK" not in text:
            continue
        if want_arch not in text:
            continue
        if want_dpi not in text:
            continue
        link = row.find("a", class_="accent_color")
        if link and link.get("href"):
            variant_page = urljoin(BASE, link["href"])
            matched_row_text = text
            break

    # If no row matched exactly (some YouTube variants list "universal" + nodpi
    # rather than "arm64-v8a + nodpi"), relax the arch check to allow any row
    # whose arch text overlaps with ours. Fallback: first row matching type+dpi.
    if not variant_page:
        for row in rows:
            text = row.get_text(" ", strip=True)
            if "APK" not in text:
                continue
            if want_dpi not in text:
                continue
            link = row.find("a", class_="accent_color")
            if link and link.get("href"):
                variant_page = urljoin(BASE, link["href"])
                matched_row_text = text
                break

    if not variant_page:
        print(f"❌ No variant row matches arch={want_arch!r} dpi={want_dpi!r}", file=sys.stderr)
        print("    Available rows:")
        for row in rows:
            print(f"      {row.get_text(' ', strip=True)[:120]}")
        return 1
    print(f"🎯 Variant row: {matched_row_text!r}")
    print(f"📄 Variant page: {variant_page}")

    # ----------------------------------------------------------------------
    # Step 2: Variant page → final download page.
    # ----------------------------------------------------------------------
    rv = get(variant_page)
    if rv.status_code != 200:
        print(f"❌ Variant page returned {rv.status_code}", file=sys.stderr)
        return 1
    variant_soup = BeautifulSoup(rv.content, "html.parser")

    download_button = variant_soup.find("a", class_="downloadButton")
    if not download_button or not download_button.get("href"):
        print(f"❌ No downloadButton on variant page", file=sys.stderr)
        return 1
    download_page = urljoin(BASE, download_button["href"])
    print(f"📄 Download page: {download_page}")

    rd = get(download_page)
    if rd.status_code != 200:
        print(f"❌ Download page returned {rd.status_code}", file=sys.stderr)
        return 1
    download_soup = BeautifulSoup(rd.content, "html.parser")

    # ----------------------------------------------------------------------
    # Step 3: Final page → the actual APK URL (typically a CDN link).
    # ----------------------------------------------------------------------
    link = download_soup.find("a", id="download-link")
    if not link or not link.get("href"):
        print(f"❌ No download-link anchor on final page", file=sys.stderr)
        return 1
    final_url = urljoin(BASE, link["href"])
    print(f"⬇️  Final URL: {final_url}")

    # ----------------------------------------------------------------------
    # Step 4: Stream the file.
    # NOTE: curl_cffi.requests.Response does NOT support `with` as a
    # context manager, so we use try/finally for explicit cleanup.
    # ----------------------------------------------------------------------
    time.sleep(1.0)
    resp = session.get(final_url, stream=True, timeout=180, allow_redirects=True)
    try:
        if resp.status_code != 200:
            print(f"❌ Final download returned {resp.status_code}", file=sys.stderr)
            return 1
        ct = resp.headers.get("content-type", "")
        if "html" in ct.lower():
            print(f"❌ Final URL returned HTML instead of binary (content-type={ct})", file=sys.stderr)
            print(f"    Last URL: {resp.url}")
            return 1
        total = int(resp.headers.get("content-length", 0))
        written = 0
        with open(outfile, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
        print(f"✅ Downloaded {written:,} bytes to {outfile}")
    finally:
        resp.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
