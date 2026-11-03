# services

Shared Python utilities used across the backend connectors:

- Secrets Manager client (fetch + cache)
- HTTP client with retry, rate-limit throttling, and pagination helpers
- Structured JSON logging

Kept dependency-light and side-effect-free so connectors can import and unit-test them easily.
