#!/usr/bin/env python3
"""Back up the images the Locke Incorporated Wix site uses.

What it does, each run:
  1. Reads the list of Wix image IDs (backup/wix-media-ids.txt). If SITE_REPO_DIR
     points at a checkout of the site code, it rescans that code first and adds
     any new image IDs to the list, so new images are picked up automatically.
  2. Downloads every image not yet backed up from Wix's image servers.
  3. If the exact same file is already in this repo (for example under photos/),
     it just records where it is. Otherwise it saves it to backup/wix/<id>.
  4. Writes backup/wix-manifest.csv: one row per Wix image with the file in this
     repo that holds a copy. That is the map for rewriting links if the site
     ever leaves Wix.
  5. Checks every supplier image link the site loads directly from other sites
     and records which ones still work (backup/supplier-image-links.csv).
  6. Writes a short summary to backup/REPORT.md.

Safety limit: if the new images would add more than --max-new-gb to the repo,
nothing is saved; the report says how big they are so a better home
(such as Cloudflare R2) can be chosen.

Standard library only. Run from the repository root.
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BACKUP_DIR = "backup"
IDS_FILE = os.path.join(BACKUP_DIR, "wix-media-ids.txt")
SUPPLIER_FILE = os.path.join(BACKUP_DIR, "supplier-image-urls.txt")
MANIFEST = os.path.join(BACKUP_DIR, "wix-manifest.csv")
SUPPLIER_REPORT = os.path.join(BACKUP_DIR, "supplier-image-links.csv")
REPORT = os.path.join(BACKUP_DIR, "REPORT.md")
WIX_DIR = os.path.join(BACKUP_DIR, "wix")
WIX_ORIGINAL = "https://static.wixstatic.com/media/{}"

WIX_ID_RE = re.compile(r"[0-9a-z]{6}_[0-9a-f]{32}(?:~mv2)?(?:_d_[0-9_s]+)?\.(?:jpg|jpeg|png|webp)\b")
SUPPLIER_RE = re.compile(r"https?://[^\"' )`<>]+\.(?:jpg|jpeg|png|webp|gif|avif)(?:\?[^\"' )`<>]*)?")
SUPPLIER_SKIP = re.compile(r"wixstatic|usrfiles|githubusercontent|github\.com|\$\{|\*")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BATCH_BYTES = 400 * 1024 * 1024  # keep each push well under GitHub's limits
MAX_FILE_BYTES = 95 * 1024 * 1024  # GitHub rejects files over 100 MB
MANIFEST_FIELDS = ["wix_id", "wix_url", "status", "repo_path", "bytes", "checked_at"]


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def scan_site_code(site_dir):
    """Return (wix_ids, supplier_urls) found in the site's source code."""
    ids, urls = set(), set()
    root = os.path.join(site_dir, "src") if os.path.isdir(os.path.join(site_dir, "src")) else site_dir
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.endswith((".js", ".json", ".css", ".html", ".md")):
                continue
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            ids.update(m.group(0) for m in WIX_ID_RE.finditer(text))
            urls.update(u for u in (m.group(0) for m in SUPPLIER_RE.finditer(text))
                        if not SUPPLIER_SKIP.search(u))
            urls.update(u for u in decode_gallery(text) if not SUPPLIER_SKIP.search(u))
    return ids, urls


GALLERY_HOSTS_RE = re.compile(r"export const HOSTS\s*=\s*\[(.*?)\];", re.S)
GALLERY_ENTRY_RE = re.compile(r'"(\d+):([^"]+)"')


def decode_gallery(text):
    """Supplier gallery files store photos as "n:path" with n indexing HOSTS
    (a host containing "*" is a template the path fills). Rebuild the links."""
    m = GALLERY_HOSTS_RE.search(text)
    if not m:
        return set()
    hosts = re.findall(r'"([^"]*)"', m.group(1))
    out = set()
    for n, path in GALLERY_ENTRY_RE.findall(text[m.end():]):
        n = int(n)
        if n >= len(hosts):
            continue
        host = hosts[n]
        url = (host.replace("*", path) if "*" in host else host + path).split("#")[0]
        if re.match(r"^https://[^\s\"'<>*]+$", url):
            out.add(url)
    return out


