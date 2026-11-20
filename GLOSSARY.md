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

**Server restart**:
An operator-requested Server activation for a Server that is already desired: the bridge is stopped and re-activated from unchanged saved configuration, so readiness re-runs discovery and the Server picks up its upstream's current tools. Unlike a retry it carries no state precondition (a running, idle, starting, or failed Server can all be restarted), and unlike an edit it changes nothing about the launch spec. Restarting a group is exactly restarting each of its enabled members — a group runs no process of its own.
_Avoid_: Reboot, reload, refresh

**Stable run**:
A Server that has remained running without interruption for the configured stability window. Reaching a Stable run restores the retry budget for later recovery.

**Local runner**:
A runner that executes operator-supplied code in mcpelevator's host environment or on its Docker daemon. The local runners are `npx`, `uvx`, `command`, and `docker`; `remote`, `openapi`, and `graphql` are not local runners.

**OpenAPI server**:
A Server with no MCP upstream: its tools are the operations of a REST API, translated by the bridge from the API's OpenAPI document each time the Server is activated. Calls are plain HTTP requests carrying only the operator's configured headers. A restart re-reads the document, the way it re-discovers an MCP upstream's tools.
_Avoid_: Generated server, API wrapper

**Setup script**:
An optional multiline POSIX shell script attached to a Server. It prepares a local runner at the start of every Startup attempt and fails that attempt if an unhandled command fails; scripts must be safe to rerun. Files and other external effects persist, but shell-local state does not carry into the MCP child.
_Avoid_: Setup commands, pre-start hook

**Source repository**:
A git repository a local-runner Server launches from (`repo_url`, `repo_ref`). mcpelevator owns the checkout under its data directory, fetches the ref on every Server activation, and runs the command inside it. Part of the launch spec.
_Avoid_: Git runner, source package

**Post-clone script**:
A Server's install/build steps for its Source repository, run in the checkout during a Startup attempt's checkout phase only when the checked-out commit or the script changed since its last successful run. Unlike a Setup script it does not rerun on every attempt.
_Avoid_: Build hook, install script

**Disabled tool**:
An upstream tool the operator has hidden from a Server (by name, in the Server's `disabled_tools`). The bridge drops it from every exposed surface — MCP `tools/list`, the REST/OpenAPI routes, and any group — and refuses it if called, so it's indistinguishable from a tool that was never registered. Part of the launch spec: changing the set restarts the bridge.
_Avoid_: Removed tool, deleted tool

**Tool override**:
The operator's replacement name and/or description for one upstream tool, keyed by the tool's upstream name in the Server's `tool_overrides`. Applied by the same bridge transform as a Disabled tool, so it reaches every exposed surface: clients discover only the operator's labels, and a renamed tool answers to its new name only. Fixes a tool whose own wording a model handles badly without rebuilding the upstream server. Part of the launch spec: changing it restarts the bridge.
_Avoid_: Tool rewrite, tool transformation

**Tool alias**:
An extra name, set in a Tool override, that also calls the tool but is never listed to clients. It answers only when no real tool has that name, so an old name keeps working after a rename.
_Avoid_: Fallback name, secondary name

**Idle quiescence**:
The supervisor stopping an enabled Server's bridge after its idle window passes with no authenticated proxy traffic. The Server's observed state is `idle`: still desired, deliberately not running, and wakeable.
_Avoid_: Sleep, suspend, pause

**Wake-on-request**:
The proxy reactivating a quiesced Server when a request arrives for it, holding that request until the new activation is ready (or its startup window lapses).
_Avoid_: Cold start, lazy start

**Usage bucket**:
The count of calls one Server's one tool received in one UTC hour — the unit every usage figure is summed from. A bucket holds a number, never a call's arguments or result. Buckets whose hour is older than the retention window are pruned, so a usage figure never reaches further back than that.
_Avoid_: Metric, event, log line

**Usage window**:
The trailing span a usage view reports on, ending at the current, still-filling bucket. It never reaches further back than retention has kept buckets, and its width decides the bucket size: hours for a day or two, whole days beyond that.
_Avoid_: Range, period, timeframe

**Tool call (counted)**:
A request that named a tool and reached a running bridge, whichever surface carried it — the MCP endpoint, the REST mirror, or a group, where it counts against the member that owns the tool rather than the group. A request refused before a bridge was picked is not one, and neither is a dashboard playground invocation; a tool that answers with an error still is.
_Avoid_: Hit, invocation, transaction

**User**:
A named identity on the control plane. A user holds no password: they prove who they are with a Login token or a Sign-in session, and their role decides what they reach.
_Avoid_: Account, login

**Admin**:
A user role that sees and manages every Server, token, user, group, and setting.
_Avoid_: Superuser, owner

**Member**:
A user role that sees and manages only the Servers and tokens it owns. Running local runners is a separate permission an admin grants.
_Avoid_: Regular user, guest

**Login token**:
A control-scoped token that is a user's credential for the control plane, pasted at sign-in or sent by an agent. Tokens scoped to a Server or group reach the data plane only and are never a login.
_Avoid_: Password, API key, admin token

**Sign-in session**:
A person's authenticated browser presence on the control plane, begun by signing in through the configured identity provider and ended by logout, expiry, or deactivation of their user.
_Avoid_: Session (that means an MCP session), cookie, login

**Linked identity**:
The one identity-provider account a user is bound to, recognized by its issuer and subject and never by email, so a changed or recycled address can't move the user to someone else.
_Avoid_: SSO account, external user

**Deactivated user**:
A user who can no longer sign in or hold any working token, but whose record and owned Servers remain. Owned Servers keep running under admin control. Only provisioning or an admin brings the user back, never a successful sign-in at the identity provider.
_Avoid_: Disabled user, suspended user, deleted user
