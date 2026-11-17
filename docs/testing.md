# Testing

## Run

- `scripts/test.sh --unit`: Python subprocess/protocol tests and Node rendering tests.
- `scripts/test.sh`: configure, build, and run CTest; prefers an isolated Xvfb display.
- `build/test-results.xml`: JUnit report. Native tests save previews in `build/`.

The runner serializes native builds and clears any ambient live-test opt-in. Missing
Node or Qt Test fails test configuration instead of silently omitting a suite.
The Node `module.registerHooks` capability is checked before tests run. The runner
uses Node from `PATH`, or `KATE_AGENT_TEST_NODE`; direct CMake builds can set
`-DNODE_EXECUTABLE=/path/to/node`.
Each subprocess test has a deadline and closes its children. CI runs the offline
suite; no agent package or credential is required. Native tests replace agent
commands with fixtures, including when real CLIs are installed on the host.

Local validation uses Fedora 45, Qt 6.11.2, and KF6 6.31. The CI matrix covers
Python/Node pairs 3.10/22.15, 3.13/24, and 3.14/26, plus the Fedora 43 native suite.
The current offline run passes all five CTest suites, including 102 Python tests
and the native editor/panel checks; the two real-provider Qt cases are opt-in. Other platforms
remain unverified.

First-open regression coverage watches the host's native surface, window ID, and
surface type while WebEngine is created. The original implementation destroys the
host surface once; the fixed implementation preserves it. A GNOME/Wayland run also
observed zero new Kate windows on first open and checked the dark theme, sidebar,
manual newlines, and Esc. Escape remains local to modal dialogs and menus.

## Coverage

Session filtering regressions cover initialization/configuration noise, interrupted first
prompts, migration of an existing SQLite index, retained selection/history, and native
Codex/Pi/Claude/DeepSeek/ZCode/Hermes/Kimi metadata. More than 2000 empty Codex threads
cannot hide an older used thread behind the SQL discovery limit. Native fixture files
and databases are compared byte for byte to confirm discovery never modifies them.

| Layer | Checks |
|---|---|
| Discovery | DSH priority, unavailable-agent fallback, resume precedence, command overrides, argv quoting, malformed overrides, numeric NVM ordering, inherited PATH priority, isolated environments, credential-name-only inventory, existing Harness profile requirement |
| Transport | Fragmented UTF-8, noisy stdout, oversized records, malformed input, EOF cleanup, busy sends, ACP cancellation, permission races, unresponsive cancellation, process-group termination, kill escalation, inherited-pipe shutdown |
| Sessions | All seven adapters; persistent turns; canonical workspace filtering; read-only metadata imports; cache restart; New Chat; unavailable agents; stale discovery; ACP capability-dependent resume; UTF-8 history limits |
| Parameters | Native default readback, queued sends during blocked preparation, idle model/effort edits, rejected-value recovery, native changes overriding cached settings, session persistence, ACP updates and legacy model IDs, Codex empty-thread recreation, ZCode model-default reasoning |
| Interaction | Single/multiple/custom/secret answers, required fields, typed values, dismiss, failed-write retry, permissions, Codex MCP elicitation, server-resolved requests, DSH socket/hook lifecycle, special-character helper paths, existing Node preloads |
| Native editor | Deferred startup, hidden/visible session restore, host palette/layout preservation, load/unload, resize/hide/show, icon fallbacks, shortcuts, split-view context, Pending, failure recovery, file-link clicks |
| Focus | First-click composer focus, Esc from composer/transcript/workspace/Pending, dialog dismissal, return to editor, draft/context retention, focus on reopening |
| Transcript | Console prompt/body alignment at multiple font sizes, ACP message IDs and legacy chunks, chronological process groups, late tool updates, visible errors, pinned one/two-line composer, inline Send/Stop, small muted working/elapsed text, Enter/Ctrl+Enter, IME protection, scroll retention, theme/font updates, reload, restored history, local-session dialog |
| Workspace | Active Kate project, local file directory, HOME fallback, New Chat after a manual directory override |
| Composer regression | Repeated Ctrl+Enter in existing multiline text, cursor visibility, undo/redo, wrap thresholds, padded styles, font fallback, bounded repaint activity, distinct icons and readable completion hints |
| Timing | Monotonic wall time, overlapping tool union, user-input waits, zero-duration turns, stop/error outcomes, one persisted summary per turn |
| Files | Workspace/absolute/file URLs, Unicode/space/percent paths, line/column/range selection, generated round trips, invalid locations and schemes |
| Rendering | Four math delimiters, matrices, aligned equations, lists/quotes, streaming, errors, code isolation, nested links, KaTeX fonts, escaped HTML and blocked network access |
| Packaging | Matching release/plugin/protocol versions, local HTML/font dependencies, licenses, English first-party text |

