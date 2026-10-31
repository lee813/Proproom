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
