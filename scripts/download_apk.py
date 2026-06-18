#!/usr/bin/env python3
"""
Download a clean APK from APKMirror using curl-cffi's TLS impersonation to
bypass Cloudflare's JS challenge. Adapted from RookieEnough/Morphe-AutoBuilds'
approach (src/__init__.py uses curl_cffi.requests.Session(impersonate=...)).

Usage:
    download_apk.py <org> <repo> <version> <arch> <dpi> <outfile>

Example:
    download_apk.py google-inc youtube 20.51.39 arm64-v8a nodpi clean_youtube.apk

How APKMirror URLs work:
    Main page:    /apk/{org}/{repo}/
    Release page: /apk/{org}/{repo}/{repo}-{version-dashed}-release/
    Variants:     /apk/{org}/{repo}/{repo}-{version-dashed}-release/
                      {repo}-{version-dashed}[-{N}]-android-apk-download/
    - The first variant has no "-{N}" suffix; later variants have -2, -3, -4...
    - The single-APK variants end in ".apk" in the page text; bundle variants
      end in ".apkm" (App Bundle, not directly installable).
    - We want the standalone .apk variant whose spec matches the requested
      arch + DPI (e.g., "nodpi" + arm64-v8a present).
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

# Note: as of late 2025/2026, Chrome impersonation in curl-cffi still gets
# Cloudflare-challenged on apkmirror.com. firefox133 / safari17_0 impersonations
# are NOT. Reference project uses DEFAULT_CHROME; we override here because of
# observed failures.
IMPERSONATE = "firefox133"


def main() -> int:
    if len(sys.argv) != 7:
        print(
            f"usage: {sys.argv[0]} <org> <repo> <version> <arch> <dpi> <outfile>",
            file=sys.stderr,
        )
        return 2

    org, repo, version, want_arch, want_dpi, outfile = sys.argv[1:7]
    version_dashed = version.replace(".", "-")

    session = requests.Session(impersonate=IMPERSONATE)
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    # ----------------------------------------------------------------------
    # 1. Resolve the release page for this version.
    # ----------------------------------------------------------------------
    release_url = f"{BASE}/apk/{org}/{repo}/{repo}-{version_dashed}-release/"
    print(f"📄 Release page: {release_url}")
    r = session.get(release_url, timeout=30)
    if r.status_code != 200:
        print(f"❌ Release page returned {r.status_code}", file=sys.stderr)
        # Fallback: try to scrape from the main app page (less reliable)
        main_url = f"{BASE}/apk/{org}/{repo}/"
        r = session.get(main_url, timeout=30)
        if r.status_code != 200:
            print(f"❌ Main page also returned {r.status_code}", file=sys.stderr)
            return 1
        soup = BeautifulSoup(r.content, "html.parser")
        target_pat = re.compile(
            rf"/apk/{re.escape(org)}/{re.escape(repo)}/"
            rf"{re.escape(repo)}-{re.escape(version_dashed)}-release/"
        )
        hits = [a["href"] for a in soup.find_all("a", href=True) if target_pat.search(a["href"])]
        if not hits:
            print(f"❌ No release link found for version {version}", file=sys.stderr)
            return 1
        release_url = urljoin(BASE, sorted(set(hits))[0])
        print(f"📄 Fallback release page: {release_url}")
        r = session.get(release_url, timeout=30)
        if r.status_code != 200:
            print(f"❌ Fallback release page returned {r.status_code}", file=sys.stderr)
            return 1

    soup = BeautifulSoup(r.content, "html.parser")

    # ----------------------------------------------------------------------
    # 2. List all variant download pages for this release.
    # Each variant has its own page; URLs end in -android-apk-download/.
    # The first variant has no "-{N}" suffix; others have -2, -3, -4, ...
    # ----------------------------------------------------------------------
    variant_re = re.compile(
        rf"/apk/{re.escape(org)}/{re.escape(repo)}/"
        rf"{re.escape(repo)}-{re.escape(version_dashed)}-release/"
        rf"{re.escape(repo)}-{re.escape(version_dashed)}(-(?P<n>\d+))?-android-apk-download/"
        rf"(?:#.*)?$"
    )
    seen = set()
    variants = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = variant_re.match(href)
        if m and href not in seen:
            seen.add(href)
            variant_n = int(m.group("n")) if m.group("n") else 1
            variants.append((variant_n, urljoin(BASE, href)))

    if not variants:
        print(f"❌ No variant download pages found on release page", file=sys.stderr)
        return 1
    variants.sort()
    print(f"🔍 Found {len(variants)} variant page(s): {[n for n, _ in variants]}")

    # ----------------------------------------------------------------------
    # 3. For each variant, fetch its page and check arch + DPI.
    # We prefer standalone .apk (singular) over .apkm bundle, since the
    # morphe-cli patcher expects a single APK.
    # ----------------------------------------------------------------------
    arch_aliases = {
        "arm64-v8a": ["arm64-v8a", "arm64"],
        "armeabi-v7a": ["armeabi-v7a", "armv7", "armeabi"],
        "x86": ["x86"],
        "x86_64": ["x86_64", "x86-64"],
        "universal": ["universal", "noarch"],
    }
    arch_match = arch_aliases.get(want_arch, [want_arch.lower()])
    dpi_match = want_dpi.lower()  # "nodpi", "240dpi", etc.

    scored = []  # (score, variant_n, variant_url, page)
    for n, url in variants:
        rv = session.get(url, timeout=30)
        if rv.status_code != 200:
            print(f"  variant {n}: HTTP {rv.status_code}, skipping")
            continue
        vsoup = BeautifulSoup(rv.content, "html.parser")
        text_blob = " ".join(vsoup.stripped_strings)
        # .apk vs .apkm — we want .apk (single APK, installable)
        is_single_apk = (
            "apkmirror.com.apk" in text_blob and "apkmirror.com.apkm" not in text_blob
        ) or (
            # title typically mentions "APK Download" for single APK and
            # "APK Bundle Download" for .apkm
            "apk download" in text_blob.lower()
            and "apk bundle download" not in text_blob.lower()
        )
        # Arch present in spec?
        arch_ok = any(tok in text_blob.lower() for tok in arch_match)
        # DPI present in spec?
        dpi_ok = dpi_match in text_blob.lower() or (
            dpi_match == "nodpi" and "nodpi" in text_blob.lower()
        )
        # Score: prefer single APK, then arch match, then dpi match
        score = (3 if is_single_apk else 0) + (2 if arch_ok else 0) + (1 if dpi_ok else 0)
        print(f"  variant {n}: score={score} apk={is_single_apk} arch={arch_ok} dpi={dpi_ok}")
        scored.append((score, n, url, vsoup))

    if not scored:
        print(f"❌ Could not load any variant page", file=sys.stderr)
        return 1

    scored.sort(key=lambda x: (-x[0], x[1]))  # highest score, lowest variant #
    _, best_n, best_url, best_soup = scored[0]
    print(f"🎯 Picked variant {best_n}: {best_url}")

    # ----------------------------------------------------------------------
    # 4. On the variant page, find the actual download link/button.
    # APKMirror's variant page has either a direct link or a button that
    # triggers a redirect to the actual download URL.
    # ----------------------------------------------------------------------
    download_url = None

    # Look for direct APK/APKM download link
    for a in best_soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/wp-content/") or "download.php" in href or href.endswith((".apk", ".apkm")):
            download_url = urljoin(BASE, href)
            break

    # Look for a download button (data-url, data-href, onclick with location)
    if not download_url:
        btn = best_soup.find(["a", "button"], attrs={"data-url": True})
        if btn:
            download_url = urljoin(BASE, btn["data-url"])

    if not download_url:
        btn = best_soup.find("a", id="download") or best_soup.find(
            "a", class_=re.compile(r"download", re.I)
        )
        if btn and btn.get("href"):
            download_url = urljoin(BASE, btn["href"])

    if not download_url:
        print(f"❌ No download link/button found on variant page", file=sys.stderr)
        # Print some diagnostics
        print("--- relevant anchors (first 10) ---")
        for a in best_soup.find_all("a", href=True)[:10]:
            print(f"  {a.get('href')[:120]!r} text={a.get_text(strip=True)[:60]!r}")
        return 1

    print(f"⬇️  Downloading from: {download_url}")

    # ----------------------------------------------------------------------
    # 5. Stream the file. APKMirror's final URL often issues a 302 to a
    # CDN; the curl_cffi session follows redirects automatically.
    # ----------------------------------------------------------------------
    with session.get(download_url, stream=True, timeout=120, allow_redirects=True) as resp:
        if resp.status_code != 200:
            print(f"❌ Final download returned {resp.status_code}", file=sys.stderr)
            return 1
        ct = resp.headers.get("content-type", "")
        if "html" in ct.lower():
            print(f"❌ Final URL returned HTML instead of APK (content-type={ct})", file=sys.stderr)
            print("    Possibly Cloudflare-challenged. Last URL:", resp.url)
            return 1
        total = int(resp.headers.get("content-length", 0))
        written = 0
        with open(outfile, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
        print(f"✅ Downloaded {written:,} bytes ({written:,}/{total:, if total else '?'}) to {outfile}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
