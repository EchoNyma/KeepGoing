# ChatGPT Desktop Task Auto-Resume Design

**Status:** Revised after user review, awaiting final approval

**Date:** 2026-08-31

**Scope:** Windows ChatGPT desktop app, local Codex tasks, five-hour Codex usage limit

## Problem statement

KeepGoing is intentionally started manually from a terminal. Once started, it currently attaches to one ChatGPT window and inspects the visible UI Automation tree. That model fails for Codex tasks that continue in the background because the rate-limit error can be persisted in a task that is not visible in the active window. Detection, waiting, and dispatch are all coupled to the active window instead of the task that actually failed.

The failure on 2026-08-31 demonstrates the scanning defect. KeepGoing was manually attached at 05:26. Later, the local task `01a03adb-25a3-75a1-b6de-0f54a18dfaa3` (`Bewerte Personal OS Konzept`) ended with a usage-limit error containing `try again at 9:27 AM`, while the KeepGoing log contained no matching detection, schedule, or dispatch before the PC was shut down at approximately 11:30. The shutdown and subsequent ChatGPT process-ID change were expected and are not the defect under investigation.

The current loop has three concrete reliability failures: it sees only the task rendered in the selected window, it blocks all further scanning while sleeping for one detected reset, and after the wait it dispatches through whichever task is visible then rather than through the identity of the task that originally failed.

The existing 24-test suite passes, but it covers parsers and low-level helpers rather than the end-to-end background-task path. Passing tests therefore do not establish that a hidden desktop task is detected or resumed.

## Confirmed platform behavior

Read-only probes against the local installation established the following:

- The globally installed Codex CLI 0.145.0 and the ChatGPT-bundled Codex CLI 0.151.0-alpha.7.2 can read the signed-in account's five-hour rate-limit bucket through `account/rateLimits/read`.
- The returned primary bucket has `windowDurationMins: 300` and an absolute Unix `resetsAt` timestamp. This is the authoritative five-hour reset source.
- Both App Server versions can read the affected desktop task from the shared Codex store.
- The affected task uses paginated history. The bundled App Server understands that format.
- The bundled App Server's `thread/list` endpoint is local and Codex-scoped but omits `hostId` and `kind`; `thread/read` omits them as well. KeepGoing requires the task ID from the response, uses the endpoint's fixed local/Codex scope only for missing fields, and rejects explicit scope conflicts.
- The desktop bridge's `read_thread` result currently exposes recent turn summaries and assistant items rather than the original user prompt item. Exact outbound `threadId`/`prompt` arguments and a successful bridge response are therefore persisted as the dispatch acknowledgement; when the user turn is exposed, the verifier still requires its exact text.
- A separate App Server cannot call `thread/resume` while the desktop app owns the task. The server correctly fails with `thread-store conflict: ... already has an active writer`.
- The desktop-bundled Codex App Tools MCP bridge can connect to the running desktop app's named pipe, list tools, and call `list_threads` read-only. Its tool catalog includes `read_thread`, `wait_threads`, and `send_message_to_thread`.

Consequently, a separate App Server is suitable for read-only rate-limit and task inspection, but not for writing to a desktop-owned task. Dispatch must go through the desktop app's own task bridge.

## Goals

1. Detect a newly failed local Codex task even when it is not visible in the ChatGPT window.
2. Identify the actual five-hour reset from the account rate-limit bucket instead of screen text or guessed delays.
3. Send exactly one `keep going` follow-up to the exact failed task after the reset is confirmed.
4. While the manually started monitor is running, survive ChatGPT restarts, sleep, and clock changes without losing or duplicating queued work. If the user manually restarts KeepGoing, restore its queued work.
5. Verify that the follow-up appears as a new user-visible turn in that task.
6. Avoid foreground-window activation, cursor movement, clipboard replacement, and global keyboard input during normal operation.
7. Preserve the current zero-third-party-Python-dependency requirement.

## Non-goals

