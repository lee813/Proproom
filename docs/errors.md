# Error codes

Every error is `{code, message, hint, retryable, retry_after, details}`.

| code | HTTP | meaning |
|---|---|---|
| `not_implemented` | 501 | operation is declared but not built yet |
| `unknown_operation` | 404 | no such operation |
| `bad_argument` | 400 | argument missing or malformed |
| `unauthorized` | 401 | missing / wrong instance token |
| `idempotency_conflict` | 409 | same idempotency_key, different request |
| `public_url_unknown` | — | warning: share URL is relative |
| `gpu_lost` | 503 | the GPU this process started with can no longer be opened; propd exits so Docker restarts it (retryable) |
| `server_unavailable` | — | the CLI or MCP server could not reach propd, or propd is restarting (retryable) |
| `server_error` | — | propd answered with something that is not an error object |
| `unsupported_dtype` | 400 | the embedder has no build for the tier's dtype |
