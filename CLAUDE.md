## Development practices

### TDD is required

Write a failing test before the implementation for any new behavior or bug fix, then make it pass. No non-trivial logic (branch, loop, parser, money/security path) lands without a test proving it.

## Agent skills

### Issue tracker

Issues live as GitHub issues on `maetad/personal-assistance-agent`, managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root (created lazily by `/domain-modeling`). See `docs/agents/domain.md`.
