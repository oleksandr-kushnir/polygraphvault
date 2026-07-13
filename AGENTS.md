# Repository Guidelines

## Project Structure & Module Organization

The custom application lives in `syncer/`. Its FastAPI package is under `syncer/app/`: `main.py` defines HTTP routes, `sync.py` owns reconciliation, `db.py` contains raw psycopg SQL, and `webdav.py`/`polygraph.py` wrap external services. Tests mirror these concerns in `syncer/tests/test_*.py`. Deployment files (`docker-compose*.yml`, `Caddyfile`, and `docker/`) assemble Nextcloud, PolyGraphRAG, PostgreSQL, Redis, and the syncer. Operational documentation belongs in `docs/`; reusable PowerShell utilities belong in `scripts/`; images belong in `docs/images/`.

Nextcloud is the source of truth and PolyGraphRAG is a rebuildable projection. Preserve the ownership, deletion-grace, and bulk-delete safeguards described in `CLAUDE.md` and `docs/concept.md` when changing reconciliation code.

## Build, Test, and Development Commands

Run Python commands from `syncer/`:

```powershell
python -m pip install -r requirements-dev.txt  # install runtime and dev tools
ruff check .                                  # lint and import-order checks
mypy app                                      # type-check production code
python -m pytest -q                           # run the complete test suite
python -m pytest tests/test_sync.py -q         # run one module
```

From the repository root, use `docker compose build polygraphvault-sync` to build the custom image and `docker compose up -d` to start the local stack. Check it with `docker compose ps`; local endpoints and ports are documented in `README.md`.

## Coding Style & Naming Conventions

Target Python 3.11, use four-space indentation, type annotations, and a 110-character line limit. Ruff enforces `E`, `F`, `I`, `UP`, and `B` rules. Use `snake_case` for functions, variables, modules, and test names; use `PascalCase` for classes. Keep SQL explicit in `db.py` rather than introducing an ORM. Prefer small service adapters and dependency injection so tests remain network-free.

## Testing Guidelines

Pytest discovers `syncer/tests/test_*.py` and `test_*` functions. Add focused regression tests for behavior changes, especially deletion, ownership, authentication, retries, and failure recovery. Tests use in-memory fakes; unit tests must not require live Nextcloud or PostgreSQL. CI requires Ruff, mypy, and the full pytest suite to pass. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Follow the repository's concise, imperative subject style with a scope prefix, such as `feat:`, `fix:`, `docs:`, `ops:`, or `chore:`. Keep each commit focused. Pull requests should explain the behavior and risk, link relevant issues, list validation commands, and call out configuration or migration effects. Include screenshots for UI/documentation changes and never commit `.env`, credentials, tokens, or generated caches.
