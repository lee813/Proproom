# Proproom for agents

Quick reference: commands, schemas, error codes, recommended flow. Source of truth for schemas is
`prop capabilities` (generated from `proproom/api/registry.py`).

Recommended flow (handover §3): `search --sheet` → (`remote` → `fetch`) → `link --plan` → `link` → `credits`.
The agent picks from the contact sheet itself. `share` gives a view-only link to show the picks; the share page
collects no feedback (the `share_feedback` operation is still there, unused).
