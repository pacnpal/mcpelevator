# MCP Elevator

The language for saved MCP endpoint definitions and their managed runtime lifecycle.

## Language

**Server**:
A saved MCP endpoint definition and its desired lifecycle state, not any one operating-system process.
_Avoid_: Bridge, child process

**Server activation**:
A startup episode that tries to make an enabled Server available from its current saved configuration. An activation can contain several Startup attempts.
_Avoid_: Process start, startup attempt

**Startup attempt**:
One pass through setup, bridge launch, and readiness checking during a Server activation. A failed attempt may be retried, and each retry runs the full pass again.

**Stable run**:
A Server that has remained running without interruption for the configured stability window. Reaching a Stable run restores the retry budget for later recovery.

**Local runner**:
A runner whose upstream MCP server executes in mcpelevator's host environment. The local runners are `npx`, `uvx`, and `command`; `docker` and `remote` are not local runners.

**Setup script**:
An optional multiline POSIX shell script attached to a Server. It prepares a local runner at the start of every Startup attempt and fails that attempt if an unhandled command fails; scripts must be safe to rerun. Files and other external effects persist, but shell-local state does not carry into the MCP child.
_Avoid_: Setup commands, pre-start hook

**Disabled tool**:
An upstream tool the operator has hidden from a Server (by name, in the Server's `disabled_tools`). The bridge drops it from every exposed surface — MCP `tools/list`, the REST/OpenAPI routes, and any group — and refuses it if called, so it's indistinguishable from a tool that was never registered. Part of the launch spec: changing the set restarts the bridge.
_Avoid_: Removed tool, deleted tool

**Tool override**:
The operator's replacement name and/or description for one upstream tool, keyed by the tool's upstream name in the Server's `tool_overrides`. Applied by the same bridge transform as a Disabled tool, so it reaches every exposed surface: clients discover only the operator's labels, and a renamed tool answers to its new name only. Fixes a tool whose own wording a model handles badly without rebuilding the upstream server. Part of the launch spec: changing it restarts the bridge.
_Avoid_: Alias, tool rewrite, tool transformation

**Idle quiescence**:
The supervisor stopping an enabled Server's bridge after its idle window passes with no authenticated proxy traffic. The Server's observed state is `idle`: still desired, deliberately not running, and wakeable.
_Avoid_: Sleep, suspend, pause

**Wake-on-request**:
The proxy reactivating a quiesced Server when a request arrives for it, holding that request until the new activation is ready (or its startup window lapses).
_Avoid_: Cold start, lazy start

**Usage bucket**:
The count of calls one Server's one tool received in one UTC hour — the unit every usage figure is summed from. A bucket holds a number, never a call's arguments or result. Buckets whose hour is older than the retention window are pruned, so a usage figure never reaches further back than that.
_Avoid_: Metric, event, log line

**Tool call (counted)**:
A request that named a tool and reached a running bridge, whichever surface carried it — the MCP endpoint, the REST mirror, or a group, where it counts against the member that owns the tool rather than the group. A request refused before a bridge was picked is not one, and neither is a dashboard playground invocation; a tool that answers with an error still is.
_Avoid_: Hit, invocation, transaction
