#!/usr/bin/env bash
set -euo pipefail

# Release checks for a version tag (used by .github/workflows/release.yml).
# Usage: scripts/release-check.sh <tag> <notes-file>
#   <tag>        a tag "v" + a semantic version, for example v0.16.3 or v1.0.0-rc.1
#   <notes-file> where to write the CHANGELOG section of that version
# It checks that the tag is a semantic version, that the tagged commit is on
# origin/main, and that pyproject.toml and tradewind.__version__ hold the same
# version. It prints the version (the tag without the "v") on stdout.

tag="${1:?usage: release-check.sh <tag> <notes-file>}"
notes="${2:?usage: release-check.sh <tag> <notes-file>}"

# semver.org's own pattern, with the leading "v".
semver='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-((0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)(\.(0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*))*))?(\+([0-9a-zA-Z-]+(\.[0-9a-zA-Z-]+)*))?$'
if ! [[ "$tag" =~ $semver ]]; then
  echo "error: $tag is not v + a semantic version" >&2
  exit 1
fi
version="${tag#v}"

commit="$(git rev-list -n 1 "$tag")"
if ! git merge-base --is-ancestor "$commit" origin/main; then
  echo "error: $tag ($commit) is not on origin/main" >&2
  exit 1
fi

in_pyproject="$(git show "$commit:pyproject.toml" | sed -n 's/^version = "\(.*\)"$/\1/p' | head -n 1)"
in_package="$(git show "$commit:src/tradewind/__init__.py" | sed -n 's/^__version__ = "\(.*\)"$/\1/p' | head -n 1)"
if [ "$in_pyproject" != "$version" ] || [ "$in_package" != "$version" ]; then
  echo "error: $tag needs version $version; pyproject.toml has '$in_pyproject', __version__ has '$in_package'" >&2
  exit 1
fi

# The CHANGELOG section "## [<version>] ..." up to the next "## [" heading.
git show "$commit:CHANGELOG.md" | awk -v v="$version" '
  index($0, "## [" v "]") == 1 { on = 1; next }
  on && /^## \[/ { exit }
  on { print }
' > "$notes"
if ! grep -q '[^[:space:]]' "$notes"; then
  echo "error: CHANGELOG.md has no section for [$version]" >&2
  exit 1
fi

echo "$version"
