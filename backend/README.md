# backend

Python AWS Lambda handlers and **read-only** source connectors.

- `connectors/` — one package per source system (`clio`, `estateably`, `graph`, `quickbooks`).
  Connectors implement **GETs only** — no write methods exist in the code.
- Shared utilities live in the top-level `services/` package.
