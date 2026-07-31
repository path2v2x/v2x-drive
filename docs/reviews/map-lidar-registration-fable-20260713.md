# Claude Fable review: map/LiDAR registration plan

- Date: 2026-07-13
- Model: `fable`
- Effort: high
- Tools: read-only `Read`
- Session persistence: disabled
- Reviewed file: `docs/map-lidar-registration-plan.md`
- Result: completed successfully after five earlier authentication failures

## Findings returned

1. Resolve the apparent conflict between six stable landmarks and the 10 fit
   plus 4 holdout control minimum.
2. Add independently authenticated roots of trust for the LiDAR validation and
   manual annotation artifacts; hash consistency alone is not authenticity.
3. Define pinned-key onboarding, rotation, revocation, expiry, and test-only
   injection behavior, and prove production allowlists default empty.
4. Authenticate vertical datum/geoid reconciliation instead of allowing an
   unexplained datum offset to consume the fitted Z-bias budget.
5. Define LiDAR annotation ordering/repeatability and use independent review to
   bound annotator error.
6. Derive the unchanged numerical thresholds from evidence accuracy and
   downstream placement requirements.
7. Keep qualitative optimizer/mode gates numerically defined.
8. Bind the deterministic rendering/numerical environment because byte-exact
   overlays and optimizer reproducibility depend on library/toolchain versions.
9. Retain rank/condition evidence for every leave-one-approach-out fold.
10. Prevent repeated operator exposure from eroding holdouts by recording a
    one-time evaluation/burn ledger.

The review also requested an explicit drift response and warned that a plan
review does not substitute for physical survey, measured intrinsics, genuine
authority evidence, or held-out deployment proof.

## Disposition

The control-count wording, pinned-key lifecycle, deterministic toolchain,
authenticated annotation/LiDAR/vertical hooks, annotation inter-review,
one-time holdout burn, and threshold derivation are addressed by the follow-up
change. Genuine pinned authority keys, signed current artifacts, and physical
evidence remain intentionally absent and therefore keep deployment ineligible.
