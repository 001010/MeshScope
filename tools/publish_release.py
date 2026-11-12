"""Publish the built installer as a GitHub Release asset.

    .venv\\Scripts\\python tools\\publish_release.py [--notes "text"] [--target <commit>] [--replace] [--draft]

Creates (or reuses) the release for tag v<version> (from meshscope/__init__.py) on the repo
behind the `origin` remote, and uploads dist\\MeshScope-<version>-setup.exe (the installer with the
MeshScope icon) and dist\\MeshScope-<version>-win64.msi (the same installer as a plain MSI) to it.

Authentication: uses $GITHUB_TOKEN if set, otherwise asks Git Credential Manager for the
github.com credential that `git push` already uses. The token is only held in memory; it
is never printed or written anywhere.

Safety checks: the commit being released must already be on GitHub, and (when releasing
HEAD) the working tree must be clean, so the release always matches pushed source code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def token() -> str:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, timeout=120, cwd=ROOT).stdout
    tok = dict(l.split("=", 1) for l in out.splitlines() if "=" in l).get("password")
    if not tok:
        sys.exit("No GitHub credential found. Run `git push` once (it signs you in) or set GITHUB_TOKEN.")
    return tok


class GitHub:
    def __init__(self, repo: str, tok: str):
        self.repo, self.tok = repo, tok

    def call(self, method: str, url: str, body=None, data: bytes | None = None, ctype: str = "application/json"):
        if not url.startswith("http"):
            url = f"{API}/repos/{self.repo}{url}"
        if body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.tok}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "MeshScope-release-tool"})
        if data is not None:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404 and method == "GET":
                return None
            detail = e.read().decode(errors="replace")[:500]
            sys.exit(f"GitHub API {method} {url.split('?')[0]} failed: {e.code} {detail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notes", help="release notes (default: commit subjects since the previous release tag)")
    ap.add_argument("--target", default="HEAD", help="commit to tag (default HEAD)")
    ap.add_argument("--replace", action="store_true", help="replace an installer already attached to this release")
    ap.add_argument("--draft", action="store_true", help="create the release as a draft")
    args = ap.parse_args()

    version = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "meshscope" / "__init__.py").read_text()).group(1)
    tag = f"v{version}"
    msi = ROOT / "dist" / f"MeshScope-{version}-win64.msi"
    setup_exe = ROOT / "dist" / f"MeshScope-{version}-setup.exe"  # the MSI wrapped with the MeshScope icon
    for f in (msi, setup_exe):
        if not f.exists():
            sys.exit(f"{f} not found - run build.ps1 first")
    files = [(setup_exe, "application/vnd.microsoft.portable-executable"), (msi, "application/x-msi")]

    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", git("remote", "get-url", "origin"))
    if not m:
        sys.exit("The origin remote is not a GitHub repository")
    repo = m.group(1)

    sha = git("rev-parse", args.target)
    git("fetch", "--quiet", "origin")
    if not git("branch", "-r", "--contains", sha):
        sys.exit(f"{sha[:7]} is not on GitHub yet - push it first, so the release matches published code")
    if args.target == "HEAD" and git("status", "--porcelain", "--untracked-files=no"):
        sys.exit("Uncommitted changes: the installer may not match HEAD. Commit and push first (or pass --target).")

    gh = GitHub(repo, token())
    rel = gh.call("GET", f"/releases/tags/{tag}")
    if rel is None:
        notes = args.notes
        if not notes:
            prev = [t for t in git("tag", "--list", "v*", "--sort=-v:refname").splitlines() if t != tag]
            rng = f"{prev[0]}..{sha}" if prev else sha
            notes = "\n".join(f"- {s}" for s in git("log", "--format=%s", rng).splitlines()) or f"MeshScope {version}"
        notes += (f"\n\nInstall: download `{setup_exe.name}` and run it. It upgrades an existing MeshScope install in place."
                  f"\n`{msi.name}` is the same installer as a plain Windows Installer package (for managed or scripted installs).")
        rel = gh.call("POST", "/releases", {"tag_name": tag, "target_commitish": sha, "name": f"MeshScope {version}",
                                            "body": notes, "draft": args.draft, "prerelease": False})
        print(f"Created release {tag} at {sha[:7]}")
    else:
        print(f"Using existing release {tag}")

    names = {f.name for f, _ in files}
    for a in rel.get("assets", []):
        if a["name"] in names:
            if not args.replace:
                sys.exit(f"{a['name']} is already attached to {tag}: {a['browser_download_url']} (use --replace)")
            gh.call("DELETE", f"/releases/assets/{a['id']}")
            print(f"Removed the previous {a['name']}")

    urls = []
    for f, ctype in files:
        size = f.stat().st_size
        print(f"Uploading {f.name} ({size / 1e6:.1f} MB)...")
        up = re.sub(r"\{.*\}$", "", rel["upload_url"]) + "?" + urllib.parse.urlencode({"name": f.name, "label": f.name})
        asset = gh.call("POST", up, data=f.read_bytes(), ctype=ctype)
        if asset.get("size") != size:
            sys.exit(f"Upload size mismatch for {f.name}: sent {size}, GitHub has {asset.get('size')}")
        urls.append(asset["browser_download_url"])
    git("fetch", "--quiet", "--tags", "origin")
    print(f"Published: {rel['html_url']}")
    for u in urls:
        print(f"Installer: {u}")


if __name__ == "__main__":
    main()
