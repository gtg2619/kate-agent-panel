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
Other platforms remain unverified.

## Coverage

| Layer | Checks |
|---|---|
| Discovery | Command overrides, argv quoting, credential-name-only inventory, existing Harness profile requirement |
| Transport | Fragmented UTF-8, noisy stdout, oversized records, malformed input, EOF cleanup, busy sends, process-group termination and kill escalation |
| Sessions | All seven adapter entry points; multiple turns; agent/workspace changes; ACP version negotiation, errors, reload and fallback; ZCode exit-before-ready |
| Interaction | Permission allow/deny, cancelled and stale replies, unknown client methods, partial tool updates, question conversion |
| Native editor | Real plugin load/unload, shortcuts, clicked split-view context, unsaved snapshots, Pending, failure recovery, actual file-link clicks |
| Transcript | Left alignment, collapsed process output, pinned composer, scroll retention, theme/font updates, reload, bounded entries, permission forms |
| Files | Workspace/absolute/file URLs, Unicode/space/percent paths, line/column/range selection, generated round trips, invalid locations and schemes |
| Rendering | Four math delimiters, matrices, aligned equations, lists/quotes, streaming, errors, code isolation, nested links, KaTeX fonts, escaped HTML and blocked network access |
| Packaging | Local HTML/font dependencies, licenses, English first-party text |

Qt integration tests use real KTextEditor documents/views and WebEngine with a
small MainWindow test host. They do not replace a manual check in a full Kate window.
Unicode cases use escaped data; project UI, documentation, and test descriptions are English.

## Live tests

These commands use the selected CLI's existing configuration and consume model quota.
Run them separately:

```sh
./scripts/test.sh --live deepseek
./scripts/test.sh --live zcode
```

Each test creates a temporary workspace, checks two-turn memory, asks the agent to
read a file, verifies the response's math, clicks its reference, and checks that
lines 123-234 are selected and the file is unchanged. No permission is auto-approved.
ZCode needs a configured provider; the test does not create one.

Verified locally on 2026-11-14: ZCode CLI 0.17.1 and DeepSeek Harness 0.2.2-rc.1,
using `deepseek-flash`. ZCode used a temporary provider outside the repository.
Other real agents, write approvals, and cancellation against live providers remain
unverified. Protocol fixtures do not establish compatibility with every CLI version.

## Reference designs

Reviewed on 2026-11-14. These informed our tests; no upstream suite is being claimed as passed.

| Reference | Applied here |
|---|---|
| [Zed ACP thread tests](https://github.com/zed-industries/zed/blob/main/crates/acp_thread/src/acp_thread.rs) | Preserve omitted tool fields, distinguish tool completion from permission, exercise state transitions |
| [Avante ACP integration](https://github.com/avante-corp/avante.nvim/blob/main/README.md#acp-support) | Test connection shutdown, bounded transport, capability-dependent resume |
| [ACP Test Compatibility Kit](https://github.com/agentclientprotocol/acp-tck#what-is-covered) | Version negotiation, lifecycle/error scenarios, transport hygiene, explicit skipped coverage |

ACP-TCK tests agents; this repository tests an editor client with controlled agent
fixtures. It is not an ACP conformance certification. Our Stop action terminates the
process group; it does not implement graceful `session/cancel` negotiation.
