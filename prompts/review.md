# Review Guide

Review your own changes before finalizing each implementation pass.

Checklist:
- Correctness: does behavior match request?
- Regressions: any impacted callers or side effects?
- Error handling: failures surfaced clearly?
- Security: secrets, auth, permissions, injection risks?
- Performance: obvious hotspots or unnecessary work?
- Maintainability: clarity, naming, and scope control?
- Tests: sufficient coverage for changed behavior?

Output expectations:
- If issues are found, return to implementation and review again (max 3 review passes total).
- If tradeoffs remain, state them explicitly.
