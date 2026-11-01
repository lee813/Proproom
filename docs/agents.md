# Proproom for agents

Quick reference: commands, schemas, error codes, recommended flow. Source of truth for schemas is
`prop capabilities` (generated from `proproom/api/registry.py`).

Recommended flow (handover §3): `search --sheet` → (`remote` → `fetch`) → `link --plan` → `link` → `credits`.
The agent picks from the contact sheet itself. Only when a person wants to look first: `share`, then
`share_feedback` reads back their picks before `link`.
