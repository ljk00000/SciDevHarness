# Conversation summary and development tree

## Automatic summary

During a coding conversation, the Agent can send a second, small LLM request
every N Agent rounds. The request contains a compact transcript, the current
Git SHA, and the configured summary instruction. A final summary is always
generated when the conversation completes. Summaries are shown in the chat
panel and stored as separate records:

```text
.research/summaries/<session>_<summary_id>.json
```

Summary generation is auxiliary: a timeout or provider failure is recorded as
`conversation_summary_failed` and never changes a completed coding task into a
failed task. Each completed conversation gets its own file; older summaries
are not overwritten.

The Agent settings button in the chat header opens an inline settings panel.
It controls:

- enabled / disabled;
- optional summary model;
- maximum output tokens;
- transcript context size;
- summary retry count;
- checkpoint frequency in Agent rounds (`0` means final summary only);
- custom summary instruction.

Settings are stored in `.research/settings.json`, which is project-local and
ignored by Git. Environment overrides are also available:

```powershell
$env:SCIDEV_SUMMARY_ENABLED="false"
$env:SCIDEV_SUMMARY_MODEL="your-summary-model"
$env:SCIDEV_SUMMARY_INTERVAL_TURNS="4"
```

## 2.5D development tree

The Git entry opens a main workbench page. The vertical trunk represents
completed coding work; side nodes represent cancelled, failed, running, or
waiting attempts. These side nodes are development directions, not Git
branches.

- drag a side/attempt card with the left mouse button to move that branch; its connector follows the card;
- drag an empty area with the left mouse button to pan the canvas;
- use the mouse wheel to zoom around the cursor;
- click a card to inspect its details;
- failed and waiting nodes can be retried from the detail panel.

Cards use layered shadows, a highlight edge, depth offsets, and accent colors
for main, failed, retrying, and active states. Branch card positions are saved
in `.research/tree_layout.json` and restored when the project opens.

## Editor workflow

The project explorer follows the common VS Code interaction model:

- single click opens a temporary preview tab;
- double click promotes the file to a persistent editor tab;
- multiple files can stay open and tabs can be moved or closed;
- modified tabs show a dot and cannot be closed accidentally before saving;
- `Ctrl+S` writes the active file and records a `file_changed` event;
- the `+` action creates a project-relative file without a modal dialog;
- the `@` action adds the active file and selected code to the next Agent prompt;
- after an Agent task completes, clean open editors reload from disk.

## IDE core additions

The center panel is now an editor workbench rather than a task-only form:

- `Ctrl+P` or the top search opens the best matching project file; `>` provides
  a small command mode for the terminal, problems panel, and replace view;
- `Ctrl+F` searches the active file, while `Ctrl+H` adds replace, replace-all,
  case-sensitive, and whole-word controls;
- `Ctrl+`` toggles an integrated terminal rooted at the project directory;
- the Problems panel keeps failed task and terminal output visible without a
  modal popup;
- the status bar reports cursor position, encoding, language, and Git state;
- `差异` opens the current file's tracked or untracked Git diff in a read-only
  editor tab; the Git page also exposes a change-file list, double-click diff,
  selected/all staging, un-staging, workspace status, and manual commit.
- `Ctrl+Shift+F` searches all readable project files and double-clicking a
  result opens the file at its line; `Ctrl+Shift+O` opens a Python-aware
  outline for classes and functions.
- `F12` jumps to a Python definition, `Shift+F12` finds whole-word symbol
  references, and `Alt+Left`/`Alt+Right` navigate through visited locations.
- `Shift+Alt+F` cleans trailing whitespace and normalizes the final newline;
  the editor context menu exposes navigation, rename, and formatting actions.
- The editor toolbar can open the active file in a second side-by-side editor
  group. Each group keeps its own tabs while sharing save, diff, diagnostics,
  and dirty-file protections.
- `Ctrl+Space` provides lightweight local word/keyword completion. It is
  intentionally local and deterministic; semantic IntelliSense still needs a
  language-server integration.
- The Problems panel runs a lightweight Python syntax check for the active
  editor and supports double-click navigation to the reported line and
  column; task and terminal errors remain visible below it.
- The explorer context menu creates files/folders, renames or deletes
  project-relative resources, and opens a terminal at a selected folder.
- The command palette shortcut is `Ctrl+Shift+P`; its current command mode
  routes to terminal, problems, search, and replace without opening a dialog.

This deliberately follows the VS Code workbench model: explorer on the left,
editor tabs in the center, Agent conversation on the right, and terminal/
problems at the bottom.
