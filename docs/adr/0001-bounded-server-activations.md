# Model server startup as bounded activations

A Server activation is a recovery episode made of complete Startup attempts: optional local setup, bridge launch, and readiness checking. Setup runs as a separate POSIX shell process on every attempt, and startup or unhealthy failures retry with bounded exponential backoff; the budget resets only after a configured stability window. This avoids session-scoped duplicate setup and unbounded crash loops while keeping setup in the same initial environment and working directory as the MCP child.
