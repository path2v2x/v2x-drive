# Claude Fable hardening review: map/LiDAR registration

- Date: 2026-07-13
- Model: `fable`
- Effort: high
- Tools: read-only `Read`
- Session persistence: disabled
- Reviewed plan, registration tool, and focused tests

## Scoped implementation result

The implementation-specific review found no acceptance-level bypass in the
new external holdout registry, retained vertical-source, or strict PDF
contracts. It confirmed the registry hash/signature chain, safe descriptor
read and identity checks, and structural PDF gates.

The review identified three concrete hardening gaps, all resolved before
commit:

1. Survey deliverables are now size-bounded before reading; an oversize sparse
   fixture proves the read is not attempted.
2. The strict PDF warning/log rejection branch now has direct regression
   coverage.
3. Cross-registry key namespaces, producer independence, and mismatched signed
   heads now have explicit adversarial coverage.

Trailing archive bytes remain covered by the explicit polyglot regression;
structurally valid PDF object streams are handled by the pinned strict parser.

## Broader plan findings

The plan-only review also raised pre-existing topics outside this three-fix
change: burn ordering versus crash resumability, vertical error-budget
decomposition, projected grid-to-ground effects, extent policy for Hausdorff,
trusted-time anchoring, and PROJ grid-resource pinning. This change does not
weaken gates or claim those physical/external prerequisites are complete.
Production signer and endpoint allowlists remain empty, deployment remains
closed, and those wider topics must be resolved before a genuine one-use
acceptance invocation.

The requested contract intentionally consumes the external authorization
before any sealed holdout metric and rejects resume/replay after consumption;
a failure after consumption requires a newly collected and authorized holdout,
as the plan states.

## Final rereview

After the three hardening gaps were resolved, a final Fable high-effort review
returned **PASS** with no remaining concrete acceptance-level bypass in the
scoped registry, vertical-source, or PDF contracts. It independently confirmed
the default-empty production roots, signed chain and inclusion checks, safe
descriptor/path identity checks, strict PDF structure and warning rejection,
and the corresponding adversarial coverage.