def repo_blobs():
    """Map git blob SHA -> path for every file already committed in this repo."""
    out = subprocess.run(["git", "ls-tree", "-r", "HEAD"], capture_output=True,
                         text=True, check=True).stdout
    blobs = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        _, kind, sha = meta.split()
        if kind == "blob":
            blobs.setdefault(sha, path)
    return blobs


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def fetch(url, tries=3, timeout=60):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (403, 404, 410):
                break
        except Exception as e:  # network hiccup: retry
            last = type(e).__name__
        time.sleep(2 * (attempt + 1))
    return None, last


def load_manifest():
    rows = {}
    if os.path.exists(MANIFEST):
        with open(MANIFEST, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[row["wix_id"]] = row
    return rows


def write_manifest(rows):
    with open(MANIFEST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        for key in sorted(rows):
            w.writerow({k: rows[key].get(k, "") for k in MANIFEST_FIELDS})


def git(*args):
    subprocess.run(["git", *args], check=True)


def commit_and_push(paths, message, push):
    if not paths:
        return
    for i in range(0, len(paths), 500):  # avoid over-long command lines
        git("add", "--", *paths[i:i + 500])
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    git("commit", "-q", "-m", message)
    if push:
        for attempt in range(3):
            if subprocess.run(["git", "push", "-q", "origin", "HEAD:main"]).returncode == 0:
                return
            subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"])
        sys.exit("Could not push to main after 3 tries.")


def check_supplier_link(url):
    for method in ("HEAD", "GET"):
        try:
            headers = {"User-Agent": UA}
            if method == "GET":
                headers["Range"] = "bytes=0-0"
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, "ok"
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 501):
                continue  # some servers refuse HEAD; try a tiny GET
            if e.code in (404, 410):
                return e.code, "broken"
            if e.code in (401, 403, 429):
                return e.code, "blocked_check"  # server refused the checker, not necessarily broken
            return e.code, "error"
        except Exception as e:
            return "", f"error:{type(e).__name__}"
    return "", "error"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new-gb", type=float, default=1.5)
    ap.add_argument("--commit", action="store_true", help="commit and push results")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()
    max_new = int(args.max_new_gb * 1024 ** 3)

    # 1. Image list, refreshed from the site code when it is available.
    ids = set(read_lines(IDS_FILE))
    supplier_urls = set(read_lines(SUPPLIER_FILE))
    site_dir = os.environ.get("SITE_REPO_DIR", "").strip()
    if site_dir and os.path.isdir(site_dir):
        found_ids, found_urls = scan_site_code(site_dir)
        print(f"Site code scan: {len(found_ids)} Wix images, {len(found_urls)} supplier links")
        ids |= found_ids
        supplier_urls = found_urls or supplier_urls
        write_lines(IDS_FILE, sorted(ids))
        write_lines(SUPPLIER_FILE, sorted(supplier_urls))

    # 2-3. Download what isn't backed up yet.
    rows = load_manifest()
    done = {k for k, r in rows.items() if r.get("status") in ("in_repo", "backed_up")}
    pending = sorted(ids - done)
    print(f"{len(ids)} Wix images listed, {len(done)} already backed up, {len(pending)} to check")

    blobs = repo_blobs()
    staging = tempfile.mkdtemp(prefix="wix-img-")
    new_files = []  # (wix_id, staged_path, size)

    def work(wix_id):
        url = WIX_ORIGINAL.format(wix_id)
        data, err = fetch(url)
        row = {"wix_id": wix_id, "wix_url": url, "checked_at": now()}
        if data is None:
            row.update(status="failed", repo_path="", bytes="")
            row["status"] = f"failed ({err})"
            return row, None
        row["bytes"] = str(len(data))
        existing = blobs.get(git_blob_sha(data))
        if existing:
            row.update(status="in_repo", repo_path=existing)
            return row, None
        if len(data) > MAX_FILE_BYTES:
            row.update(status="too_large_for_github", repo_path="")
            return row, None
        staged = os.path.join(staging, wix_id)
        with open(staged, "wb") as f:
            f.write(data)
        row.update(status="new", repo_path=os.path.join(WIX_DIR, wix_id))
        return row, (wix_id, staged, len(data))

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for n, (row, new) in enumerate(pool.map(work, pending), 1):
            rows[row["wix_id"]] = row
            if new:
                new_files.append(new)
            if n % 500 == 0:
                print(f"  checked {n}/{len(pending)}")

    new_bytes = sum(s for _, _, s in new_files)
    over_limit = new_bytes > max_new
    pushed_batches = 0

    # Save new files, in batches so each push stays small.
    if new_files and not over_limit:
        os.makedirs(WIX_DIR, exist_ok=True)
        batch, batch_bytes = [], 0
        for wix_id, staged, size in new_files:
            dest = os.path.join(WIX_DIR, wix_id)
            shutil.move(staged, dest)
            rows[wix_id]["status"] = "backed_up"
            batch.append(dest)
            batch_bytes += size
            if batch_bytes >= BATCH_BYTES:
                if args.commit:
                    pushed_batches += 1
                    commit_and_push(batch, f"Image backup: {len(batch)} Wix images "
                                    f"({batch_bytes / 1e6:.0f} MB)", push=True)
                batch, batch_bytes = [], 0
        if batch and args.commit:
            pushed_batches += 1
            commit_and_push(batch, f"Image backup: {len(batch)} Wix images "
                            f"({batch_bytes / 1e6:.0f} MB)", push=True)
    elif over_limit:
        for wix_id, _, _ in new_files:
            rows[wix_id]["status"] = "not_saved_over_limit"
            rows[wix_id]["repo_path"] = ""
    shutil.rmtree(staging, ignore_errors=True)
    write_manifest(rows)

    # 5. Supplier links: check only (these are other companies' images).
    supplier_rows = []
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        for url, (code, status) in zip(sorted(supplier_urls),
                                       pool.map(check_supplier_link, sorted(supplier_urls))):
            supplier_rows.append({"url": url, "status": status, "http_code": code,
                                  "checked_at": now()})
    with open(SUPPLIER_REPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["url", "status", "http_code", "checked_at"])
        w.writeheader()
        w.writerows(supplier_rows)

    # 6. Summary.
    counts = {}
    for r in rows.values():
        key = r["status"].split(" ")[0]
        counts[key] = counts.get(key, 0) + 1
    sup = {}
    for r in supplier_rows:
        key = r["status"].split(":")[0]
        sup[key] = sup.get(key, 0) + 1
    covered = counts.get("in_repo", 0) + counts.get("backed_up", 0)
    lines = [
        "# Image backup report",
        "",
        f"Last run: {now()}",
        "",
        "## Wix images used by the site",
        "",
        f"- Listed: {len(ids)}",
        f"- Have a copy in this repo: {covered}",
        f"  - Already here (same file, e.g. under photos/): {counts.get('in_repo', 0)}",
        f"  - Saved to backup/wix/: {counts.get('backed_up', 0)}",
        f"- Could not download: {counts.get('failed', 0)}",
        f"- Over GitHub's 100 MB file limit: {counts.get('too_large_for_github', 0)}",
        f"- New this run: {len(new_files)} files, {new_bytes / 1e6:.0f} MB",
    ]
    if over_limit:
        lines += ["",
                  f"**Not saved:** the new images total {new_bytes / 1024 ** 3:.2f} GB, over the "
                  f"{args.max_new_gb} GB limit for this repo. Store them somewhere built for "
                  "large files (such as Cloudflare R2) instead, or rerun with a higher limit."]
    lines += ["", "## Supplier images loaded straight from other sites", "",
              f"- Checked: {len(supplier_rows)}",
              f"- Working: {sup.get('ok', 0)}",
              f"- Broken (404/410): {sup.get('broken', 0)}",
              f"- Server refused the check (may still work for visitors): {sup.get('blocked_check', 0)}",
              f"- Other errors: {sup.get('error', 0)}",
              "", "Details: `wix-manifest.csv` maps every Wix image to its copy here; "
              "`supplier-image-links.csv` lists each supplier link's status."]
    write_lines(REPORT, lines)
    print("\n".join(lines))

    if args.commit:
        commit_and_push([MANIFEST, SUPPLIER_REPORT, REPORT, IDS_FILE, SUPPLIER_FILE],
                        f"Image backup report: {covered}/{len(ids)} Wix images have a copy",
                        push=True)


if __name__ == "__main__":
    main()
