#!/usr/bin/env python3
"""Find websites that serve their images from Cloudinary, from a ranked list of domains.

Fetches each site's home page once (curl, honest user agent, 12 s timeout, 3 MB cap)
and looks for Cloudinary delivery URLs:

  res.cloudinary.com/<cloud>/image/upload/...   any plan
  <cloud>-res.cloudinary.com/image/upload/...   private CDN (Advanced plan and up)
  img.example.com/image/upload/w_400,c_fill/... custom domain (Advanced plan and up), a guess

Writes one JSON line per site that matches. Nothing else is stored.

    python3 cloudinary_scan.py --list top-1m.csv --count 10000 --out scan.jsonl

The list is Tranco's format: "rank,domain" per line. --resume skips domains already in --out.
Set your own user agent with --user-agent or CLOUDINARY_SCAN_UA, so site owners can reach you.
"""

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time

DEFAULT_UA = "Mozilla/5.0 (compatible; cloudinary-scan/1.0; +https://github.com/taktekhq/cloudinary-scan)"
UA = os.environ.get("CLOUDINARY_SCAN_UA") or DEFAULT_UA
TAIL = r"""([^"'\s<>)\\]*)"""
RES = re.compile(r"res\.cloudinary\.com/([A-Za-z0-9_-]+)/(image|video|raw)/(upload|fetch|private|authenticated)/" + TAIL)
PRIVATE = re.compile(r"-res\.cloudinary\.com/(image|video|raw)/(upload|fetch|private|authenticated)/" + TAIL)
NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
CNAME = re.compile(
    r"https?://([A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,})/(?:[A-Za-z0-9_-]{1,100}/)?(image|video)/(upload|fetch)/"
    r"((?:[a-z]{1,3}_[A-Za-z0-9.:_-]{1,300}[,/]){1,40})" + TAIL
)
TRANSFORM = re.compile(r"^((?:[a-z]{1,3}_[A-Za-z0-9.:_-]+[,/])+)")


def cloud_before(html, end):
    """The cloud name just before index `end` (up to 100 name characters), found by walking back."""
    i = end
    while i > 0 and end - i < 100 and html[i - 1] in NAME_CHARS:
        i -= 1
    return i, html[i:end]


def transformation(tail):
    """The transformation segments at the start of a delivery path, or '' if none."""
    m = TRANSFORM.match(tail)
    return m.group(1).rstrip("/") if m else ""


def parse(html):
    """Cloudinary usage found in one page's HTML, or None when there is none.

    Each regex only runs when the literal text it needs is on the page. `re` holds the GIL, so a
    pattern that backtracks over a long run of letters (an inline base64 blob, say) would freeze
    every worker thread; the substring checks and the bounded repeats above keep that from happening.
    """
    html = html.replace("\\/", "/").replace("&amp;", "&")
    clouds, urls, transforms, private, cnames = {}, set(), {}, set(), set()
    if "res.cloudinary.com/" in html:
        for m in RES.finditer(html):
            cloud, tail = m.group(1), m.group(4)
            clouds[cloud] = clouds.get(cloud, 0) + 1
            urls.add(m.group(0))
            t = transformation(tail)
            transforms[t] = transforms.get(t, 0) + 1
        if "-res.cloudinary.com/" in html:
            for m in PRIVATE.finditer(html):
                begin, cloud = cloud_before(html, m.start())
                if not cloud:
                    continue
                tail = m.group(3)
                clouds[cloud] = clouds.get(cloud, 0) + 1
                private.add(cloud)
                urls.add(html[begin:m.end()])
                t = transformation(tail)
                transforms[t] = transforms.get(t, 0) + 1
    cname_matches = list(CNAME.finditer(html)) if ("/upload/" in html or "/fetch/" in html) else []
    for m in cname_matches:
        host = m.group(1).lower()
        if host.endswith("cloudinary.com"):
            continue
        cnames.add(host)
        urls.add(m.group(0))
        t = m.group(4).rstrip("/")
        transforms[t] = transforms.get(t, 0) + 1
    if not urls:
        return None
    text = " ".join(transforms)
    return {
        "clouds": clouds,
        "private_cdn": sorted(private),
        "custom_hosts": sorted(cnames),
        "refs": sum(clouds.values()) + len(cname_matches),
        "unique_urls": len(urls),
        "transforms": len(transforms),
        "f_auto": "f_auto" in text,
        "q_auto": "q_auto" in text,
        "top_transforms": sorted(transforms, key=transforms.get, reverse=True)[:8],
        "sample": sorted(urls)[:3],
        "_urls": sorted(urls),
    }


