# Task Lifecycle

## State Machine

```
                  ┌──────────┐
                  │  queued   │ ◄── submit()
                  └────┬─────┘
                       │ queue dispatches
                       ▼
                  ┌──────────┐
          ┌──────►│ running  │◄──────────────┐
          │       └──┬───┬───┘               │
          │          │   │                   │
          │  ┌───────┘   └────────┐          │
          │  │                    │          │
          │  ▼                    ▼          │
     ┌─────────┐           ┌──────────┐     │
     │ blocked │──────────►│          │     │
     └─────────┘ unblock() │          │     │
                           │          │     │
          ┌────────────────┤          │     │
          │                │          │     │
          ▼                ▼          ▼     │
     ┌───────────┐   ┌──────────┐  ┌──────┐│
     │ completed │   │  failed  │  │stopped││
     └───────────┘   └──────────┘  └──────┘│
                                           │
                     re-queue ──────────────┘

     cancel() → cancelled  (terminal, any non-terminal state)
```

## Transitions

| From | To | Trigger |
|------|----|---------|
| `queued` | `running` | Queue poll loop dispatches task (capacity available). |
| `running` | `completed` | All subtasks succeeded + deliver passed. |
| `running` | `failed` | Step failed, max_retries exhausted, workspace command failed, or policy limit exceeded. |
| `running` | `blocked` | Step returned `blocked_reason`. |
| `running` | `stopped` | `engine.stop()` called; flow checks stop event at safe points. |
| `blocked` | `queued` | `engine.unblock()` called. Task re-enters queue and resumes from checkpoint. |
| `queued/running/blocked/stopped` | `cancelled` | `engine.cancel()` called. Terminal — cannot be resumed. |

Invalid transitions (e.g., cancelling a completed task) raise `TaoError`.

## Execution Modes

TAO supports two execution modes per step:

### LLM-direct (default)

The engine calls the LLM provider directly with the task context. The `cwd` config sets the working directory; the `cycle` array determines the model and behavior per step. No scripts or manifests needed.

### Subprocess (escape hatch)

When a step has a manifest with a `command` field, TAO runs it as a subprocess instead of calling the LLM directly. Used for deterministic checks, external API calls, or custom logic. See [step-protocol.md](step-protocol.md).

## Flow Phases

Within a `running` task, the flow progresses through these phases:

```
1. Workspace create (if configured) — or use cwd for simple cases
2. Scope (decompose into subtasks) — omit for one-shot tasks
3. For each subtask in batch:
   a. Cycle steps in order (e.g. plan → implement → validate → fix)
      - Each step's `next` keyword controls which step runs after it
      - Command steps use `on_fail` to jump backward (e.g. to a fix step)
      - Backward jumps count toward max_retries; exhausted → subtask fails
   b. Workspace persist (if configured)
4. Batch checkpoint saved
5. Re-scope (always — zero subtasks = task complete)
   → repeat from step 3 with new subtasks, or finish if empty
6. Workspace deliver (if configured)
7. Workspace cleanup (if configured)
```

`max_iterations` acts as a **checkpoint** (pause), not a failure. When the batch counter reaches `max_iterations`, the flow pauses for human approval before the next re-scope. The task enters `blocked` state; use `engine.unblock()` to continue.

## Policies

Policies control flow behavior limits. All are configurable per task.

### `max_subtasks` (default: 20, range: 1–100)

Maximum subtasks the scope step can return. If scope returns more, the flow raises `TaoError` immediately (before executing any subtask).

### `timeout` (default: 1800 for cycle steps, 300 for subprocess steps)

Default timeout in seconds. Each cycle step can override this with its own `timeout` field.

When a step exceeds its timeout:
1. Process is killed (`SIGKILL`).
2. Engine waits 5s for cleanup.
3. If still alive, `SIGTERM` is sent.
4. `StepTimeoutError` is raised.

### `batch_size` (default: 5, range: 1–50)

Passed to the scope step as `ctx.batch_size`. The scope step should use this to limit how many subtasks it returns per batch.

### `max_iterations` (default: 10, range: 1–100)

Maximum scope→execute cycles (batches) before the flow pauses for human approval. When reached, the task enters `blocked` state. Use `engine.unblock()` to continue. This is a **checkpoint**, not a failure.

### `max_retries` (default: 3, per-task config field)

Maximum backward jumps allowed per subtask. Each time a `next` or `on_fail` keyword causes execution to jump to a step that appears earlier in the cycle, that counts as one retry. When `max_retries` is exhausted, the subtask fails.

Forward jumps (to a later step) are free and do not count toward `max_retries`. A linear cycle with no backward jumps runs exactly once regardless of this value.

## Blocked → Unblock Flow

When a task is blocked:

1. The checkpoint is saved with the current state (completed subtasks, pending subtasks, context, blocked reason).
2. The `on_blocked` hook fires.
3. Task status becomes `blocked`.

To resume:

```python
engine.unblock(task_id, context={"answer": "approved"})
# Optionally update task config at the same time:
engine.unblock(task_id, context={"answer": "approved"}, config={"max_retries": 5})
```

1. The provided context is merged into the checkpoint's context.
2. If `config` is provided, it replaces the task's stored config.
3. Task status becomes `queued`.
4. Queue picks it up and resumes from the checkpoint (skipping already-completed subtasks/phases).

## Checkpoint and Resume

Checkpoints are saved at these points:
- After each batch completes.
- When the task is stopped.
- When a subtask is blocked.

A checkpoint contains:
- `workspace_path` — for workspace commands.
- `completed_subtasks` — list of finished subtasks.
- `pending_subtasks` — remaining subtasks.
- `context` — accumulated context dict.
- `batch_number` — current batch.
- `current_subtask_index` — for mid-batch resume.
- `current_subtask_step_index` — for mid-subtask resume.

On resume, the flow skips completed subtasks and completed steps within the current subtask.

## Stop Flow

`engine.stop()` works on any non-terminal task (queued, running, or blocked):
- **Running**: sets a threading event; the flow finishes the active step, saves checkpoint, then marks `stopped`.
- **Queued/Blocked**: marks `stopped` immediately without resuming.

The stop event is checked at safe points:
- Before starting a new subtask.
- Before running scope.
- After each subtask completes.

When the event is set:
1. Current step finishes (not interrupted mid-execution).
2. Checkpoint is saved.
3. Task status becomes `stopped`.

`stopped` is **non-terminal** and **resumable** — call `engine.unblock()` to re-queue it. Note: despite being non-terminal, `stopped` tasks can be deleted (unlike `queued`, `running`, or `blocked` tasks).

## Cancel

`engine.cancel()` works on any non-terminal task. Unlike stop, `cancelled` is terminal — the task cannot be resumed. If the task has a running thread, cancel waits for the active step to finish before marking cancelled.