Qt integration tests use real KTextEditor documents/views and WebEngine with a
small MainWindow test host. They do not replace a manual check in a full Kate window.
The host models Kate's existing layout and automatic child insertion. A full Kate
26.08.2 window was also checked locally under Xvfb at 100% and 200% scaling,
including a dark GTK theme. GNOME 51.1 / Wayland was checked in an isolated headless
session with GPU acceleration: plugin disabled, enabled with its panel hidden, and
restored with its panel visible. Both GTK dark colors and Kate's Breeze Dark scheme
were checked, including the document tree, toolbar icons, and Esc.

The pre-fix startup path reproduced a light sidebar/toolbar with unreadable white
icons when Kate selected Breeze Dark over the GTK platform theme. Deferring
WebEngine initialization until after window restoration preserved the dark theme.
For a manual regression check, select Breeze Dark, enable Agent Panel, restart Kate
with the panel hidden and visible, then open it and press Esc from the composer and
transcript. Check the document tree and toolbar before and after opening the panel.

The multiline composer regression also passed at 200% scaling and in a full Kate
window on GNOME/Wayland. The reported complete freeze was not reproduced locally;
the fix removes synchronous resizing from document/repaint callbacks and tests
cursor visibility after repeated manual line breaks.

### Dock shrinking regression (2026-11-17)

The panel uses a native RHI surface to protect Kate's main window during WebEngine
initialization. Under Qt/Wayland that child surface can intercept pointer motion
while dragging the parent dock splitter downward. The Voice Panel fix now also
applies here: the child surface temporarily becomes transparent to input for an
ancestor splitter drag, then restores its original flags on release, hide, mouse
ungrab, deactivation, or plugin teardown. Unrelated splitters are unaffected.

The dock layout also derives its minimum height from the actual controls rather
than the current/restored ToolView size. It preserves room for the composer and
Pending while allowing an enlarged dock to shrink again.

The two input-region tests failed before the fix and passed afterwards in an
isolated headless Mutter/Wayland session. They check release and hide during a
drag, followed by successful composer input. Repeated enlargement/shrinking and
draft preservation pass as well. These Wayland cases are skipped under Xvfb; the
complete five-suite offline run passes under Xvfb, with the live-provider cases
remaining opt-in.

Unicode cases use escaped data; project UI, documentation, and test descriptions are English.

The Stop regression was reproduced against the old implementation on Python 3.13:
an agent ignoring SIGTERM and a detached child retaining its output pipes left
`Process.wait()` blocked after SIGKILL. The fixed teardown passes on Python 3.13
and 3.15. Native coverage sends multiple assistant bodies around tool calls, stops
a running Bash fixture, sends another turn, and reopens the persisted conversation.

## Live tests

These commands use the selected CLI's existing configuration and consume model quota.
Run them separately:

```sh
./scripts/test.sh --live deepseek
./scripts/test.sh --live zcode
```

Each run creates temporary workspaces, checks two-turn memory, asks the agent to
read a file, verifies the response's math, clicks its reference, and checks that
lines 123-234 are selected and the file is unchanged. A second native test answers
a choice prompt, reopens the panel, and checks restored-session memory.
No permission is auto-approved.
ZCode needs a configured provider; the test does not create one.

