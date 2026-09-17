# AGENTS.md

## AI Debug Protocol Boundary

When this file is loaded into an AI Debug model-driver context, the harness protocol takes precedence over direct tool instructions in this document. Treat instructions such as read, search, patch, command, ask, plan, and test as intent to express through the role-allowed `ModelAction`; do not invoke a CLI, browser, filesystem, shell, MCP, or other built-in tool directly. The harness validates and dispatches accepted actions through the registered run environment.

The direct commands in the [browser automation guide](.github/docs/browser-automation-guide.md) apply only to an interactive repository-maintenance agent outside the AI Debug model driver. Inside AI Debug, request an administrator-registered browser command by `command_id` when the role permits it; otherwise return `needs_input` or `final`.

## Working agreement

- Read relevant files before editing. For multi-step work, state a brief plan and verifiable completion criteria.
- Ask when missing information materially changes the outcome, scope, or external impact. For reversible choices supported by context, state the assumption and proceed; respect authorization already given.
- Keep analysis and planning requests within that scope. Carry implementation requests through the necessary verification.
- Preserve existing user changes, reuse project patterns, and make the smallest change that satisfies the request. Avoid unrelated cleanup, dependencies, and abstractions; remove only unused code introduced by your changes.
- Do not commit, push, or change remote state unless explicitly requested.

## Verification

- Choose reproducible checks for the changed behavior and its regression risk. Reuse existing tests when sufficient; add tests for uncovered behavior rather than duplicating coverage.
- Run applicable lint, tests, and required project gates. For documentation-only changes, check Markdown, local links, and the diff; validate changed configuration with its parser or available validator.
- Once required checks pass, repeat them only after relevant changes, failures, or unresolved concerns.
- Report the result, checks actually run, and remaining limits. Never describe a skipped or unconfigured check as passing.

## Repository specifics

- This repository contains the AI Debug product documents and the `devauto` Python prototype. `make init` installs the development environment; `make check` runs `.venv/bin/python -m pytest -q`. No standalone lint command is configured; validate changed configuration and scripts directly.
- `.husky/pre-commit` calls `make check`. Preserve that entry point when connecting real checks for a derived project.
- `CLAUDE.md` links to this file. `.agents/skills` and `.claude/skills` both link to `.skills`; preserve the shared source and links. For skill changes, see [.skills/README.md](.skills/README.md).
- For browser automation or changes requiring UI validation, read the [browser automation guide](.github/docs/browser-automation-guide.md).
