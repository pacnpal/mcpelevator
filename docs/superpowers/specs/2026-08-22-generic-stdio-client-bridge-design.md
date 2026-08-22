# Generic stdio client bridge documentation

## Goal

Document how an MCP client that only launches local stdio servers can connect to a remote mcpelevator server or group on Windows, macOS, or Linux.

## Approach

Reuse `mcp-remote`, which already translates local stdio to remote Streamable HTTP and is already emitted by mcpelevator's Claude Desktop copy option. Add one client-neutral section to the main README near "Adding a server" so users encounter it immediately after the remote URL.

The section will include:

- Node.js/npm as the only local prerequisite; `npx` downloads and launches `mcp-remote` on every supported desktop OS.
- A generic `mcpServers` JSON entry for an HTTPS, unauthenticated endpoint.
- The `--allow-http` argument for trusted loopback or private-network HTTP endpoints.
- The existing Windows-safe bearer configuration: `Authorization:${AUTH_HEADER}` in `args` and `Bearer <YOUR_TOKEN>` in `env`.
- A note that the same configuration works for `/s/<slug>/mcp` and `/g/<name>/mcp`, and that clients with native Streamable HTTP support should use the URL directly.
- A short troubleshooting check using `mcp-remote-client` to connect and list capabilities.

No bridge code, packages, binaries, backend behavior, UI options, or client-specific configuration paths will be added.

## Security and errors

Plain HTTP will be described only for loopback or trusted private networks. Bearer tokens will remain in the client process environment rather than an argument containing the secret, avoiding command-line quoting problems and keeping the documented command aligned with the existing copy-menu output. Connection failures should be investigated with the upstream client's diagnostic command; mcpelevator continues to enforce its existing Host/Origin and bearer checks.

## Verification

Validate that the documented JSON examples parse after replacing placeholders, that their arguments match the tested `installOptions()` output, and that all README links and commands are syntactically valid. Because this change is documentation-only, no production or dependency changes are required.
