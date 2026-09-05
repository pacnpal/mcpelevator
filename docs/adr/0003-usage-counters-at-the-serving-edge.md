# Count usage at the serving edge, in hourly buckets

Per-server and per-tool usage is counted where a call is actually served — the `/s`
proxy and the `/g` dispatcher — not in the bridge. Both already hold the request
body, so naming the tool costs no extra I/O, and one rulebook (`app.usage.attribution`)
keeps "a tool call" identical across MCP, the REST mirror, and a group, where a call
is attributed to the member that owns the tool rather than to the group. A bridge is a
separate process with no control-plane database access, so counting there would mean a
second writer to SQLite for no gain in fidelity.

Counters accumulate in memory and are folded into `(server, tool, UTC hour)` buckets by
a background task every few seconds, never in the request's own transaction: the data
plane must not pay a database write per request, and usage bookkeeping must never be
able to fail a request it only observes. The trade is that a hard crash loses at most
one flush interval — acceptable for statistics, and the reason this is not an audit log.
Hourly buckets (rolled up to days for long windows) keep a busy server's row count
bounded; retention (`usage_retention_days`, default 30) prunes the rest, so a usage
window never reaches further back than the operator's retention.

The read side rolls up in SQL and ships whole listings (bounded by servers x tools),
so the dashboard filters and sorts in the browser rather than round-tripping per
keystroke, and both the per-server panel and the instance-wide view derive their
window from the same code — "the last 7 days" cannot mean two things depending on
which page you opened. Charts are LayerChart (the Svelte 5 library shadcn-svelte's own charts are built
on), so scales, axes, tick formatting, hover tooltips and resize come from the
ecosystem rather than from hand-rolled geometry — but the product's design system
still decides how they look: LayerChart reads its colours from CSS variables, which
`app.css` points at mcpelevator's zinc surfaces and single emerald accent. That
system locks one accent and declares no categorical palette, so the split-by-server
view is small multiples on a shared scale rather than a stacked multi-hue chart, and
the activity grid — a fixed 7x24 punchcard, where a charting library would add
nothing over a DOM grid — uses one sequential ramp of the same accent.

A bucket stores a server id, a tool name, an hour, a count, and the time that count
was last written — no arguments and no results, so a usage row can't become a shadow
copy of the traffic it summarizes, and the one timestamp it does keep expires with the
bucket. Non-tool traffic is counted separately rather than discarded: distinguishing
"clients connect but call nothing" from "nothing reaches this server" is the signal that
makes a tool rename or description rewrite worth trying. Traffic that never reached a
bridge (unknown slug, refused auth, nothing running) is not counted at all, matching the
rule idle bookkeeping already follows, and neither is the dashboard playground — the
panel reports what clients did, not what the operator did while testing.