def image_bytes(url, timeout):
    """Content-Length of one delivered image, asking for the formats a browser accepts."""
    if not url.startswith("http"):
        url = "https://" + url
    cmd = ["curl", "-sIL", "--max-time", str(timeout), "-A", UA,
           "-H", "Accept: image/avif,image/webp,image/apng,image/*,*/*;q=0.8", url]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout + 5).stdout.decode("latin-1")
    except subprocess.TimeoutExpired:
        return None
    sizes = re.findall(r"(?im)^content-length:\s*(\d+)", out)
    return int(sizes[-1]) if sizes else None


def measure(found, limit, timeout):
    """Average and summed bytes of the page's Cloudinary images (first `limit` measured)."""
    urls = [u for u in found["_urls"] if "/image/" in u][:limit]
    sizes = [s for s in (image_bytes(u, timeout) for u in urls) if s]
    if not sizes:
        return {"measured": 0}
    avg = sum(sizes) / len(sizes)
    images = sum(1 for u in found["_urls"] if "/image/" in u)
    return {"measured": len(sizes), "avg_bytes": round(avg), "page_image_bytes": round(avg * images)}


def fetch(url, timeout):
    cmd = ["curl", "-sL", "--compressed", "--max-time", str(timeout), "--max-filesize", "3000000",
           "-A", UA, "-H", "Accept: text/html", "-w", "\n%{url_effective}", url]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout + 5).stdout
    except subprocess.TimeoutExpired:
        return None, None
    text = out.decode("utf-8", errors="replace")
    body, _, final = text.rpartition("\n")
    return (body, final) if body else (None, None)


def scan(rank, domain, timeout, measure_limit=0):
    for url in (f"https://{domain}/", f"https://www.{domain}/"):
        body, final = fetch(url, timeout)
        if body:
            found = parse(body)
            if not found:
                return False
            if measure_limit:
                found.update(measure(found, measure_limit, timeout))
            found.pop("_urls")
            return dict(rank=rank, domain=domain, final_url=final, **found)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", required=True, help="rank,domain CSV (Tranco format)")
    ap.add_argument("--start", type=int, default=0, help="skip this many rows first")
    ap.add_argument("--count", type=int, default=10000, help="rows to scan")
    ap.add_argument("--workers", type=int, default=96)
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--measure", type=int, default=0, metavar="N",
                    help="also HEAD up to N Cloudinary images per site to estimate bytes per page view")
    ap.add_argument("--user-agent", default=None, metavar="UA",
                    help="user agent to send (default: $CLOUDINARY_SCAN_UA, else a string that points to this repo)")
    ap.add_argument("--out", required=True, help="JSON lines, one per matching site")
    ap.add_argument("--resume", action="store_true", help="skip domains already in --out")
    a = ap.parse_args()
    if a.user_agent:
        global UA
        UA = a.user_agent

    done = set()
    if a.resume:
        try:
            with open(a.out) as f:
                done = {json.loads(line)["domain"] for line in f if line.strip()}
        except FileNotFoundError:
            pass
    rows = []
    with open(a.list) as f:
        for i, line in enumerate(f):
            if i < a.start:
                continue
            if len(rows) >= a.count:
                break
            rank, _, domain = line.strip().partition(",")
            if domain and domain not in done:
                rows.append((int(rank), domain))

    lock, stats, t0 = threading.Lock(), {"done": 0, "hit": 0, "fail": 0}, time.time()
    with open(a.out, "a") as out, concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
        futures = {pool.submit(scan, r, d, a.timeout, a.measure): d for r, d in rows}
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            with lock:
                stats["done"] += 1
                if res:
                    stats["hit"] += 1
                    out.write(json.dumps(res) + "\n")
                    out.flush()
                elif res is None:
                    stats["fail"] += 1
                if stats["done"] % 1000 == 0:
                    rate = stats["done"] / (time.time() - t0)
                    print(f"{stats['done']}/{len(rows)} scanned, {stats['hit']} use Cloudinary, "
                          f"{stats['fail']} unreachable, {rate:.0f}/s", file=sys.stderr, flush=True)
    print(json.dumps(stats), file=sys.stderr)


if __name__ == "__main__":
    main()
