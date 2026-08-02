#!/usr/bin/env python3
"""Generate the Homebrew formula for trrnt.

Homebrew installs a Python app into its own virtualenv and refuses to reach
out to the network for anything it has not pinned, so every transitive
dependency has to appear in the formula as a `resource` block with a URL and
a sha256. Nineteen of them, today. Hand-maintaining that list is how tap
formulae rot, so this generates the whole file instead.

    python3 scripts/brew_formula.py --version 0.1.0 -o Formula/trrnt.rb

The formula points at the sdist attached to the GitHub release, not at the
tarball GitHub generates on the fly for a tag. Those generated archives are
not contractually byte-stable — Homebrew has been broken by a change to how
they are produced before — and a release asset is a file we uploaded once.

So the sha256 comes from that exact artifact: `--tarball` hashes a local file
(what CI does, right after building it), and otherwise it is downloaded from
the release. `--offline-sha` skips both for a dry run.

Resolution comes from `uv pip compile`, which is the same resolver the project
is developed against, so the formula pins what was actually tested.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPI = "https://pypi.org/pypi"
PACKAGE = "trrnt"

# aria2 is the download engine — trrnt cannot move a byte without it, it is a
# small install, and having brew place it means the first run has one less
# thing to do. Jackett and ClamAV are deliberately NOT dependencies: Jackett
# drags in a .NET runtime and ClamAV a virus database, both are large, and the
# setup wizard already installs them with an explanation of what they are for.
# A formula that silently downloads several hundred megabytes is a worse first
# impression than a wizard that asks.
#
# libyaml is not ours — it is PyYAML's. Homebrew builds every resource from
# its sdist, and without libyaml headers PyYAML quietly drops to its pure
# Python loader. `brew style` flags the omission (FormulaAudit/
# ResourceRequiresDependencies), which is how it got here.
BREW_DEPS = ["aria2", "libyaml"]

PLACEHOLDER_SHA = "0" * 64


def resolve_dependencies() -> list[tuple[str, str]]:
    """Every transitive dependency as (name, version), trrnt itself excluded."""
    # Writes the pinned set to stdout and its progress chatter to stderr, so
    # no -o is passed: `-o -` would create a file literally named "-".
    result = subprocess.run(
        ["uv", "pip", "compile", "pyproject.toml", "--no-header"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.exit(f"uv pip compile failed:\n{result.stderr}")

    packages = []
    for line in result.stdout.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, _, version = line.partition("==")
        name = name.strip()
        if name.lower() == "trrnt":
            continue
        packages.append((name, version.strip()))

    if not packages:
        # A formula with no resources builds an empty virtualenv and fails at
        # runtime on the user's machine, not here. Refuse to emit one.
        sys.exit("resolved no dependencies — uv's output format has changed:\n"
                 f"{result.stdout[:400]}")
    return packages


def sdist_for(name: str, version: str) -> tuple[str, str]:
    """The (url, sha256) of a release's source distribution.

    Homebrew builds from sdists — a wheel-only dependency would need a
    different stanza, so say so loudly rather than emit a formula that fails
    to build on someone else's machine.
    """
    with urllib.request.urlopen(f"{PYPI}/{name}/{version}/json", timeout=30) as resp:
        data = json.load(resp)

    for entry in data["urls"]:
        if entry["packagetype"] == "sdist":
            return entry["url"], entry["digests"]["sha256"]
    sys.exit(f"{name} {version} publishes no sdist — the formula needs one")


def sha256_of_url(url: str) -> str:
    """sha256 of what the URL actually serves, streamed rather than buffered."""
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            while chunk := resp.read(1 << 20):
                digest.update(chunk)
    except urllib.error.HTTPError as exc:
        sys.exit(
            f"GitHub returned {exc.code} for {url}\n"
            "The release and its sdist asset have to exist before the formula "
            "can pin them. In CI, pass --tarball instead."
        )
    return digest.hexdigest()


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def render(url: str, sha: str, python: str,
           resources: list[tuple[str, str, str]]) -> str:
    blocks = "\n\n".join(
        f'  resource "{name}" do\n'
        f'    url "{res_url}"\n'
        f'    sha256 "{res_sha}"\n'
        f'  end'
        for name, res_url, res_sha in resources
    )
    # Homebrew's audit wants depends_on lines in alphabetical order, which puts
    # the python formula in among the rest rather than at the top.
    deps = "\n".join(
        f'  depends_on "{d}"' for d in sorted([*BREW_DEPS, python])
    )

    return f'''# Generated by scripts/brew_formula.py — do not edit by hand.
# Regenerate with: python3 scripts/brew_formula.py --version <x.y.z>
class Trrnt < Formula
  include Language::Python::Virtualenv

  desc "Terminal torrent aggregator and downloader with VPN kill switch"
  homepage "https://github.com/BeauOuellette/trrnt"
  url "{url}"
  sha256 "{sha}"
  license "MIT"
  head "https://github.com/BeauOuellette/trrnt.git", branch: "main"

{deps}

{blocks}

  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      Run `trrnt` to start. The first launch opens a setup wizard that
      installs Jackett (the indexer aggregator) and optionally ClamAV,
      starts them, and writes ~/.config/trrnt/config.yaml for you.

      trrnt keeps BitTorrent traffic bound to a VPN interface and refuses
      to start aria2 when no tunnel is carrying traffic. Connect your VPN
      before launching.
    EOS
  end

  test do
    # Proves the virtualenv resolved and the entry point is wired up. Nothing
    # here touches the network, a config file, or the user's real state.
    assert_match version.to_s, shell_output("#{{bin}}/trrnt --version")
    assert_match "search", shell_output("#{{bin}}/trrnt --help")
  end
end
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="release version, e.g. 0.1.0")
    parser.add_argument("--repo", default="BeauOuellette/trrnt")
    parser.add_argument("--python", default="python@3.14",
                        help="Homebrew python formula to build against")
    parser.add_argument("-o", "--output", default="Formula/trrnt.rb")
    parser.add_argument("--tarball", type=Path, default=None,
                        help="hash this local sdist instead of downloading it")
    parser.add_argument("--offline-sha", action="store_true",
                        help="skip hashing entirely and emit a placeholder sha256")
    args = parser.parse_args()

    # The sdist asset uploaded to the release, not GitHub's generated archive.
    url = (f"https://github.com/{args.repo}/releases/download/"
           f"v{args.version}/{PACKAGE}-{args.version}.tar.gz")

    print("resolving dependencies…", file=sys.stderr)
    packages = resolve_dependencies()
    resources = []
    for name, version in packages:
        res_url, res_sha = sdist_for(name, version)
        print(f"  {name} {version}", file=sys.stderr)
        resources.append((name, res_url, res_sha))

    if args.offline_sha:
        sha = PLACEHOLDER_SHA
        print("warning: emitting a placeholder sha256 — this formula will not "
              "install until it is regenerated against a real release",
              file=sys.stderr)
    elif args.tarball:
        if not args.tarball.is_file():
            sys.exit(f"no such tarball: {args.tarball}")
        print(f"hashing {args.tarball}…", file=sys.stderr)
        sha = sha256_of_file(args.tarball)
    else:
        print(f"hashing {url}…", file=sys.stderr)
        sha = sha256_of_url(url)

    out = Path(args.output)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(url, sha, args.python, resources))
    print(f"wrote {out} ({len(resources)} resources)", file=sys.stderr)


if __name__ == "__main__":
    main()
