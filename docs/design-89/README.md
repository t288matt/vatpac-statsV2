# Working papers for issue #89

These four files are the dated working papers behind the design of issue #89 (the `flight` view recomputes the merge chain over all history on every query). They are kept unchanged so the reasoning can be audited.

- `DESIGN_89.md`: Sonnet designer (branch `design-89`), options A to C, an addendum and a final recommendation.
- `OPUS_REVIEW_89.md`: Opus architect (branch `opus-89`), many dated sections.
- `DESIGN_89_FINAL.md`: Opus build-ready design for option (c), four phases.
- `INDEX_TEST_89.md`: measurements showing indexes do not fix it (branch `test-89-index`).

**Recommendations in earlier sections are superseded by later ones.** Both designers changed their advice as the owner added facts. For example, the top summary and section 1.10 of `OPUS_REVIEW_89.md` recommend option (a); that was replaced by `DESIGN_89_FINAL.md` (option c). Likewise the first two parts of `DESIGN_89.md` are replaced by its final section.

**`docs/DESIGN_89_OPTIONS.md` is the summary.** Read it first: it lists every option, the measured evidence, how the advice changed, the current recommendation, the open owner decisions and how to resume. Issue #89 is parked (12-10-2026).
