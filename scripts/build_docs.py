"""Render the GitHub Pages site.

    python scripts/build_docs.py              # docs/ -> site/
    python scripts/build_docs.py --repo URL   # override the detected repository
    python scripts/build_docs.py --out dist   # a different output directory

The site source in `docs/` contains `{{PLACEHOLDER}}` markers instead of
absolute URLs, so nothing about a user, a machine or a GitHub account is
hardcoded in the repository. The values are resolved at build time from the
git remote and from the latest git tag:

    {{REPO_URL}}    https://github.com/<owner>/<repo>
    {{REPO_NAME}}   <owner>/<repo>
    {{VERSION}}     the latest git tag, e.g. v0.0.1
    {{YEAR}}        the current year

The rendered site is written to `site/` by default and is published by
`.github/workflows/pages.yml`.
"""

from __future__ import annotations

import argparse
import datetime
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
DEFAULT_OUT = ROOT / "site"

# files that are copied as they are
PLAIN_SUFFIXES = (".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2", ".webmanifest")
# files that get the placeholder substitution
TEXT_SUFFIXES = (".html", ".css", ".js", ".json", ".txt", ".xml", ".md")

REMOTE_RE = re.compile(
    r"(?:https?://|git@)(?:github\.com[:/])?(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
# a bare "owner/repo" as well
SHORT_RE = re.compile(r"^(?P<owner>[^/@\s]+)/(?P<repo>[^/@\s]+?)(?:\.git)?$")


def run_git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def detect_repo(explicit: str | None = None) -> tuple[str, str]:
    """Return (owner, repo). Falls back to the repository directory name."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    remote = run_git("remote", "get-url", "origin")
    if remote:
        candidates.append(remote)
    for candidate in candidates:
        candidate = candidate.strip()
        match = REMOTE_RE.search(candidate) or SHORT_RE.match(candidate)
        if match:
            owner = match.group("owner")
            repo = match.group("repo")
            if owner and repo:
                return owner, repo
    # no remote yet: use the folder name and a neutral owner
    return "your-name", ROOT.name


def detect_version() -> str:
    tag = run_git("describe", "--tags", "--abbrev=0")
    if tag:
        return tag
    return "v0.0.0"


def render(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{%s}}" % key, value)
    return text


def build(repo: str | None = None, out_dir: Path = DEFAULT_OUT) -> int:
    if not DOCS_DIR.is_dir():
        print("docs directory not found: %s" % DOCS_DIR, file=sys.stderr)
        return 2

    owner, name = detect_repo(repo)
    values = {
        "REPO_URL": "https://github.com/%s/%s" % (owner, name),
        "REPO_NAME": "%s/%s" % (owner, name),
        "VERSION": detect_version(),
        "YEAR": str(datetime.date.today().year),
    }

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    copied = 0
    for source in sorted(DOCS_DIR.rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(DOCS_DIR)
        target = out_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() in TEXT_SUFFIXES:
            text = source.read_text(encoding="utf-8")
            target.write_text(render(text, values), encoding="utf-8")
        else:
            shutil.copy2(source, target)
        copied += 1

    print("repository : %s" % values["REPO_NAME"])
    print("version    : %s" % values["VERSION"])
    print("rendered   : %d file(s) -> %s" % (copied, out_dir))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", help="repository URL or owner/name (default: the git remote)")
    parser.add_argument("--out", help="output directory (default: site/)")
    args = parser.parse_args()
    out_dir = Path(args.out).resolve() if args.out else DEFAULT_OUT
    return build(repo=args.repo, out_dir=out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
