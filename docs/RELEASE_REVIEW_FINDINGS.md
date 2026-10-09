# Release review findings: issues to file (08-10-2026)

Detail, evidence and proposed diffs: docs/RELEASE_REVIEW.md.

1. Size budget: main.py is 310 lines, module limit 300: CI size-budget fails after the merge, so release.yml cannot publish.
2. Parser: infinite number (1e999) in an integer field aborts every poll: catch `OverflowError` in `_i`.
3. Parser: NaN or infinite latitude/longitude is not counted as malformed: reject non-finite coordinates like unparsable ones.
4. Document and soften the clock-skew failure mode of FEED_MAX_FUTURE_SECONDS: a VM clock about 128 s slow fails every poll; add a runbook line and consider a 60 s default.
5. Deploy runbook: build the collector image before first use of the archive volume: an old image gives a root-owned volume and a dead archive.
6. Archive hard cap should delete routine files before anomaly files: sort compact and full ahead of anomaly.
7. Archive: ts_future reason is unreachable from the collector and ignores FEED_MAX_FUTURE_SECONDS.
8. Archive: never drop an error-poll anomaly because the worker is busy.
9. Archive: remove the partial tmp file on a failed write and back off: a full disk retries two 1.5 MB gzips every poll.
10. Archive: stop between writes on shutdown: SIGTERM waits for the in-flight job (11 s with a slow disk).
11. Settings: tie ARCHIVE_GAP_S to POLL_INTERVAL_SECONDS: above a 35 s poll interval every poll is an anomaly.
12. Collector-health panel 9: describe rejected_values correctly and note persistent conditions count every poll.
13. Docs: refresh test counts (282 unit, 238 integration) and the size-budget line count in run-tests.md and the budget text.
