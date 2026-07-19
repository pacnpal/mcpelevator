# Quiesce inactive Servers and wake them on request

An enabled Server whose idle window (its own `idle_timeout_s`, else the global
setting; 0 = never) passes with no authenticated proxy traffic is quiesced by the
reconciler: the bridge stops, the observed state becomes `idle`, and the cached
tool list is kept for the UI. Quiescence is desired-state-aware — the Server stays
enabled, and the reconciler deliberately does not restart it — so "enabled" now
means "available on demand", not "process resident". The next `/s` request wakes
it: the proxy requests an activation and holds the request until readiness (bounded
by the startup timeout), reusing ADR-0001's bounded-activation machinery for the
wake path. Group (`/g`) traffic marks members active but does not wake them, since
a bundle mounts only running members and remounts on the reconcile after a wake.
Operator actions (stop, disable, delete, edit, retry) always override quiescence.
