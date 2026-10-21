## What changes and why

<!-- One or two sentences. What does a reader of the diff need to know that the diff does not say? -->

## Issues

<!--
Write `Refs #N` for every issue this touches.

Do NOT write Closes, Fixes or Resolves followed by an issue number unless this pull request finishes the whole issue and
it is already deployed and verified. GitHub closes the issue the moment the pull request merges, leaves no comment on it,
and does not care whether the work is deployed (#201 was closed by mistake that way on 21-10-2026).
Close issues by hand after the deploy, with a comment saying what shipped, the commit and how it was verified.
-->

Refs #

## Checked

<!-- Say what you actually ran and what it printed, not what should pass. -->
- [ ] lint and unit tests (the exact list is `.github/workflows/ci.yml`; a local script can run it)
- [ ] integration tests against a throwaway database, never against a copy of production
- [ ] anything not run is named here, with the reason:

## Deploy impact

<!-- None / watchdog image / collector image / migration / dashboards only / docs only. A migration needs a dump first and a rehearsal. -->
