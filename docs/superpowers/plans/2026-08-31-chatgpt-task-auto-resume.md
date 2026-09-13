# Reliable ChatGPT Task Auto-Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the manually started `keepgoing-chatgpt` monitor reliably detect a local Codex task that failed on the five-hour usage limit, wait for the authoritative reset, inject exactly one `keep going` message into that exact task through the ChatGPT desktop app, and verify that the task resumed.

**Architecture:** Replace the primary visible-window/UIA loop with a task-aware supervisor. A private bundled Codex App Server is read-only and supplies task failures plus the account's 300-minute reset bucket. The ChatGPT desktop app's own Codex App Tools bridge is the sole writer and sends to a stored thread ID. Durable queue state makes detection, reset waiting, dispatch, and verification independent and non-blocking. KeepGoing remains manual-only.

**Tech stack:** Python 3 standard library, Windows process APIs/PowerShell process discovery, JSONL Codex App Server protocol, stdio MCP bridge, `unittest`, Node launcher already bundled by this repository.

**Approved spec:** `docs/superpowers/specs/2026-08-31-chatgpt-task-auto-resume-design.md`

## Global constraints

- Preserve the user's existing uncommitted Gemini-authored `README.md`, `package.json`, `bin/chatgpt.js`, `chatgpt_attach.py`, and `tests/test_chatgpt_attach.py` work. Inspect diffs before every commit and never discard unrelated edits.
- Keep manual terminal startup as the only startup path. Do not create a scheduled task, service, registry startup entry, automation, or implicit ChatGPT launcher.
- Do not send a test message to `Bewerte Personal OS Konzept` (`01a03adb-25a3-75a1-b6de-0f54a18dfaa3`) or any other existing user work task.
- Do not redeem usage-reset credits, buy credits, change models, or change task permissions.
- The primary path must never focus a window, move the mouse, use the clipboard, call `SendInput`, or initialize UI Automation.
- Every behavior change starts with a failing automated test. Run the focused test, implement the minimum code, rerun it, and commit only after it passes.
- Treat live protocols as versioned and untrusted. Validate capabilities at startup and fail closed when required methods or task identity are unavailable.
- Do not declare completion from unit tests alone. Completion requires the fake end-to-end suite, read-only live protocol checks, and one controlled real desktop-bridge dispatch to a dedicated disposable test task.

## Target file layout

```text
chatgpt_attach.py                         # thin manual CLI entry point
keepgoing_chatgpt/
  __init__.py
  models.py                              # typed queue/task/rate-limit records
  jsonrpc.py                             # correlated JSONL request client
  discovery.py                           # trusted ChatGPT/Codex/bridge discovery
  app_server.py                          # read-only App Server adapter
  desktop_bridge.py                      # MCP handshake and exact-task writer
  state.py                               # atomic queue/watermark persistence
  supervisor.py                          # non-blocking state machine
  cli.py                                 # arguments, logs, manual lifecycle
  legacy_uia.py                          # current UIA implementation, opt-in only
tests/
  fixtures/
    app_server_initialize.jsonl
    app_server_rate_limits.jsonl
    app_server_failed_thread.jsonl
    bridge_initialize.jsonl
    bridge_tools.jsonl
  fake_protocol_peer.py
  test_models.py
  test_jsonrpc.py
  test_discovery.py
  test_app_server.py
  test_desktop_bridge.py
  test_state.py
  test_supervisor.py
  test_cli.py
  test_chatgpt_integration.py
  test_chatgpt_attach.py                  # retained legacy behavior tests
  live_chatgpt_smoke.py                   # opt-in live validation
```

## Task 1: Freeze the regression and establish a clean baseline

**Files:**

- Modify: `tests/test_chatgpt_attach.py`
- Create: `tests/test_models.py`
- Create: `keepgoing_chatgpt/__init__.py`
- Create: `keepgoing_chatgpt/models.py`

- [ ] Record `git status --short`, the current `chatgpt_attach.py` checksum, and the passing baseline without changing user files:

  ```powershell
  git status --short
  Get-FileHash .\chatgpt_attach.py -Algorithm SHA256
  python -m unittest discover -s tests -v
  ```

