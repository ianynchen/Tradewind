#!/usr/bin/env bash
set -euo pipefail

# Release checks for a version tag (used by .github/workflows/release.yml).
# Usage: scripts/release-check.sh <tag>
#   <tag>  a tag "v" + a semantic version, for example v0.17.0 or v1.0.0-rc.1
# It checks that the tag is a semantic version and that the tagged commit is on
# origin/main. It prints the version (the tag without the "v") on stdout. The tag
# is the version: the build reads it from git (hatch-vcs), so no file holds it.

tag="${1:?usage: release-check.sh <tag>}"

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

echo "$version"
