# web (Mathodology M1 shell)

```bash
pnpm install                      # from repo root
cp .env.example .env              # at repo root — Vite reads VITE_* vars
pnpm --filter web dev             # http://localhost:5173
pnpm --filter web typecheck
pnpm --filter web build
```