- [ ] Add immutable dataclasses/enums for `RateLimitBucket`, `FailedTask`, `RetryItem`, and `RetryState`. Queue identity must be `thread_id:failed_turn_id`; queue states must match the approved spec.
- [ ] Write failing tests proving that two failures from different hidden tasks remain separate, a duplicate failed turn produces the same key, and a stored item always retains its originating thread ID.
- [ ] Implement only the model behavior required by those tests and rerun:

  ```powershell
  python -m unittest tests.test_models -v
  python -m unittest discover -s tests -v
  ```

- [ ] Commit the model foundation without folding in unrelated README/package changes:

  ```powershell
  git add keepgoing_chatgpt/__init__.py keepgoing_chatgpt/models.py tests/test_models.py
  git commit -m "feat(chatgpt): model task-aware retry state"
  ```

## Task 2: Build a deterministic JSONL protocol client

**Files:**

- Create: `keepgoing_chatgpt/jsonrpc.py`
- Create: `tests/fake_protocol_peer.py`
- Create: `tests/test_jsonrpc.py`

- [ ] Write failing tests for request-ID correlation, interleaved notifications, structured remote errors, malformed JSON, timeout, stderr capture with secret redaction, and child-process exit.
- [ ] Implement a standard-library subprocess client with a dedicated stdout reader, monotonically increasing IDs, per-request waiters, bounded timeouts, graceful shutdown, and sanitized exceptions. Never use shell string interpolation to launch a protocol child.
- [ ] Prove a timeout does not poison later responses and that shutdown leaves no reader thread or process alive.
- [ ] Run and commit:

  ```powershell
  python -m unittest tests.test_jsonrpc -v
  git add keepgoing_chatgpt/jsonrpc.py tests/fake_protocol_peer.py tests/test_jsonrpc.py
  git commit -m "feat(chatgpt): add correlated JSONL client"
  ```

## Task 3: Discover and verify the desktop-owned runtime

**Files:**

- Create: `keepgoing_chatgpt/discovery.py`
- Create: `tests/test_discovery.py`

- [ ] Write failing tests around captured, sanitized process records for: packaged `ChatGPT.exe`; its descendant bundled `codex.exe`; a misleading global Codex binary; a stale process; an untrusted named pipe; and paths outside the package/app-managed runtime.
- [ ] Implement discovery that reads process ID, parent process ID, executable path, and command line without UI Automation. Resolve the exact bundled Codex executable, bundled Node executable, App Tools `server.mjs`, and `CODEX_APP_TOOLS_PIPE_PATH` only from a verified ChatGPT descendant.
- [ ] Require all resolved executables/resources to exist and validate their trusted roots. Return a typed discovery snapshot; never silently fall back to global `PATH`.
- [ ] Add a read-only live diagnostic command used by tests to print only versions and sanitized paths, never the pipe payload or authentication material.
- [ ] Run and commit:

  ```powershell
  python -m unittest tests.test_discovery -v
  git add keepgoing_chatgpt/discovery.py tests/test_discovery.py
  git commit -m "feat(chatgpt): discover trusted desktop runtime"
  ```

## Task 4: Add the read-only App Server adapter

**Files:**

- Create: `keepgoing_chatgpt/app_server.py`
- Create: `tests/test_app_server.py`
- Create: `tests/fixtures/app_server_initialize.jsonl`
- Create: `tests/fixtures/app_server_rate_limits.jsonl`
- Create: `tests/fixtures/app_server_failed_thread.jsonl`

- [ ] Capture sanitized responses from the bundled App Server for initialize, account rate limits, thread listing, and paginated thread reading. Fixtures must omit transcript content unrelated to classification.
- [ ] Write failing contract tests that require the desktop-bundled version, select only the primary bucket where `windowDurationMins == 300`, preserve absolute `resetsAt`, read paginated task history, and reject weekly-only or ordinary conversational mentions.
- [ ] Implement only read operations. Maintain an explicit method allowlist and make any attempted write method (`thread/resume`, `turn/start`, reset-credit consumption, account mutation) raise locally before a request is sent.
- [ ] Classify the historical affected task as a five-hour failure in a read-only replay fixture while retaining its exact thread and failed-turn IDs.
- [ ] Run and commit:

  ```powershell
  python -m unittest tests.test_app_server -v
  git add keepgoing_chatgpt/app_server.py tests/test_app_server.py tests/fixtures/app_server_*.jsonl
  git commit -m "feat(chatgpt): read task failures and rate limits"
  ```

