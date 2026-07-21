# Requirements source inventory

The initial framework was verified against private, immutable requirement archives on
the development VPS. Those source archives and the generated machine/host evidence
are intentionally not included in the public repository: they are not runtime
dependencies and can contain environment-specific metadata.

The retained public artifacts are the implemented schemas, contracts, architecture
documents, runbooks, tests, and ADRs in this repository. Runtime evidence should be
regenerated on each deployment under the ignored `evidence/` directory.

## Authority order

1. The operator's explicit requirements and local deployment configuration.
2. The checked-in contracts, configuration schemas, architecture decisions, and
   runbooks.
3. Current official Binance and OpenAI documentation.
4. Methodology-only reference material; it cannot override execution, security,
   deployment, or safety controls.

No private archive is required to install or operate the copy-trading service. The
deployment path is documented in
[`deployment/copy-trading-vps.md`](deployment/copy-trading-vps.md).
