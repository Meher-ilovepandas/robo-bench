"""
Standalone tool: download the entire VEX AIM Python API reference to a
single local text file suitable for feeding verbatim to an LLM prompt.

Usage:
    python download_api.py                        # writes vex_api.txt
    python download_api.py --out my_api.txt       # custom output path
    python download_api.py --url <custom-index>   # custom start URL

The scraper:
  1. Fetches the index page.
  2. Discovers all relative links on the page (same domain/path prefix).
  3. Fetches each linked page exactly once.
  4. Strips HTML, concatenates readable text with section separators.
  5. Writes the combined content to the output file.
"""
from __future__ import annotations

import argparse
import sys
import textwrap
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as cf_requests
from bs4 import BeautifulSoup

DEFAULT_INDEX = "https://api.vex.com/aim/home/python/index.html"
DEFAULT_OUT = "vex_api.txt"

CF_IMPERSONATE = "chrome124"   # curl-cffi Chrome fingerprint — bypasses Cloudflare


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _same_site(base: str, href: str) -> bool:
    """True if href shares the same netloc and path prefix as base."""
    base_p = urlparse(base)
    href_p = urlparse(urljoin(base, href))
    return (
        href_p.netloc == base_p.netloc
        and href_p.path.startswith(base_p.path.rsplit("/", 1)[0])
    )


def _extract_text(html: str, url: str) -> str:
    """Convert HTML page to clean readable text."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove script / style noise
    for tag in soup(["script", "style", "nav", "footer", "head"]):
        tag.decompose()

    # Try to find main content area
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", {"role": "main"})
        or soup.find("div", class_=lambda c: c and "content" in c.lower())
        or soup.body
        or soup
    )

    # Preserve code blocks literally
    for code in main.find_all(["code", "pre"]):
        code.string = "\n" + (code.get_text() or "") + "\n"

    lines = []
    for elem in main.descendants:
        if not hasattr(elem, "name"):
            continue
        if elem.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(elem.name[1])
            prefix = "#" * level
            lines.append(f"\n{prefix} {elem.get_text(strip=True)}\n")
        elif elem.name == "p":
            txt = elem.get_text(" ", strip=True)
            if txt:
                lines.append(txt + "\n")
        elif elem.name in ("pre", "code"):
            lines.append(elem.get_text())
        elif elem.name == "li":
            txt = elem.get_text(" ", strip=True)
            if txt:
                lines.append("  - " + txt)

    return "\n".join(lines)


def _discover_links(html: str, base_url: str) -> list[str]:
    """Return absolute URLs for same-site links found on the page."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].split("#")[0]  # strip fragments
        if not href or href.startswith("javascript:"):
            continue
        abs_url = urljoin(base_url, href)
        if _same_site(base_url, href) and abs_url not in seen:
            seen.add(abs_url)
            urls.append(abs_url)
    return urls


# ---------------------------------------------------------------------------
# Main download logic
# ---------------------------------------------------------------------------

def download(index_url: str, out_path: Path, delay: float = 0.3) -> None:
    visited: set[str] = set()
    queue: list[str] = [index_url]
    pages: list[tuple[str, str]] = []  # (url, text)

    print(f"Fetching VEX AIM Python API from: {index_url}")

    session = cf_requests.Session()
    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            print(f"  GET {url}")
            resp = session.get(url, impersonate=CF_IMPERSONATE,
                               timeout=20, allow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            print(f"  WARNING: could not fetch {url}: {exc}")
            continue

        html = resp.text
        text = _extract_text(html, url)
        pages.append((url, text))

        # Discover more links from this page
        for link in _discover_links(html, url):
            if link not in visited:
                queue.append(link)

        time.sleep(delay)

    # Combine all pages
    parts = [
        "VEX AIM Python API Reference",
        "=" * 60,
        f"Source: {index_url}",
        f"Pages fetched: {len(pages)}",
        "=" * 60,
        "",
    ]
    for url, text in pages:
        parts.append(f"\n{'='*60}")
        parts.append(f"SOURCE: {url}")
        parts.append("=" * 60)
        parts.append(text.strip())
        parts.append("")

    combined = "\n".join(parts)
    out_path.write_text(combined, encoding="utf-8")
    print(f"\nSaved {len(combined):,} characters to: {out_path}")
    print(f"Pages: {len(pages)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download VEX AIM Python API reference to a single text file."
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_INDEX,
        help=f"Index URL to start from (default: {DEFAULT_INDEX})",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help=f"Output file path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Delay between requests in seconds (default: 0.3)",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    download(args.url, out_path, delay=args.delay)


if __name__ == "__main__":
    main()