## Task 5: Add the desktop task bridge writer

**Files:**

- Create: `keepgoing_chatgpt/desktop_bridge.py`
- Create: `tests/test_desktop_bridge.py`
- Create: `tests/fixtures/bridge_initialize.jsonl`
- Create: `tests/fixtures/bridge_tools.jsonl`

- [ ] Write failing tests for MCP initialization, tool discovery, missing `send_message_to_thread`, exact argument forwarding, structured tool errors, bridge exit, reconnect, and denial of every non-allowlisted tool.
- [ ] Implement bridge launch through the verified bundled Node and `server.mjs`, passing the verified named-pipe environment. Require `list_threads`, `read_thread` or its supported equivalent, `wait_threads`, and `send_message_to_thread` before enabling writes.
- [ ] Expose one write method only:

  ```python
  send_message(thread_id: str, prompt: str, host_id: str | None = None)
  ```

  It must serialize exactly `threadId`, `prompt`, and optional `hostId`; model/thinking overrides and unrelated tools are forbidden.
- [ ] Add read helpers for exact-task preflight and post-send verification. Ensure an uncertain transport result is surfaced distinctly from a confirmed rejection.
- [ ] Run and commit:

  ```powershell
  python -m unittest tests.test_desktop_bridge -v
  git add keepgoing_chatgpt/desktop_bridge.py tests/test_desktop_bridge.py tests/fixtures/bridge_*.jsonl
  git commit -m "feat(chatgpt): send through exact desktop task bridge"
  ```

## Task 6: Persist queue state atomically and safely

**Files:**

- Create: `keepgoing_chatgpt/state.py`
- Create: `tests/test_state.py`

- [ ] Write failing tests for first-run watermark creation, atomic replacement, interruption before replace, schema-version rejection, corrupt JSON quarantine, mutex exclusion, terminal-entry compaction, and round-tripping every queue state.
- [ ] Implement `%LOCALAPPDATA%\KeepGoing\chatgpt-auto-resume-state.json` storage with a same-directory temporary file, flush, `fsync`, atomic `os.replace`, and a named Windows mutex. Do not erase a corrupt/unknown pending queue; quarantine it and fail closed.
- [ ] Persist state before and after every externally visible transition, especially before dispatch.
- [ ] Run and commit:

  ```powershell
  python -m unittest tests.test_state -v
  git add keepgoing_chatgpt/state.py tests/test_state.py
  git commit -m "feat(chatgpt): persist retry queue atomically"
  ```

## Task 7: Implement the non-blocking supervisor state machine

**Files:**

- Create: `keepgoing_chatgpt/supervisor.py`
- Create: `tests/test_supervisor.py`

- [ ] Start with a fake clock and fake adapters. Write failing tests proving:

  - all recent local Codex tasks are scanned even when none is visible;
  - scanning continues while another item waits several hours;
  - each failure retains its originating thread ID;
  - only a 300-minute account bucket schedules a five-hour retry;
  - the absolute reset epoch, not a guessed sleep, gates readiness;
  - sleep/clock jumps are handled by wall-clock reevaluation;
  - a newer user turn, active task, approval request, or user-input request cancels automatic dispatch;
  - multiple ready items dispatch sequentially;
  - a second limit creates a new key from the new failed turn;
  - ChatGPT/bridge loss disconnects and later reconnects without losing queue state.

- [ ] Implement a short-tick state machine. No transition may block the scan loop for the reset duration. Each tick performs bounded discovery/scan, reset refresh, one dispatch decision, one verification decision, and persistence as needed.
- [ ] Enforce FIFO dispatch and bounded exponential retry. A transport-uncertain send moves to verification, never directly back to send.
- [ ] Log sanitized audit events for detection, queueing, reset confirmation, preflight, dispatch result, and verification.
- [ ] Run and commit:

  ```powershell
  python -m unittest tests.test_supervisor -v
  git add keepgoing_chatgpt/supervisor.py tests/test_supervisor.py
  git commit -m "feat(chatgpt): coordinate reliable task auto-resume"
  ```

