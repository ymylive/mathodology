Mathodology macOS package — what gets installed:

  /usr/local/mathodology/                  payload (binary, web, worker, configs)
  /usr/local/mathodology/.env              created from .env.example on first install
  /Library/LaunchDaemons/com.mathodology.gateway.plist
  /Library/LaunchDaemons/com.mathodology.worker.plist
  Service user: _mathodology (system account, no shell)

Runtime requirements (NOT bundled):
  - PostgreSQL 14+   brew install postgresql@16 && brew services start postgresql@16
  - Redis 6+         brew install redis           && brew services start redis
  - uv (Python pkg)  curl -LsSf https://astral.sh/uv/install.sh | sh
  - pandoc           brew install pandoc          (PDF/DOCX export)
  - tectonic         brew install tectonic        (LaTeX export, optional)

After install, edit /usr/local/mathodology/.env and follow the instructions
the postinstall script prints.

Full docs: https://github.com/ymylive/mathodology/blob/main/docs/install/macos.md
