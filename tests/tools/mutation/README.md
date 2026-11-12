# Mutation harness (on demand, reviewers only)

`mutate.py` plants one small fault at a time in a COPY of the repository and runs the tests, to see whether a test notices
(KILLED) or not (SURVIVED). `mutants_workflow.py` holds the workflow, compose, backup, deploy and scope-filter mutants and is
read by `mutate.py`. CI never runs either file, and they are outside the collector size budget (`ops/check_size_budget.py`
counts `collector/src` only). The file names do not start with `test_`, so pytest does not collect them.

Usage, settings and the lane letters are in the docstring at the top of `mutate.py`. In short:

```sh
git worktree add --detach .worktrees/mut <commit>          # a throwaway copy: the harness edits files in it
# a throwaway PostgreSQL 16 built like CI, database name ending _test (docs/RUNBOOKS/run-tests.md, section 1b)
VATPAC_MUT_PORT=58339 VATPAC_MUT_PASSWORD=... python tests/tools/mutation/mutate.py ABCDFW .worktrees/mut out.json my_mut_test
```

A full run (73 mutants) takes about 90 minutes. Re-run only the survivors with `SURV=out.json`.