- Automatically resuming ordinary consumer ChatGPT chats in the first release.
- Starting KeepGoing automatically at Windows logon, through a scheduled task, as a service, or when ChatGPT starts.
- Resuming weekly-limit failures as though they were five-hour failures.
- Redeeming a usage-reset credit.
- Purchasing credits, changing models, changing reasoning effort, or modifying task permissions.
- Continuing tasks that failed before the monitor's durable observation watermark unless the user explicitly requests recovery.
- Using the Personal OS task as a live integration-test target.
- Exposing the desktop app's named pipe or an App Server listener over the network.

## Architecture

The primary implementation is a long-running, manually started supervisor with four isolated adapters:

1. `AppServerClient` launches the exact Codex binary bundled with the running ChatGPT desktop app and uses JSONL App Server requests for read-only operations.
2. `DesktopTaskBridge` connects to the desktop-bundled Codex App Tools MCP bridge and uses the desktop app's own `send_message_to_thread` capability for dispatch.
3. `RetryStateStore` atomically persists observation watermarks and queued retries under the current user's local application-data directory.
4. `AutoResumeSupervisor` coordinates detection, reset confirmation, dispatch, verification, cancellation, and reconnects.

The existing `chatgpt_attach.py` remains the command entry point for backward compatibility. It becomes a thin CLI around the supervisor. Existing UI Automation code remains available only behind an explicit legacy mode and is never selected automatically.

## Component boundaries

### AppServerClient

Responsibilities:

- Discover the active ChatGPT main process and its child `codex.exe` process.
- Resolve the bundled Codex executable from that verified process rather than from global `PATH`.
- Start a private stdio App Server subprocess; do not open a TCP, WebSocket, or Unix-socket listener.
- Perform `initialize` and validate the returned Codex version and Codex home.
- Call `account/rateLimits/read`.
- List recent local Codex tasks and inspect recent paginated turns through the bundled server's supported read APIs.
- Surface typed protocol, process-exit, timeout, and unsupported-method errors.

The client must never call `thread/resume`, `turn/start`, `thread/inject_items`, filesystem-write methods, account login/logout, or reset-credit consumption.

### DesktopTaskBridge

Responsibilities:

- Discover `CODEX_APP_TOOLS_PIPE_PATH`, the bundled Node runtime, and `server.mjs` from the verified ChatGPT-owned Codex process configuration.
- Validate the process ancestry and installation paths before trusting a pipe. The Codex process must descend from the active packaged ChatGPT app, and the bridge resources must be under that package or the app-managed Codex runtime directory.
- Launch the bundled bridge over stdio and complete the MCP handshake.
- Call `tools/list` and require the exact tools used by the supervisor.
- Invoke `send_message_to_thread` with only `threadId`, `hostId` when known, and `prompt`.
- Supply the desktop app's required executor thread context as MCP request metadata. The executor context is explicit (`--executor-thread-id` or `KEEPGOING_EXECUTOR_THREAD_ID`) and is kept separate from the exact target task ID in the tool arguments; KeepGoing never invents a controller task.
- Re-discover and reconnect after any ChatGPT or bridge restart.

The bridge must not invoke automation creation, task creation, task deletion, archiving, handoff, usage-reset consumption, or any unrelated app tool.

### RetryStateStore

The store lives at `%LOCALAPPDATA%\KeepGoing\chatgpt-auto-resume-state.json`. Writes use a same-directory temporary file, flush, and atomic replace. A named mutex prevents two supervisors from modifying the state concurrently.

The versioned state contains:

```json
{
  "schema_version": 1,
  "observation_watermark_ms": 0,
  "queue": [
    {
      "key": "<thread-id>:<failed-turn-id>",
      "thread_id": "<thread-id>",
      "host_id": "local",
      "failed_turn_id": "<turn-id>",
      "failed_at_ms": 0,
      "reset_at_epoch": 0,
      "window_duration_minutes": 300,
      "prompt": "keep going",
      "state": "waiting_for_reset",
      "dispatch_attempts": 0,
      "dispatch_confirmed": false,
      "last_error": null,
      "created_at_ms": 0,
      "updated_at_ms": 0
    }
  ]
}
```

Valid queue states are `waiting_for_reset`, `ready`, `dispatching`, `verifying`, `completed`, `cancelled`, and `needs_attention`. Terminal entries may be compacted after seven days, while their keys remain in a bounded deduplication ledger for 30 days.

### AutoResumeSupervisor

