# Self Review Guide

Review your own changes before finalizing.

Checklist:
- Correctness: does behavior match request?
- Regressions: any impacted callers or side effects?
- Error handling: failures surfaced clearly?
- Security: secrets, auth, permissions, injection risks?
- Performance: obvious hotspots or unnecessary work?
- Maintainability: clarity, naming, and scope control?
- Tests: sufficient coverage for changed behavior?

Output expectations:
- If issues are found, fix them before final output.
- If tradeoffs remain, state them explicitly.