Verified locally on 2026-11-14: ZCode CLI 0.17.1 and DeepSeek Harness 0.2.2-rc.1,
using the configured DeepSeek Flash models. ZCode used a temporary provider outside the repository.
Additional real-provider probes exercised a model override, a native single-choice
question answered through the panel bridge, process shutdown, and restored-session
memory. DSH also passed native model/effort readback, an idle model change, and
configuration readback after a cold ACP resume. Pi, Codex, and Claude reported their
default models without a conversation prompt; Codex accepted an idle effort change.
These probes did not rewrite the original CLI configuration files.

Kimi's real ACP initialization/config surface and workspace-filtered session listing
were checked. Codex 0.167.2 protocol types, Pi 1.5.0 RPC types, Claude's control
messages, and current upstream Kimi/ZCode/DSH/Hermes implementations informed the
fixtures. Hermes was not installed. Other real-provider conversations, live write
approvals, and cancellation with other providers remain unverified. Fixtures do not establish
compatibility with every CLI version.

Verified on 2026-11-16 with DSH 0.2.2-rc.1 and DeepSeek V4 Flash: three distinct
assistant messages around two Bash calls, cancellation of a real `sleep 30` tool
in 58 ms with no surviving child, and a successful next prompt in the same session.
The probe used a temporary workspace and the existing ACP profile without changing
CLI configuration. It does not establish the cause of every long-running Bash call.

Cached-session tests hold native discovery indefinitely and verify that history is
restored first. The selected CLI then connects asynchronously to read its effective
configuration. Unselected CLIs are not launched for configuration discovery.
Metadata discovery runs outside the bridge event loop; Kimi's listing has a three
second deadline. Discovery scans at most 2,000 candidate records per file/database
source and keeps the newest 500 matching sessions. The panel caches recent history;
the CLI remains responsible for its full conversation and tool state.

Hermes' current ACP adapter accepts only ACP-origin persisted sessions. Its model
setting uses ACP's legacy model endpoint, while unsupported reasoning effort is
reported explicitly. The reviewed adapter does not install the native `clarify`
callback. No Hermes choice-prompt compatibility is claimed.

## Reference designs

Reviewed on 2026-11-14. These informed our tests; no upstream suite is being claimed as passed.

| Reference | Applied here |
|---|---|
| [Zed ACP thread tests](https://github.com/zed-industries/zed/blob/main/crates/acp_thread/src/acp_thread.rs) | Preserve omitted tool fields, distinguish tool completion from permission, exercise state transitions |
| [Avante ACP integration](https://github.com/avante-corp/avante.nvim/blob/main/README.md#acp-support) | Test connection shutdown, bounded transport, capability-dependent resume |
| [ACP Test Compatibility Kit](https://github.com/agentclientprotocol/acp-tck#what-is-covered) | Version negotiation, lifecycle/error scenarios, transport hygiene, explicit skipped coverage |
| [Kimi ACP reference](https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/reference/kimi-acp.md) | Form elicitation, configuration options, resume and session listing |
| [ZCode protocol](https://github.com/zai-org/ZCode/blob/main/docs/zcode-protocol.md) | Session server, runtime model settings, native permission/question responses |
| [DSH ACP frontend](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md) | Automation-only boundary; use the native question service in the current process |
| [Hermes ACP adapter](https://github.com/NousResearch/hermes-agent/tree/main/acp_adapter) | Verify persisted-session scope and unsupported settings rather than assuming parity |

ACP-TCK tests agents; this repository tests an editor client with controlled agent
fixtures. It is not an ACP conformance certification. Stop gives ACP's
[`session/cancel`](https://agentclientprotocol.com/protocol/v1/prompt-turn#cancellation)
two seconds to settle, then tears down the transport with bounded TERM/KILL and
pipe cleanup. A response body alone never completes a turn; the correlated prompt
response remains authoritative.
