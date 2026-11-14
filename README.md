# Kate Agent Panel

Bring your agent CLIs into Kate's bottom panel, beside Terminal and Diagnostics.

## Overview

- A compact, left-aligned conversation using Kate's colors and fonts.
- A fixed bottom composer with a console-style `>` prompt, aligned message text,
  one Send/Stop button, and no footer row.
- Reasoning, tool calls, and logs collapsed under **Process**.
- Plain **Model ID** and **Reasoning effort** fields populated from the CLI session.
- Small, muted working status and a per-turn elapsed-time breakdown.
- Workspace-matched **Local Sessions**, with CLI labels and automatic resume.
- Editor selections and cursor lines staged in **Pending**, including unsaved edits.
- Clickable workspace references and local Markdown/LaTeX rendering.
- DeepSeek Harness (DSH) first in the agent list and selected by default when ready.
- Adapters for DSH, Pi, Codex, Claude Code, Kimi Code, Hermes, and ZCode.

The plugin discovers installed commands and inherits their environment. Agent
installation and authentication stay with each CLI. Model and reasoning edits
apply through the CLI to the selected session and are saved with its history.
The panel does not rewrite global CLI configuration files.

## Screenshot

![Agent Panel showing an editor snapshot, clickable file references, rendered equations, and the bottom composer](docs/screenshots/agent-panel.png)

The native panel with a local test agent. The sample shows a staged selection,
inline and display math, and collapsed process output.

## Requirements

- **Runtime:** Kate built with Qt 6 / KDE Frameworks 6, Qt WebEngine, Python 3.10+,
  and at least one configured agent CLI available on `PATH`.
- **Build:** a C++20 compiler, CMake 3.22+, Extra CMake Modules 6, Qt 6.5+
  development packages (Widgets, WebEngineWidgets, WebChannel), and KDE Frameworks 6
  development packages (CoreAddons, TextEditor, XmlGui, I18n).
- **Tests:** Qt Test, Node.js 22.15+ or 24+, `flock`, and a display server or Xvfb.

Build against the Qt and KDE libraries used by your Kate installation. Local
validation uses Linux; see [testing](docs/testing.md) for the verified environment.

## Build

On Fedora, install the build and runtime dependencies first:

```sh
sudo dnf install kate git gcc-c++ cmake make ninja-build extra-cmake-modules \
  qt6-qtbase-devel qt6-qtwebengine-devel qt6-qtwebchannel-devel \
  kf6-ktexteditor-devel kf6-kcoreaddons-devel kf6-kxmlgui-devel kf6-ki18n-devel \
  python3
```

`extra-cmake-modules` provides `ECMConfig.cmake`. The `-devel` packages provide the
headers and CMake configuration files required to build the plugin.

For tests, also install:

```sh
sudo dnf install nodejs xorg-x11-server-Xvfb xorg-x11-xauth util-linux \
  dejavu-sans-fonts google-noto-sans-cjk-fonts
```

Clone the repository and configure a release build for installation under `~/.local`:

```sh
git clone https://github.com/gtg2619/kate-agent-panel.git
cd kate-agent-panel
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_TESTING=OFF \
  -DCMAKE_INSTALL_PREFIX="$HOME/.local" \
  -DKDE_INSTALL_PLUGINDIR=lib/qt6/plugins
cmake --build build --parallel 4
```

To try the build without installing it, run `./scripts/run-dev.sh`. The launcher
opens a separate Kate instance with the plugin loaded from the build directory.

## Install

After building with the paths above, install and launch Kate:

```sh
cmake --install build
QT_PLUGIN_PATH="$HOME/.local/lib/qt6/plugins${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}" kate --new
```

Enable **Agent Panel** in **Settings > Configure Kate > Plugins**, then open the
bottom **Agent** tab. Keep `QT_PLUGIN_PATH` in your Kate launcher or shell environment
when using this installation location. Agent CLIs must be available in that environment.

To uninstall this user installation, remove
`~/.local/lib/qt6/plugins/kf6/ktexteditor/kateagentpanel.so` while Kate is closed.

## Usage

1. Select an agent and working directory. The default is Kate's active project,
   then the current local file's directory, then your home directory. **New Chat**
   recalculates this default; an existing conversation keeps its original directory.
   With no session to restore, DSH is selected if its command and ACP profile are
   available; otherwise, the first available agent is selected.
2. Add a selection or cursor line through the editor's **Agent Pending** context menu.
   Pending stores a snapshot; double-click an item to edit it.
3. Model ID and reasoning effort show the CLI's effective values, where reported.
   Edit either field and press **Tab** or **Enter** to apply it to the session.
   The CLI validates changes and returns the resulting values. Send with **Enter**; use
   **Ctrl+Enter** for a new line. The composer grows from one to two visible lines.
   You can prepare the next draft while a turn runs.
   The button to the right of the input switches between **Send** and **Stop**.
   **Stop** terminates this panel's agent process group. The toolbar's restore button
   recovers the last input after a failed send.
4. Click a file reference to open the file and select its lines. Permissions and
   agent questions require an explicit response.

During a turn, **Working...** appears below the conversation. On completion or stop,
it becomes the elapsed time and the time spent waiting, reasoning, running tools,
streaming the reply, and awaiting user input. These are client-observed wall times,
not server measurements. Concurrent tools count once, and unreported reasoning
stays in waiting time.

| Action | Shortcut |
|---|---|
| Show panel | Ctrl+Alt+A |
| Hide panel and return to the editor | Esc (focus inside the panel) |
| Stage selection | Ctrl+Alt+S |
| Stage cursor line | Ctrl+Alt+L |
| Send | Enter |
| Insert a new line | Ctrl+Enter |
| Copy workspace reference | Context menu; configurable in Kate |

