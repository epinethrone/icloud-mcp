# Contributing

Thanks for helping. This server holds someone's mail, calendar and contacts, so a few rules are stricter than usual. CI
enforces most of them; this page says what they are before a failing check does.

## Before you open a pull request

- `pip install -e ".[test]"`, then `pytest tests --ignore=tests/integration -q` (about a minute).
- The integration tests run against a local Dovecot, SMTP sink and Radicale: `sudo bash dev/start_local_stack.sh`, then
  `pytest tests/integration -q`. CI runs them too, and a failure fails the build.
- `python dev/privacy_check.py origin/main` runs the same privacy check CI runs.
- `main` is protected: every change arrives through a pull request, and the tests and the privacy check must pass.

## Privacy: nothing personal in the repository

The privacy check refuses private network addresses (192.168.x, 10.x, 172.16-31.x), home-folder paths (`/Users/<name>`,
`/home/<name>`), common secret formats, precise map coordinates (5 or more decimals), and commits whose author is not a
GitHub noreply address.

- Use made-up values from `example.org`, `example.com` or `example.net` in tests, docs and descriptions. Never a real
  address, name, phone number or place, including your own.
- A line that deliberately holds a generic look-alike (an obviously fake LAN address in a test) may carry `privacy-ok`.
- Commit with your GitHub noreply address (`<id>+<user>@users.noreply.github.com`), set per repository with
  `git config user.email`. A rebase records the committer from your config too.
- No AI co-author trailers in commits.

## Security rules for code

- Text from mail, events, contacts, notes and files is untrusted data: never let it choose a tool, an address or a command.
- Nothing is sent, deleted or changed without the agent being asked to in the conversation; destructive tools default to
  the safe choice (Trash, drafts, approval).
- Writes are never retried after a network error (`callctx.retry_once_if_safe`): a retry could send or create twice.
- Errors go through `scrub_error` / `redact_error` so passwords, tokens and account ids never reach the model.
- See `SECURITY.md` for the full model and how to report a vulnerability privately.

## The Mac helper and the server must agree

The helper on the Mac runs only operations from a fixed table. A new operation goes into BOTH `OPS` in
`src/icloud_mcp/bridge.py` and `OPS` in `mac-helper/icloud_mac_helper.py` (a test fails when they differ), and the helper's
`VERSION` goes up. Users update the helper first, then the server. Helper code runs on Apple's own Python 3.9: no newer syntax.

## Tools

- Names follow `area_verb_noun` (`mail_list_senders`, `calendar_create_event`), checked against a fixed verb list in
  `tests/test_tool_surface.py`. Renaming a tool is breaking: add the old name to `RENAMED` in `server.py`.
- Keep descriptions short and keep every safety sentence; the total parameter schema has a character budget, enforced by
  the same test file (`python dev/tool_surface.py` shows the numbers).
- Instructions are built from the tools actually registered (`instructions.py`): never name a tool in text that might
  not exist.
- Performance changes are measured with `dev/bench.py` and written up in `docs/PERFORMANCE.md`.
