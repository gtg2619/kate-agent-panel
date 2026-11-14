# Changelog

## Unreleased

- Defer and coalesce composer resizing outside document editing and repaint callbacks.
- Measure wrapped text independently and keep the cursor visible after manual newlines.
- Use distinct arrow/stop icons and remove raw ACP completion codes from button hints.

## 0.2.0

- Prefer DeepSeek Harness when no session is restored; keep unavailable agents visible.
- Align the bottom composer's `>` prompt and text with the console-style transcript.
- Use smaller, muted working indicators and elapsed-time summaries.
- Resume workspace-matched local sessions, including the last selected session at startup.
- Read model and reasoning settings from the CLI and apply edits to the current session.
- Support native questions, choices, permission replies, and failed-reply recovery.
- Keep Send/Stop beside the one/two-line composer; show client-observed turn timings.
- Preserve Kate's layout, dark theme, sidebar controls, and Esc behavior on GNOME/Wayland.
- Improve file links, LaTeX rendering, process cleanup, and CLI error handling.
- Fix numeric NVM discovery order and DSH helper loading from special-character paths.
- Expand offline integration coverage and the Python/Node CI matrix.

This is a source release. Build against the Qt/KDE libraries used by your Kate
installation; see [README](README.md#build) and [testing](docs/testing.md).
