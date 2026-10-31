# Plugins

Types: source, embedder, analyzer, segmenter, policy, ranker, exporter, hook, command (handover §11).
v1 loads `<home>/plugins/*.py` and directories with `plugin.toml`. Plugins import only `proproom.sdk`,
return structured results and never get a DB connection; core performs every side effect.
