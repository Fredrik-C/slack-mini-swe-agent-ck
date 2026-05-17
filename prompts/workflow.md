# Workflow

Use this strict phase order for every task:

1. Plan
2. Implement
3. Review
4. If review finds issues, return to Implement then Review (max 3 total review passes)
5. Test
6. Create PR

Execution rules:
- Do not skip phases.
- Keep the review loop bounded to at most 3 review passes total before moving to test.
- If blocked, report blocker, attempted command, and next required user action.
- Keep edits scoped to the requested task.
- Prefer small, reviewable changes.
