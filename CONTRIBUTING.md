# Contributing

Issues and pull requests are welcome. Keep changes small and focused.

```sh
uv sync                       # Python 3.12 from uv.lock
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q              # set REDIS_TEST_URL to run the 17 real-Redis tests too
```

* CI must stay green with **no secrets**: tests use fakeredis, a real Redis service and a scripted fake provider.
* A new security control needs a test that fails when the control is removed (see the README's Security model).
* Numbers in the README must come from committed raw files in `results/`. Do not edit result files by hand.
* Never commit `.env`, keys, model weights or large data. CI runs gitleaks over the full history.
* Use conventional commit messages (`feat:`, `fix:`, `docs:`, ...).
