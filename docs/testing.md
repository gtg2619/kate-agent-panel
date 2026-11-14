# Testing

## Run

- `scripts/test.sh --unit`: Python subprocess/protocol tests and Node rendering tests.
- `scripts/test.sh`: configure, build, and run CTest; prefers an isolated Xvfb display.
- `build/test-results.xml`: JUnit report. Native tests save previews in `build/`.

The runner serializes native builds and clears any ambient live-test opt-in. Missing
Node or Qt Test fails test configuration instead of silently omitting a suite.
Each subprocess test has a deadline and closes its children. CI runs the offline
suite; no agent package or credential is required.

Local validation uses Fedora 45, Qt 6.11.2, and KF6 6.31. GitHub Actions also passed
the Python 3.10/3.13 unit jobs and the Fedora 43 native suite on 2026-11-14.
The current offline run passes all five CTest suites, including 86 Python tests
and 25 Qt test cases; the two real-provider Qt cases are opt-in. Other platforms
remain unverified.

## Coverage

| Layer | Checks |
|---|---|
| Discovery | Command overrides, argv quoting, credential-name-only inventory, existing Harness profile requirement |
| Transport | Fragmented UTF-8, noisy stdout, oversized records, malformed input, EOF cleanup, busy sends, process-group termination and kill escalation |
| Sessions | All seven adapters; persistent turns; canonical workspace filtering; read-only metadata imports; cache restart; New Chat; unavailable agents; stale discovery; ACP capability-dependent resume; UTF-8 history limits |
| Parameters | Native default readback, idle model/effort edits, rejected-value recovery, native changes overriding cached settings, session persistence, ACP updates and legacy model IDs, Codex empty-thread recreation, ZCode model-default reasoning |
| Interaction | Single/multiple/custom/secret answers, required fields, typed values, dismiss, failed-write retry, permissions, Codex MCP elicitation, server-resolved requests, DSH native socket/hook lifecycle |
| Native editor | Deferred startup, hidden/visible session restore, host palette/layout preservation, load/unload, resize/hide/show, icon fallbacks, shortcuts, split-view context, Pending, failure recovery, file-link clicks |
| Focus | First-click composer focus, Esc from composer/transcript/workspace/Pending, dialog dismissal, return to editor, draft/context retention, focus on reopening |
| Transcript | Left alignment, collapsed process output, visible errors, pinned one/two-line composer, inline Send/Stop, animated working status, elapsed breakdown, Enter/Ctrl+Enter, IME protection, scroll retention, theme/font updates, reload, restored history, local-session dialog |
| Workspace | Active Kate project, local file directory, HOME fallback, New Chat after a manual directory override |
| Timing | Monotonic wall time, overlapping tool union, user-input waits, zero-duration turns, stop/error outcomes, one persisted summary per turn |
| Files | Workspace/absolute/file URLs, Unicode/space/percent paths, line/column/range selection, generated round trips, invalid locations and schemes |
| Rendering | Four math delimiters, matrices, aligned equations, lists/quotes, streaming, errors, code isolation, nested links, KaTeX fonts, escaped HTML and blocked network access |
| Packaging | Local HTML/font dependencies, licenses, English first-party text |

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

Unicode cases use escaped data; project UI, documentation, and test descriptions are English.

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
approvals, and live cancellation remain unverified. Fixtures do not establish
compatibility with every CLI version.

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
fixtures. It is not an ACP conformance certification. Our Stop action terminates the
process group; it does not implement graceful `session/cancel` negotiation.