Responsibilities:

- Establish a baseline watermark on the first monitor start so old failures are not resumed unexpectedly.
- Poll recent local Codex tasks and their latest turns at a modest cadence, initially 15 seconds with jitter.
- Recognize a five-hour limit only when a failed turn contains a server-classified Codex usage-limit error or a narrowly matched usage-limit message and the account exposes a 300-minute primary bucket.
- Queue the pair `(thread_id, failed_turn_id)` exactly once.
- Prefer the bucket's absolute `resetsAt` value. A timestamp parsed from the failed-turn message is diagnostic fallback only.
- Wait using wall-clock comparisons against the stored Unix epoch, not one uninterrupted multi-hour sleep.
- At reset, refresh `account/rateLimits/read` until the 300-minute bucket is no longer reached and the previous reset epoch has passed. Apply the configured post-reset margin, default 60 seconds.
- Before dispatch, inspect the target again. Cancel the queue entry if the user already added a newer turn, if the task is active, if it is waiting for approval or user input, or if the failed turn is no longer the latest unresolved limit failure.
- Dispatch one queued task at a time in FIFO order.
- Persist `dispatching` before the tool call. On an uncertain result, verify before retrying.
- Verify a successful dispatch by observing a newer user turn containing the exact configured continuation text and then a started or active assistant turn when the bridge exposes user items. With the current turn-summary bridge contract, require the persisted exact-argument dispatch acknowledgement plus newer non-empty active/completed task progress; an uncertain dispatch without an exact user turn never uses generic progress as proof.
- Retry an unverified dispatch at most three times with bounded exponential backoff. Never send a second copy when verification finds the first copy.
- If a resumed task immediately hits another five-hour limit, create a new queue item for its new failed turn and new reset epoch.

## Detection rules

The first release watches only entries that satisfy all of the following:

- `kind` is `codex`.
- `hostId` is `local`.
- The turn status is `failed`.
- The failure is classified as a usage/rate limit or matches an anchored allowlist equivalent to `You've hit your usage limit ... try again at ...`.
- The failure occurred after the durable observation watermark, or its key is already present in a non-terminal persisted queue entry.

Messages mentioning limits in normal conversation do not qualify. Weekly reset text does not qualify for a five-hour retry. The current behavior that caps a multi-day reset to five hours is removed from the primary path because it can retry before a weekly limit clears.

## Restart and recovery behavior

- KeepGoing starts only when the user runs `keepgoing-chatgpt` and stops when that process is terminated. It does not register or launch another background instance.
- If ChatGPT is not running while the manually started monitor remains alive, the supervisor waits with a bounded polling backoff and performs no UI actions.
- If ChatGPT restarts while the monitor remains alive, the supervisor discards stale process, pipe, and protocol handles and performs full verified discovery again.
- If Windows sleeps past a reset, the next loop refreshes the account bucket and resumes only after the reset gate succeeds.
- If the user stops and later manually starts KeepGoing while an item is waiting, it restores the queue and continues from the persisted state.
- A Windows shutdown ends KeepGoing normally. Nothing starts it automatically after the next boot.
- A `dispatching` or `verifying` item found after a crash is verified first; it is never blindly resent.
- Corrupt state is copied to a timestamped diagnostic file, the supervisor fails closed for pending dispatch, and the user receives an actionable log message. It does not silently replace unknown queue state with an empty queue.

## Manual lifecycle

`keepgoing-chatgpt` starts the monitor in the invoking terminal and prints a clear connected/monitoring status. `Ctrl+C` stops it. There is no `--install`, scheduled task, Windows service, startup entry, or implicit launch from ChatGPT. An optional `--status` command may report the persisted queue and last connection state without starting a monitor or exposing prompt content from other tasks.

## Logging and privacy

Human-readable and JSONL logs are written under `%LOCALAPPDATA%\KeepGoing\logs`. Logs contain timestamps, protocol versions, process IDs, task IDs, turn IDs, queue transitions, reset epochs, and sanitized error classes. They do not contain task transcripts, agent reasoning, authentication tokens, named-pipe payloads, clipboard data, or arbitrary scraped UI text.