## Task 8: Prove exactly-once dispatch and verification

**Files:**

- Modify: `keepgoing_chatgpt/supervisor.py`
- Modify: `tests/test_supervisor.py`

- [ ] Add failing tests for confirmed send, uncertain send whose message actually arrived, uncertain send that did not arrive, restart from `dispatching`, restart from `verifying`, duplicate user text from an older turn, and three failed verification attempts.
- [ ] Verification must require a newer user turn on the exact thread containing the exact configured continuation text, followed by a newer started/active assistant turn or equivalent task progress. Older identical messages do not count.
- [ ] Mark `completed` only after verification. Mark `needs_attention` after the retry budget. Never produce a second message if the first can be observed.
- [ ] Run and commit:

  ```powershell
  python -m unittest tests.test_supervisor -v
  git add keepgoing_chatgpt/supervisor.py tests/test_supervisor.py
  git commit -m "fix(chatgpt): guarantee verified single dispatch"
  ```

## Task 9: Replace the primary CLI while preserving opt-in legacy UIA

**Files:**

- Create: `keepgoing_chatgpt/cli.py`
- Create: `keepgoing_chatgpt/legacy_uia.py`
- Replace: `chatgpt_attach.py`
- Modify: `tests/test_chatgpt_attach.py`
- Create: `tests/test_cli.py`
- Modify: `bin/chatgpt.js`

- [ ] Move the existing Gemini UIA implementation mechanically into `legacy_uia.py`; preserve its tests and behavior under `--legacy-uia`. Do not redesign it during the move.
- [ ] Replace `chatgpt_attach.py` with a tiny import-safe entry point that imports `keepgoing_chatgpt.cli` only. The primary process must not import `legacy_uia`, initialize COM, or bind UI input APIs.
- [ ] Write failing CLI tests for manual start, `Ctrl+C`, `--status`, `--legacy-uia`, configuration validation, duplicate-monitor mutex behavior, and absence of `--install`/`--uninstall`.
- [ ] Keep `--text`, `--margin`, and `--log-path`. The default invocation runs the new supervisor in the current terminal and clearly prints connected, scanning, queued, and stopped states.
- [ ] Update the Node launcher only as needed to preserve arguments and exit codes.
- [ ] Run and commit:

  ```powershell
  python -m unittest tests.test_cli tests.test_chatgpt_attach -v
  python -m unittest discover -s tests -v
  git add chatgpt_attach.py keepgoing_chatgpt/cli.py keepgoing_chatgpt/legacy_uia.py tests/test_cli.py tests/test_chatgpt_attach.py bin/chatgpt.js
  git commit -m "feat(chatgpt): make task-aware monitor the manual default"
  ```

## Task 10: Add a complete fake-process end-to-end test

**Files:**

- Create: `tests/test_chatgpt_integration.py`
- Modify: `tests/fake_protocol_peer.py`

- [ ] Build one deterministic integration scenario with fake App Server and fake bridge subprocesses:

  1. Two tasks exist; only a hidden one fails on the five-hour limit.
  2. The account bucket returns an absolute future reset.
  3. The clock advances while scanning continues.
  4. The bucket clears after reset plus margin.
  5. Preflight confirms no manual continuation.
  6. The bridge records one message for the hidden task's exact ID.
  7. The first send response is deliberately lost although the message is stored.
  8. Verification observes the message and assistant progress.
  9. The queue completes without a duplicate send.

- [ ] Assert that the visible task receives zero messages and that no UIA, focus, mouse, clipboard, or keyboard function is imported/called.
- [ ] Add a second scenario where the user manually continues before reset and assert zero automatic messages.
- [ ] Run the integration test repeatedly to expose timing races:

  ```powershell
  1..20 | ForEach-Object { python -m unittest tests.test_chatgpt_integration -v; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
  ```

