# TAO Presets — Ready-to-use cycle configurations

Copy a preset, replace `REPLACE:` fields, and submit to TAO.

Available presets:

| Preset | Use case | Cycle shape |
|--------|----------|-------------|
| `dev.json` | Greenfield features / tasks with new acceptance criteria | design → implement → review → test → validate ↔ fix |
| `refactor.json` | Audit-driven refactors / code quality work where behavior must NOT change | plan → implement → verify ↔ fix → guardrail → commit |

Pick by question: *"Is this task defining new behavior?"*
- **Yes** → `dev.json`. The design step produces acceptance criteria; the test step locks them in.
- **No** → `refactor.json`. There's nothing to design (the finding is the plan) and no new tests to write (existing tests are the contract). A `guardrail` step reads the git diff to catch behavior leakage, and `commit` auto-ships each subtask.

## `dev.json` — Development cycle

6-step cycle for code tasks:

```
design (sonnet) → implement (opus) → review (sonnet) → test (opus) → validate (command) ←→ fix (opus)
```

### Setup

1. Copy `dev.json` to your task file.
2. Replace `title`, `body`, and `cwd`.
3. Replace validate `commands` with your project's actual commands:

| Stack | Typecheck | Test |
|-------|-----------|------|
| Node/TS | `npx tsc --noEmit` | `npm test` |
| Python | `python -m mypy src/` | `python -m pytest tests/ -v` |
| Python (no mypy) | _(remove typecheck line)_ | `python -m pytest tests/ -v` |
| Go | `go vet ./...` | `go test ./...` |

4. (Optional) Add scope for larger tasks that need decomposition:

```json
{
  "scope": { "model_spec": "sonnet@claude" },
  "policies": { "batch_size": 3, "max_iterations": 5, "max_subtasks": 15 }
}
```

### Design principles

