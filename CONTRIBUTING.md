# Contributing

Thanks for helping improve Mathodology. Keep it simple:

## Branching

- Work on feature branches off `main`: `feat/<short-name>`, `fix/<short-name>`.
- Open a PR against `main`. We squash-merge, so PR titles become commit titles.
- No force-pushes to `main`.

## Commit style

Conventional-ish, matching existing history:

- `feat: ...` — new functionality.
- `feat(scope): ...` — scoped (e.g. `feat(web): ...`, `feat(worker): ...`, `feat(llm): ...`).
- `fix: ...` — bug fixes.
- `chore: ...`, `docs: ...`, `refactor: ...` — as appropriate.

One logical change per commit.

## Before you push

```bash
just lint    # cargo clippy + ruff + vue eslint
just test    # cargo test + pytest + vitest
```

CI (`.github/workflows/ci.yml`) runs the equivalent in GitHub Actions on every PR.
CI must be green before a PR is merged.

## Code of conduct

Be kind. Assume good intent. Disagree on ideas, not on people.