- [ ] Commit:

  ```powershell
  git add tests/fake_protocol_peer.py tests/test_chatgpt_integration.py
  git commit -m "test(chatgpt): cover complete auto-resume flow"
  ```

## Task 11: Perform controlled live protocol and dispatch validation

**Files:**

- Create or modify: `tests/live_chatgpt_smoke.py`
- Modify only if a defect is found: relevant adapter/supervisor files and tests

- [ ] Add explicit flags so the live script defaults to read-only. A real send requires both `--allow-dispatch` and a dedicated disposable task ID created for this test.
- [ ] Run read-only validation against the current desktop app:

  - verify the bundled App Server version and account identity;
  - read the 300-minute bucket and absolute `resetsAt`;
  - list/read tasks including paginated history;
  - confirm required desktop bridge tools;
  - replay classification of the historical affected failure without writing to it.

- [ ] Outside the KeepGoing implementation, use the Codex app's task-creation tool to create a dedicated harmless temporary local task whose first prompt asks for a short completion and stop. Give it a title that clearly marks it as a KeepGoing smoke test, wait until its first turn is complete, and pass only that new task ID to the live script. Never use an existing task, and do not add task creation to `DesktopTaskBridge`.
- [ ] Seed a temporary isolated state directory with one `ready` retry item targeting only that test task. Run one supervisor cycle with real adapters, send `keep going`, and verify through the desktop task API that exactly one user message appears and a new assistant turn starts.
- [ ] Query the test task again after a bounded delay and confirm there is still exactly one injected message. Outside the KeepGoing implementation, archive the disposable test task with the Codex app's archive tool after evidence is captured; do not add archival permission to `DesktopTaskBridge`.
- [ ] If any live step fails, first add a deterministic regression test, then fix and rerun the focused, full, fake end-to-end, and live checks. Do not weaken validation to make the smoke test pass.
- [ ] Save only sanitized evidence (versions, task IDs for the disposable test, transition names, counts, timestamps, and error classes); do not save task transcripts or pipe contents.

## Task 12: Documentation, package integrity, and final verification

**Files:**

- Modify: `README.md`
- Modify: `package.json`
- Modify: `.gitignore` if new runtime artifacts require it
- Modify: `docs/superpowers/specs/2026-08-31-chatgpt-task-auto-resume-design.md` only if verified protocol facts require correction

- [ ] Update documentation to describe manual startup, task-aware scanning, exact-task injection, persisted queue behavior, logs, `--status`, and `--legacy-uia`. Explicitly state that no autostart is installed.
- [ ] Make `npm test` execute the Python test suite and ensure package `files` includes the new package and excludes test/runtime state.
- [ ] Run final static and behavioral checks:

  ```powershell
  python -m compileall -q chatgpt_attach.py keepgoing_chatgpt tests
  python -m unittest discover -s tests -v
  npm test
  git diff --check
  git status --short
  ```

- [ ] Repeat the fake end-to-end test 20 times after the final code state.
- [ ] Run the read-only live smoke test once more. Run the controlled disposable-task dispatch once more only if implementation changed after its first successful run.
- [ ] Inspect the final diff for accidental UI input in the primary path, automatic-start mechanisms, secrets, unrelated user changes, and broad task permissions.
- [ ] Request a code review using `superpowers:requesting-code-review`; resolve findings with `superpowers:receiving-code-review` and rerun all affected checks.
- [ ] Commit the final documentation/package changes:

  ```powershell
  git add README.md package.json .gitignore keepgoing_chatgpt chatgpt_attach.py bin/chatgpt.js tests
  git commit -m "docs(chatgpt): document reliable manual auto-resume"
  ```

## Completion evidence

The worker must return all of the following before claiming success:

- commit list and final `git status --short`;
- full unit-suite and `npm test` results;
- 20/20 fake end-to-end passes;
- read-only live App Server and bridge capability results;
- controlled disposable-task dispatch evidence showing the exact target ID, one injected message, observed assistant progress, no duplicate, and successful archival;
- confirmation that the historical Personal OS task was read only and received no test message;
- confirmation that no scheduled task, service, startup entry, reset-credit action, focus/mouse/clipboard/keyboard input, or global Codex fallback was used by the primary path.