Hiding the panel preserves the conversation, draft, and Pending context. It does
not stop a running agent; use **Stop** for that.

References use `project/path/to/file:L123-234`, with one-based line numbers. Single
lines, `file.cpp:12:3`, and `file:///...#L12-L30` also work. Use backticks or a Markdown
link for paths containing spaces. HTTP(S) links open in the system browser.

Markdown and KaTeX render locally. Math delimiters are `$...$`, `$$...$$`,
`\(...\)`, and `\[...\]`; code stays literal. Full TeX documents are not supported.

### Sessions and model parameters

Open **Local Sessions** from the history button. The list shows the CLI tool,
title, and update time for the current working directory, with symlinks resolved.
Select a row and **Resume**, or use **New Chat** to start fresh. Startup restores
the last selected session for that directory; on first use, it selects the newest
available local session. Choosing New Chat prevents that automatic selection until
another conversation is started or resumed.

Restoration loads the panel's recent transcript and parameters first. The agent's
selected CLI connects in the background to read its session configuration, without
sending a prompt. Native sessions are discovered read-only;
older versions or custom storage layouts may expose fewer sessions. Imported
transcripts are available for Pi, Codex, Claude, and Hermes ACP sessions; other
native imports start with an empty display while the CLI retains its context.

The panel stores its index and recent transcript (up to 200 events / 8 MiB per
session) in `$XDG_STATE_HOME/kate-agent-panel`, defaulting to
`~/.local/state/kate-agent-panel`. `KATE_AGENT_STATE_HOME` overrides that directory.
Each Kate window has its own running process; the session index is shared.

Parameter values are validated by the CLI. For example, Codex accepts model IDs
and effort names, Pi accepts `provider/model` and thinking levels, and Kimi's
thinking values depend on the selected model. ZCode accepts an existing
`provider-id/model-id`; DeepSeek Harness accepts a model ID or `provider/model`.
An unavailable model or unsupported effort produces an error rather than being
silently ignored. Switching CLI tools reloads the fields from that CLI. Unknown or
unsupported settings remain empty. Claude's model can be read through its context
usage endpoint (or its first-turn initialization on older versions); its default
reasoning effort is not reported by the current control protocol.

## Supported Agents

| Agent | Transport | Notes |
|---|---|---|
| DeepSeek Harness (DSH) | ACP v1 + native question bridge | Default when ready; existing ACP profile; questions use a process-local helper |
| Pi | RPC | Persistent session, tools, extension confirm/select/input/editor requests |
| Codex | app-server | Streaming, permissions, user questions |
| Claude Code | stream-json | Persistent session, permissions, questions |
| Kimi Code | ACP v1 | Form questions, multiple choice, permissions, resume |
| Hermes | ACP v1 | Permissions and saved ACP sessions; see compatibility limits below |
| ZCode | app-server | Persistent process, questions, permissions, resume; new sessions use `build` mode |

Questions appear as explicit choices, multiple selections, or text fields where
supported. No answer is preselected or submitted automatically. Failed replies
remain editable. Permissions are forwarded to the CLI only after a response.

DeepSeek Harness requires `profiles/acp/cordis.yml` under `DSH_HOME` or `~/.dsh`,
and Node.js 24.9+ for the tested DSH version. Its ACP frontend omits human questions, so a temporary
module loads the installed native question tool and connects it to the panel.
No installed package or profile is edited. ACP resume depends on the agent's
advertised capabilities; client filesystem and terminal capabilities remain disabled.

Hermes is covered by ACP fixtures, but has not been tested locally with a real
installation. The reviewed upstream ACP adapter resumes ACP-origin sessions and
does not expose a reasoning-effort setting or a `clarify` answer callback. Those
features are not claimed as supported. See [test coverage](docs/testing.md) for
verified versions and remaining limits.

## Configuration

For a nonstandard installation, set an argv-style command override before launching Kate:

```sh
export KATE_AGENT_PI_COMMAND='/absolute/path/to/pi'
export KATE_AGENT_ZCODE_COMMAND='node /absolute/path/to/zcode.cjs'
./scripts/run-dev.sh
```

The same override is available for `CODEX`, `CLAUDE`, `KIMI`, `HERMES`, and `DEEPSEEK`.
Commands are never evaluated by a shell. Agent selection tooltips show detected
environment variable names, never their values.

Discovery preserves `PATH` priority, then checks common user bin directories and
installed NVM versions from newest to oldest. It does not source shell startup
files or change Node versions. If an agent needs a different runtime, use an
absolute Node path in its command override, or set `PATH` in Kate's launcher.

The development and test scripts accept `KATE_AGENT_BUILD_DIR` and an optional
`KATE_AGENT_SDK` dependency bundle. A normal system build does not need this bundle.

## Testing

```sh
./scripts/test.sh --unit  # Python and Markdown/KaTeX tests; no Qt build
./scripts/test.sh         # Debug build, native editor/WebEngine tests, JUnit report
```

Default tests use local fixtures and never call a model. The full runner enables
`BUILD_TESTING` and writes `build/test-results.xml`. See the [testing guide](docs/testing.md)
for coverage, opt-in live tests, reference projects, and known gaps.

## License

[MIT](LICENSE). Bundled asset versions are recorded in
[the vendor manifest](web/vendor/manifest.json); [KaTeX](web/vendor/katex/LICENSE)
and [markdown-it](web/vendor/markdown-it/LICENSE) retain their own licenses.
