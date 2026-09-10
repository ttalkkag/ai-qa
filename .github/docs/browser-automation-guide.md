# Browser automation

Use this guide for browser automation or changes that require UI validation.

Use the user's specified tool when provided. Otherwise, prefer `agent-browser` when installed; if unavailable, use an available browser tool that can observe the page and verify interactions. A documentation-only change does not require a browser run.

## agent-browser workflow

Run `agent-browser --help` to check available commands.

1. `agent-browser open <url>` — navigate to the page.
2. `agent-browser snapshot -i` — inspect interactive elements and their refs (`@e1`, `@e2`).
3. `agent-browser click @e1` or `agent-browser fill @e2 "text"` — interact using the observed refs.
4. Re-snapshot after page changes and check the expected result before continuing.

With any other browser tool, inspect the current page before interacting and verify the resulting state afterward. If no browser tool is available, report the UI validation limit.
