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

**Idle quiescence**:
The supervisor stopping an enabled Server's bridge after its idle window passes with no authenticated proxy traffic. The Server's observed state is `idle`: still desired, deliberately not running, and wakeable.
_Avoid_: Sleep, suspend, pause

**Wake-on-request**:
The proxy reactivating a quiesced Server when a request arrives for it, holding that request until the new activation is ready (or its startup window lapses).
_Avoid_: Cold start, lazy start