Based on [Anthropic's harness research](https://www.anthropic.com/engineering/harness-design-long-running-apps):

**Generator-evaluator separation.** Review (sonnet) evaluates implement's output (opus). Different model + fresh TAO session = the reviewer has never seen the implementer's reasoning. Eliminates self-evaluation bias ("agents confidently praise mediocre work").

**Review reports, doesn't fix.** The reviewer flags problems — it doesn't write code. Fixes come from opus (in the fix step) or are caught by tests. This avoids a weaker model degrading a stronger model's code.

**Acceptance criteria flow through the pipeline.** Design generates falsifiable criteria. Implement builds to them. Review checks the code against them. Test writes tests targeting them. Validate enforces them mechanically. Every step references the same contract.

**Command-based validation.** The validate step runs objective checks (typecheck, tests) that can't be talked around. Failures loop to fix → validate with a retry cap (`max_retries`).

**No over-specification.** Design describes deliverables and criteria, not implementation details. The implementer has flexibility to choose how.

### Step flow detail

| Step | Model | Gets context from | Produces | Notes |
|------|-------|-------------------|----------|-------|
| design | sonnet | subtask description | approach + files + acceptance criteria | First step — reads project files |
| implement | opus | design output | code on disk | Writes files, summary in LLM output |
| review | sonnet | implement output | findings report | Reads files from disk, not just summary |
| test | opus | review output | test files on disk | Uses review findings + criteria to write tests |
| validate | command | — | pass/fail | Typecheck + tests. On fail → fix |
| fix | opus | fix prompt + validation errors | fixed code on disk | Loops back to validate |

### Customization

**Models.** Swap models freely. Keep implement and review on different models for separation. Recommendation: opus for implement/test/fix (code quality), sonnet for design/review (analysis, cheaper).

**Failover.** Add to any LLM step for resilience:
```json
{ "failover": ["sonnet@claude"] }
```

**Retries.** `max_retries: 3` caps the fix→validate loop. Increase for complex projects with flaky tests.

**Timeout.** Default 1800s (30 min) per step. Override per step:
```json
{ "timeout": 3600 }
```

**Tools.** By default all tools are available. Restrict per task:
```json
{ "tools": ["Read", "Write", "Edit", "Glob", "Grep"] }
```

---

## `refactor.json` — Audit-driven refactor cycle

6-step cycle for code-quality work where **observable behavior must not change**:

```
plan (sonnet) → implement (opus) → verify (command) ←→ fix (opus) → guardrail (sonnet) → commit (command)
```

### When to use

- Applying findings from an audit (dead code, duplication, typing, naming, moving files).
- N+1 → batch and similar "same result, different execution" perf work.
- Any cleanup where the contract (API shape, DB state, error envelopes) must stay identical.

### When NOT to use

- You're adding a new endpoint or changing response shape → `dev.json`.
- You're changing auth / rate limits / env vars / schema types → these are functional changes, use `dev.json` (or split the work).
- The fix is risky and needs tests written for it → `dev.json`.

### Setup

1. Copy `refactor.json` to your task file.
2. Replace `title`, `body_file`, `cwd`.
3. Replace the `verify.commands` with typecheck + test commands for your stack.
4. Replace the `commit.commands[1]` with the project's commit convention (e.g. `git commit -m "[hoku] BE quality: ${subtask_title}"`).
5. Write the `body_file` with the findings to apply (one subtask per finding). See **Body file format** below.

### Design principles

**The audit is the design.** The `plan` step only verifies the finding still exists and lists concrete edits — it does NOT reinvent a plan or produce acceptance criteria (which don't fit refactors). This drops one step compared to `dev.json`.

**No new tests, ever.** Existing tests are the contract. The absence of a `test` step is intentional — for quality-only work, writing new tests would either duplicate coverage or hallucinate assertions. The `fix` prompt explicitly forbids weakening existing tests.

**Guardrail over review.** The `guardrail` step reads the git diff and makes a binary call: SAFE or BLOCK. No middle category — "probably fine" is BLOCK. This catches behavior leakage (`"you added a rate limit"`) that tsc+tests won't catch if existing tests don't cover it. A generic review step would flag style opinions; guardrail flags only behavior risk. The binary design eliminates the comfort of a "RISKY" escape hatch that lets marginal changes slip through.

**Subtask contract propagates.** The `plan` step produces a `## Subtask contract` block listing Files, Change category (from a closed enum like `extract-helper` / `dead-code-removal` / `literal-to-constant`), and Expected diff shape. `implement` copies this block verbatim into its own output; `guardrail` reads it from context and judges the real diff against it. Any edit that exceeds the declared Files list or doesn't fit the declared Change category is automatically BLOCK, even if it wouldn't alter behavior — scope discipline is enforced mechanically.

**Forbidden list in `implement`.** The prompt ships a concrete negative list: no new deps, no rate limits, no auth changes, no env vars, no table drops, no new exports, no public-signature changes. Violating any aborts the subtask cleanly rather than producing a half-done refactor.

**Commit per subtask.** `commit` is mechanical. Many small commits → easy revert, easy review, easy bisect if something breaks later.

### Step flow detail

| Step | Model | Reads | Produces | Notes |
|------|-------|-------|----------|-------|
| plan | sonnet | subtask + listed files | `## Subtask contract` + edits + risks | Read-only. Contract enumerates Files, Change category, Expected diff shape. Can emit `SKIP` or `ABORT` before any edit |
| implement | opus | plan output | edits on disk + output starting with verbatim contract | Enforces hard/soft rule lists + contract. Aborts on rule violation. Preserves contract for guardrail |
| verify | command | — | pass/fail | typecheck + tests. On fail → fix |
| fix | opus | fix prompt + verify errors | fixed code | Forbidden: weakening tests, adding `any`, `@ts-ignore`, `!`. Can abort |
| guardrail | sonnet | contract (from implement's output) + git diff | PASS or BLOCK (+ revert if BLOCK) | Reads actual diff, not implement's narrative. Judges diff against contract — scope creep is automatic BLOCK |
| commit | command | — | commit or no-op | Standardized message. If guardrail reverted, `if git diff --cached --quiet` short-circuits to a no-op exit 0 |

### Body file format

`body_file` is markdown. Structure it as an ordered list of findings. Each finding = one subtask. Each finding MUST declare the same five contract fields the `plan` step will echo — this way the body, the plan, and the guardrail all speak the same vocabulary.

```markdown
# <Project> — Code quality refactor

## Context
- Project conventions: `<path>/CLAUDE.md`
- Audit report: `<path>/audits/YYYY-MM-DD-<slug>.md`

## HARD rules (every subtask must preserve)
- NO new npm deps, routes, rate limits, env vars, auth/JWT changes
- NO schema migrations beyond declaring indexes that already exist in prod
- NO table/column drops
- NO public-signature changes
- NO response-shape / error-message / log-format changes

## Change category enum
Every finding's `Change category` MUST be one of:
`rename` | `move-file` | `extract-helper` | `extract-module` | `dead-code-removal` |
`literal-to-constant` | `inline-to-import` | `type-narrowing` | `type-generic-rewrite` |
`index-declaration` | `loop-to-batch` | `deduplication` | `import-reorganization`

If the change doesn't fit any of those, it's not pure refactor — route it to `dev.json` instead.

## Findings

### F-01: <short title>
- **Files**: `path/to/file.ts`, `path/to/other.ts`  (exhaustive — include cascade imports)
- **Change category**: `extract-helper`
- **Expected diff shape**: one sentence describing additions/deletions (e.g. "New lib/serialize.ts with 2 exports; 9 callsites updated to import; no other edits")
- **Behavior preservation**: one sentence (e.g. "Compile-time only" or "Runtime unchanged because output format is preserved")
- **Finding**: one-paragraph description of the smell and why it needs fixing

### F-02: ...
```

The scope LLM picks `batch_size` findings per iteration and outputs each as a subtask whose `description` is the finding block verbatim. The `plan` step reads this description and echoes the five fields into its `## Subtask contract` block, which then propagates through `implement` → `guardrail`.

### Customization

**batch_size**. Default 3 — refactors cluster on shared files, so smaller batches avoid conflicts. Increase if findings are well-isolated (e.g., 10 routes with independent fixes).

**max_retries**. Default 2 for refactor (vs 3 for dev). Refactors that fail twice after a fix attempt usually mean the finding is wrong or out of scope — escalate rather than loop.

**Skip vs abort semantics**. `SKIP` = finding no longer applies (moved, already fixed). `ABORT` = finding applies but cannot be fixed without behavior change. Both are healthy outcomes; TAO records them in the subtask summary and moves on.

**Commit step handles guardrail BLOCK automatically.** The commit command's second line (`if git diff --cached --quiet; then echo …; else <commit> fi`) succeeds whether or not there's anything staged. A BLOCK from guardrail → files reverted → nothing staged → the `if` branch prints a message and exits 0. The subtask is marked successful (it correctly did nothing). If you want BLOCK to appear as a failed subtask instead, change the `if` branch to `exit 1`.
