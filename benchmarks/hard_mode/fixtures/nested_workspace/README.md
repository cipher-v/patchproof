# Nested workspace fixture

The deterministic fixture models a monorepo registry with overlapping workspace roots. The BASE
snapshot chooses the shallowest matching workspace; the HEAD snapshot chooses the deepest matching
workspace. The hard-mode bootstrap creates immutable Git commits from these checked-in bytes using
fixed author metadata and timestamps.
