# lifecycle platform

shared Linux primitives with no production consumer integration

- durable cas
  - descriptor-bound byte compare-and-swap
  - atomic exchange, directory durability, rollback, authenticated recovery
  - [contract](docs/durable-cas.md)
- sealed bootstrap
  - manifest-bound freestanding launcher
  - authenticated closure copied into a detached read-only runtime root
  - [contract](docs/sealed-bootstrap.md)
- capability ids
  - package: `lifecycle-platform 0.1.0`
  - cas journal: `LPCAS`, version `2` with hmac-sha-256 authority
  - bootstrap: `sealed-bootstrap-linux-x86_64-v1`
- checks
  - `uv sync --group dev`
  - `PYTHONPATH=src pytest`
  - `ruff check src tests`
  - `basedpyright --project pyproject.toml`
