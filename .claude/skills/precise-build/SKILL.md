---
name: "precise-build"
description: "Use when writing or changing code that must be correct: enumerates every case, builds it test-first, then proves the tests catch bugs with property-based and mutation testing."
---

# Precise Build

Goal: code that is correct for every class of input, with proof that the tests would catch a bug.
"All possibilities" is infinite, so cover every *class* of input, then generate random inputs, then mutate the code to prove the tests are strong.

Announce: "Using precise-build." Create a task per phase below. Do not skip phases.

## Phase 0 - Detect the stack
Read CLAUDE.md and the repo to find: language, test runner, coverage tool, lint, type checker.
Pick tools:

| Stack | Property-based | Coverage (branch) | Mutation |
|---|---|---|---|
| TypeScript/JS | fast-check | `vitest --coverage` or `jest --coverage` | Stryker (`npx stryker run`) |
| Python | Hypothesis | `pytest --cov --cov-branch` | mutmut (`mutmut run`) |
| Java/Kotlin | jqwik | JaCoCo | PIT |
| Go | `testing/quick` or rapid | `go test -cover` | go-mutesting |

If a tool is missing, ask before installing it as a dev dependency.

## Phase 1 - Specify (no code yet)
If requirements are unclear, use `superpowers:brainstorming` first.
Write the contract for the unit: inputs, outputs, side effects, errors raised, invariants.
Ask the user about anything ambiguous. Never guess a business rule.

## Phase 2 - Enumerate every case (the test matrix)
Produce a table before writing tests. Cover each category:
1. **Happy path**: typical valid inputs.
2. **Equivalence classes**: one representative per class of input that should behave the same.
3. **Boundaries**: min, max, min-1, max+1, zero, one, empty, exactly-at-limit.
4. **Invalid input**: null/None/undefined, wrong type, malformed, missing fields, extra fields.
5. **Special values**: empty string, whitespace, unicode/emoji, very long strings, negative numbers, NaN/Infinity, floats with rounding, dates at DST/leap-year/timezone edges.
6. **Collections**: empty, single item, duplicates, unsorted, very large.
7. **State and order**: called twice (idempotency), out of order, concurrent calls, retries.
8. **External failures**: timeout, throttling, 4xx/5xx, partial failure, network drop (for AWS SDK, DB, HTTP).
9. **Security**: injection, path traversal, auth missing/expired, permission denied, oversized payloads.
10. **Invariants / properties**: rules that hold for ALL inputs (e.g. round-trip encode/decode, output sorted, total preserved, never negative).

Show the matrix to the user and confirm before continuing. Use `engineering:testing-strategy` if the scope spans multiple components.

## Phase 3 - Build test-first
Follow `superpowers:test-driven-development` strictly, one matrix row at a time:
- Write one failing test. Run it. Confirm it fails for the expected reason.
- Write the minimal code to pass. Run the FULL suite.
- Refactor only while green.
Order: happy path -> boundaries -> invalid input -> external failures -> security.
Mock only external boundaries (AWS SDK, HTTP, clock, randomness), behind thin interfaces.

## Phase 4 - Property-based tests
For every invariant from category 10, write a property test that generates hundreds of random inputs.
When a property fails, take the shrunk counterexample, add it as a permanent example test, then fix the code (TDD).

## Phase 5 - Coverage gate
Run branch coverage. Target: 100% of branches in the changed code.
For every uncovered branch: add a test, or delete the branch if it is unreachable. Never add a test that only executes a line without asserting behavior.

## Phase 6 - Mutation testing (proves the tests catch bugs)
Run mutation testing scoped to the changed files.
Target: mutation score of 90% or higher on changed code.
For each surviving mutant: write a test that kills it, or explain why it is equivalent (behaviorally identical).
Re-run until the target is met.

## Phase 7 - Verify and review
1. `superpowers:verification-before-completion`: run full test suite, lint, type check, build. Paste the real output.
2. `engineering:code-review` on the diff, plus `/security-review`.
3. If the change touches AWS resources or IaC and the plugin is installed, run `aws-agents-for-devsecops:diff-scanning-with-aws-security-agent`.

## Final report to the user
- Test matrix (categories covered, count of tests per category)
- Property tests and any counterexamples found
- Branch coverage % on changed code
- Mutation score and any equivalent mutants with reasons
- Full suite / lint / type check results (actual output)
- Anything not covered and why

## Red flags - stop and fix
- A test passed the first time it ran
- Code written before its test
- A category in Phase 2 skipped without a written reason
- Surviving mutants left unexplained
- Claiming done without pasted command output