Every dispatch has one audit chain: detection, queued reset, reset confirmation, preflight result, tool-call result, and verification result.

## Backward compatibility

- The npm command `keepgoing-chatgpt` remains valid.
- `--text`, `--margin`, and `--log-path` remain supported where meaningful.
- Manual terminal startup remains the only supported startup model.
- Positional process-ID targeting and UI Automation monitoring move behind `--legacy-uia` and are documented as compatibility behavior.
- The primary monitor does not import or initialize COM UI Automation.
- Existing uncommitted Gemini-authored files are preserved and evolved rather than discarded.
- Python remains standard-library-only; Node is supplied by the ChatGPT desktop installation and is not added as an npm dependency.

## Testing strategy

### Unit tests

- JSONL App Server request/response correlation, notification handling, timeout, and subprocess exit.
- MCP handshake, tool discovery, framed named-pipe bridge failures, and sanitized errors.
- Five-hour bucket selection using `windowDurationMins == 300`.
- Failure classification for exact English and German server messages and rejection of ordinary conversation text.
- Queue state transitions, atomic persistence, crash recovery, deduplication, and corrupt-state handling.
- Reset gating across local midnight, daylight-saving transitions, system sleep, and clock changes by injecting a clock.
- Manual-resume cancellation and active-task preflight cancellation.
- Uncertain dispatch followed by successful verification without a duplicate send.
- Multiple queued tasks processed sequentially.
- App restart and pipe rotation with successful reconnect.

### Contract tests

- Generate or inspect the schema from the discovered bundled Codex version and verify required read methods before monitoring.
- Validate that the desktop bridge advertises `list_threads`, `read_thread` or an equivalent inspection tool, `wait_threads`, and `send_message_to_thread` before enabling dispatch.
- Verify that the global CLI is never selected when it differs from the desktop-bundled version.
- Verify that no primary-path test calls focus, mouse, clipboard, `SendInput`, or UIA functions.

### Integration tests

- Fake App Server and fake desktop bridge processes exercise the complete supervisor without external state.
- A read-only live smoke test checks authentication, the 300-minute rate-limit bucket, task listing, and required desktop tools.
- A controlled live dispatch smoke test uses a dedicated harmless temporary Codex task, sends a harmless continuation, verifies that the message and new turn appear through the desktop task APIs, and archives the test task afterward. It never targets `Bewerte Personal OS Konzept` or another user work task.
- The live test must not manufacture or wait for a real five-hour limit. It injects a ready queue item only for the dedicated test task.

## Acceptance criteria

Implementation is complete only when all of the following are demonstrated:

1. A hidden simulated Codex task failure is detected without making its window visible.
2. Scheduling uses the account's absolute 300-minute `resetsAt` value.
3. Stopping and manually restarting KeepGoing during the wait preserves the queue and produces one follow-up.
4. Restarting ChatGPT rotates the pipe and the supervisor reconnects automatically.
5. A manual user continuation before reset cancels the queued automatic continuation.
6. An uncertain send cannot produce duplicate `keep going` messages.
7. Multiple failures are resumed sequentially rather than as a burst.
8. Normal operation does not move the mouse, change focus, touch the clipboard, or send global keystrokes.
9. The dedicated end-to-end task receives exactly one message and starts a new turn visible through the desktop app's task system.
10. All existing and new automated tests pass, and logs show the full audit chain without transcript content.
11. Normal execution creates no scheduled task, service, startup entry, or other automatic launcher.

## Rollout

1. Build the protocol clients and deterministic supervisor behind the existing manually invoked `keepgoing-chatgpt` command.
2. Run unit and fake-process integration tests.
3. Run the read-only live smoke test.
4. Run the dedicated temporary-task dispatch smoke test.
5. Observe the first naturally occurring five-hour failure while the user-started monitor is running and retain its sanitized audit chain for confirmation.
6. Keep legacy UIA opt-in available until one natural-limit cycle has succeeded, then reassess whether it can be removed in a later release.

## Failure policy

When required methods, trusted process ancestry, the 300-minute bucket, task identity, or post-dispatch verification are unavailable, KeepGoing fails closed and records `needs_attention`. It never substitutes a guessed task, guessed reset, arbitrary active window, or global keyboard input.
