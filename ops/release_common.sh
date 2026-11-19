# Helpers for ops/stale_issues.sh (issue 215). Sourced, never run; plain POSIX, no output of its own.

# gh_bin: the GitHub CLI to call. GH_BIN names it; else gh on the PATH;
# else the usual Windows install, written in the form Git Bash accepts.
gh_bin() {
  if [ -n "${GH_BIN:-}" ]; then echo "$GH_BIN"; return; fi
  command -v gh 2>/dev/null || echo "/c/Program Files/GitHub CLI/gh.exe"
}

# The release marker is ~/vatpac-statsV2/.deploy/release on the VM: a line commit=<sha>. It is read with ONE
# pattern, 7 to 40 hex characters: a short or hand-written marker still works because every
# caller hands the value to git, which resolves or rejects it; anything that is not 7 to 40 hex characters is ignored.
RELEASE_MARKER_CMD='cat .deploy/release 2>/dev/null'   # run on the VM, in the checkout directory

# release_marker_commit: stdin is the text of the marker file; prints its commit, or nothing.
release_marker_commit() {
  tr -d '\r' | sed -n 's/^commit=\([0-9a-fA-F]\{7,40\}\)$/\1/p' | head -n 1
}
