# Map/LiDAR development registration plan

## Scope and safety

Implement an immutable-evidence registration tool. It reads evidence locally
and makes exactly one allowlisted HTTPS compare-and-append transaction to the
independent holdout registry before exposing sealed holdout metrics. It reads a
hash-bound raw LAS/LAZ, its independent validation and authoritative metadata,
the exact OpenDRIVE document, a map-geometry export, and manual feature
annotations. It never connects to CARLA, Unreal, AWS, or a running service and
never writes deployment configuration. Unreal Engine 6 and the untouched
future holdout vault are out of scope.

## Evidence contract

1. Decode the entire LAS/LAZ and verify its byte hash, point count, bounds,
   quantization scales, projected CRS, and acquisition metadata against an
   independent validation artifact. Require projected horizontal and explicit
   vertical CRS axes in metres, and reject non-metre OpenDRIVE georeferences.
   Acceptance additionally requires each validation/raw-cloud pair to be bound
   by a current detached Ed25519 attestation from a separately pinned LiDAR
   authority. Missing attestations keep acceptance closed; the raw-cloud loader
   may still be used for provenance diagnostics, but the CLI cannot evaluate
   the sealed holdout without the complete authorization chain.
2. Require a manual annotation artifact whose hashes bind the raw cloud,
   validation, metadata, OpenDRIVE, and geometry export. Every feature has a
   globally unique identity, approach identity, immutable `fit` or `holdout`
   split, stable map-polyline reference, and manually selected raw-cloud point
   indices whose recorded XYZ values reproduce the decoded points. Require two
   current reviewers from distinct organizations to independently reproduce the
   complete fixed feature denominator; recompute every corresponding map and
   LiDAR vertex deviation and require a maximum no greater than 0.10 m. A
   separately pinned annotation authority signs the exact annotation, review,
   reviewer identities, holdout set, and one-use ledger.
3. Recompute the geometry export's full content provenance: exporter source,
   pair/camera manifests, all retained real/twin frame bytes and dimensions,
   camera-object hashes, stable feature identities, and a separate retained raw
   CARLA map-source export. Rebuild every lane boundary, crosswalk, and
   environment object from that raw export, rebind exact OpenDRIVE road-mark
   ranges, recompute camera projections, rerender the overlays, and require the
   geometry JSON and overlay bytes to match the retained evidence exactly.
4. Reject split leakage at the feature, source-feature, raw-point,
   physical-control, or geometric/resampled-polyline level, plus missing
   approaches, non-finite/zero-length polylines, coarse coordinate resolution,
   CRS disagreement, stale OpenDRIVE/geometry mismatch, or insufficient
   geometric rank before optimization. Exact endpoint/coordinate reuse is a
   hard failure, and fit/holdout geometry must remain more than 0.50 m apart in
   both map and LiDAR/survey coordinates using exact two-dimensional distance
   over every original finite segment and endpoint. Intersections and
   degenerate component segments are handled explicitly; distinct self-declared
   IDs cannot bypass this gate. Each annotated map/LiDAR feature requires at
   least three independently retained vertices/raw points; a two-point claim is
   too weak to establish a reviewed polyline.
5. Bind the current survey to three actual retained files: raw observation CSV,
   surveyor-license PDF, and instrument-calibration PDF. Require exact byte
   hashes, sizes, roles, types, licensed-surveyor identity, instrument identity,
   provider/project/source identity, and observation-column schema. PDF magic
   and caller strings are never authority: also require a detached Ed25519
   signature over a separate attestation from a source-pinned, allowlisted,
   independent producer. It binds the exact manifest and deliverables, all
   identities and statuses, verification/expiry times, and pinned-key hash.
   Parse both PDFs with pinned `pypdf` in strict mode; require a bounded complete
   document, parsed cross-reference/trailer, root Catalog, and at least one Page,
   and reject encryption, trailing polyglot bytes, parser warnings, or errors.
   Tiny or malformed PDFs fail before attestation. The checked-in production
   signer allowlist is intentionally empty until genuine authority keys and
   evidence are reviewed; no authority evidence is invented. Read coordinates
   only from the CSV and resolve each control independently from rebuilt
   geometry. The 10 fit plus 4 holdout minimum therefore requires 14 distinct
   eligible stable native CARLA traffic-light or traffic-sign objects; six is
   only the separate minimum for independent CRS authority checkpoints. Caller
   `StableLandmark` or category strings never establish eligibility.
6. Compare OpenDRIVE, LiDAR, and survey horizontal CRS by WKT/EPSG, datum,
   metre units, and coordinate epoch. If they differ, require a separate
   hash-bound reconciliation artifact with the exact PROJ pipeline and
   source/target WKT and epochs, plus a separate detached Ed25519 attestation
   from a source-pinned geodetic authority. The signed attestation binds the
   exact artifact, operation, pipeline, CRS/epochs, verification time, and at
   least six non-collinear authority checkpoints. Checkpoint IDs and physical
   controls must be distinct from survey fit/holdout controls, and their source
   and target coordinates must remain outside the same 0.50 m exclusion
   buffer. Execute the pipeline and recompute all residuals; an arbitrary
   affine or self-declared authority cannot pass. The production CRS signer
   allowlist also remains empty until genuine keys/evidence are reviewed.
7. Reconcile vertical truth separately. Require a pinned-authority-signed
   artifact that binds the exact OpenDRIVE, LiDAR vertical WKT/EPSG/datum,
   source vertical reference and retained-source hash/name/byte count, target reference,
   operation, and at least six independent controls. Recompute the operation's
   residuals and require the authenticated offset to agree with the fitted Z
   bias within 0.10 m. The exact retained source is a required CLI input opened
   with `O_NOFOLLOW`; it must be a bounded single-link regular file whose
   descriptor/path identity remains unchanged across the read, and its recomputed
   byte hash, name, and size must equal the signed reference. A missing artifact
   is an acceptance blocker; an unsigned supplied offset is an error.
8. Bind execution to the tracked deterministic toolchain lock: exact CPython,
   NumPy, SciPy, LAS, PROJ, Pillow, cryptography, strict PDF parser, OpenBLAS identity, Linux
   architecture, a forced `Haswell` OpenBLAS kernel family, and single-thread
   numerical environment. The CLI has no
   alternate-lock option. A mismatch fails before evidence evaluation, and the
   lock/tool hashes are retained in the report. Those hashes assume the tracked
   source and release owner are trusted; if malicious source operators enter
   the threat model, production must additionally verify a separately signed
   release manifest for the registration tool and lock before invocation.

## Model and evaluation

1. Fit exactly one site-wide SE(2) transform `(tx, ty, yaw)` and one additive
   Z bias. No per-camera, per-road, per-approach, nonlinear, or local-warp
   degrees of freedom are allowed.
2. Score each finite polyline symmetrically in both map-to-LiDAR and
   LiDAR-to-map directions with point-to-nearest-segment normal residuals.
   Normalize each direction and feature so density and polyline length do not
   dominate the fit.
3. Keep holdouts outside the objective. Report fit and holdout identities,
   global/per-approach/per-feature horizontal RMSE/max, symmetric Hausdorff,
   and vertical RMSE/P95/max, plus before/after regression deltas.
   Global and per-approach reported RMSE values are point-pooled over the
   deterministic 0.10 m samples from both directions; per-feature values use
   that feature's two directions only. Acceptance applies the same absolute
   gates separately to every feature as well as the pooled summaries, so a
   dense or easy feature cannot conceal a failing sparse feature. The fitting
   objective remains equal-approach, equal-feature, equal-direction weighted.
4. Run deterministic multi-start optimization using center, near-bound seeds
   on every parameter axis, and deterministic low-discrepancy interior seeds.
   Cluster converged solutions into basins and run leave-one-approach-out
   refits over at least four distinct approaches. Report seed-bound coverage,
   Jacobian rank/condition/covariance,
   bound proximity, near-optimal separated modes, fold transform spread, and
   every failed gate.

## Fixed gates

- horizontal RMSE <= 0.25 m and max <= 0.50 m;
- symmetric Hausdorff <= 0.50 m;
- vertical RMSE <= 0.10 m, P95 <= 0.20 m, max <= 0.30 m;
- leave-one-approach-out translation/yaw spread <= 0.10 m / 0.10 deg;
- full four-parameter rank, Jacobian condition <= 1e8, no bound hit;
- no per-feature absolute-gate failure or transformed-vs-baseline regression;
- no materially separated near-optimal mode.

These limits are fixed rather than tuned against the retained holdout. The
0.10 m survey-control uncertainty ceiling is the base physical scale: 0.25 m
horizontal RMSE allows bounded map/annotation noise while 0.50 m maximum and
Hausdorff limits cap any local placement failure at five times that control
uncertainty. The 0.50 m split-exclusion radius equals the absolute horizontal
failure limit so near-copies cannot masquerade as independent truth. Vertical
0.10/0.20/0.30 m RMSE/P95/max limits are one/two/three times the same control
scale and require a separately authenticated datum operation. The 0.10 m LOAO
translation limit matches one control uncertainty; 0.10 degrees contributes
about 0.087 m at a 50 m intersection radius. The 0.01 m regression tolerance
matches the retained cloud quantization and prevents a nominally green global
fit from degrading a feature. Condition 1e8 is an upper numerical-stability
guard, not evidence accuracy. A near-optimal mode is already numeric: within
5% cost and separated by more than 0.10 m translation, 0.10 degrees yaw, or
0.10 m Z. None of these values may be relaxed to pass a candidate.

The physical error budget is conservative rather than additive permission to
consume every limit: survey/control uncertainty is capped at 0.10 m and
inter-review disagreement at 0.10 m, while raw-cloud quantization is capped at
0.05 m per axis. Their root-sum-square horizontal scale is about 0.15 m, leaving
about 0.20 m of the 0.25 m RMSE gate for map/model residual rather than assuming
all worst cases align. A failed global SE(2)+Z fit is not permission to add roll,
pitch, scale, or local warps. First classify residuals: constant site-wide XY/Z
bias suggests authoritative datum/reconciliation error; residual growing with
distance suggests scale/CRS/epoch error; approach-correlated horizontal vectors
suggest map geometry or control misidentification; planar Z gradients suggest a
vertical reference or map-grade problem. Correct and independently re-authorize
the upstream artifact, then use a new sealed holdout. Do not tune this model on
the burned holdout.

The 2018 QL2 artifact is always reported with `acceptance_eligible=false` and
is development control only. A deployment artifact is refused unless a
separate, current, licensed-deliverable-bound horizontal survey supplies raw
controls with exact projected CRS WKT/EPSG/datum/coordinate epoch/metre units
and per-control uncertainty.
The tool independently resolves every map control against the bound geometry,
requires at least 10 fit and 4 held-out controls with full two-dimensional
rank, rejects source-feature/physical-control/geometric leakage including the
0.50 m spatial exclusion buffer, re-fits the survey SE(2), and recomputes every
fit and holdout residual. Summary-only claims, JSON coordinate literals,
generic environment objects presented as stable landmarks, unsigned authority
claims, or unbound files cannot pass. This implementation does not modify
deployment state.

## Authority-key management

Production survey, CRS, annotation, LiDAR-validation, and vertical-datum signer
allowlists are empty source constants. A
runtime flag, manifest field, environment variable, or caller-provided key can
never populate them. Onboarding requires obtaining the authority public key
through an independently authenticated channel, verifying its producer/source
identity and fingerprint outside the submitted evidence bundle, adding that
exact PEM and identity through reviewed source, and rerunning every adversarial
test. Rotation pins the replacement before the old key is removed; revocation
removes the compromised key in source and invalidates every report whose
retained signer fingerprint is no longer trusted. Test keys are deterministic
fixtures injected only into the imported test module and are not present in the
CLI defaults.

Coordinate epoch is an exact evidence value, not inferred. `null` on both
otherwise equivalent CRS sources is treated as equal; one missing value or two
different values fails direct equality and requires an independently signed
reconciliation artifact that binds those exact values.

## Holdout burn accounting

The authority-signed ledger names one evaluation ID, exact annotation and
holdout hashes, zero prior evaluations, maximum count one, purpose
`final_acceptance`, a bounded authorization window, and one absolute burn
receipt path in the ledger's evidence directory. This prevents copying the
signed ledger to a new directory to manufacture an unused sibling receipt.
After annotation authority verification and before fitting, the CLI refuses to
continue if the review, ledger, or authority chain is absent, then creates that
receipt exclusively with status
`evaluation_started_holdout_burned`. An existing receipt rejects reuse. The
report retains the ledger, authority, holdout, and receipt hashes. This tool
therefore provides no unburned diagnostic path that reveals sealed holdout
metrics. Fit-only exploration must use a separately prepared development
artifact that contains no holdout truth and cannot satisfy this schema.

Before creating the local receipt, the CLI requires a source-pinned HTTPS
registry ID and a separately pinned Ed25519 registry key. It verifies a fresh
signed prior head exactly matching the authority-signed ledger, submits one
atomic compare-and-append request, and verifies the fresh signed successor head,
exact consumed entry, sequence, prior-head link, and independent inclusion read.
The entry binds the evaluation, ledger, annotation, holdout, annotation-authority,
registration-tool, and toolchain-lock hashes. Deleting or restoring the local
receipt cannot reuse the authorization because the external head already advanced;
rollback, fork, replay, and concurrent double-consume responses fail closed.
Production registry endpoint and signer allowlists remain empty until reviewed.
Before the real burn, run a full dress rehearsal with a
real-shaped but explicitly non-acceptance annotation bundle and separate test
keys through every parser, signature, CRS, optimizer, report, and refusal path.
If any failure occurs after the production receipt is created, the holdout stays
burned; recovery requires a newly collected holdout set, new independent
reviews, and a new authority-signed ledger/registry entry. Reusing or deleting
the failed receipt is forbidden.

## External evidence sequence and feasibility gate

1. The map owner exports the exact native CARLA/XODR geometry and counts the
   distinct eligible traffic-light/sign objects before commissioning survey.
2. The licensed survey provider confirms that at least 14 eligible controls,
   six additional horizontal CRS checkpoints, and six additional vertical
   controls can be physically observed with the required separation and
   uncertainty. If the site cannot supply them, acceptance remains blocked;
   counts and thresholds are not lowered to fit the site.
3. The geodetic authority supplies signed horizontal and vertical operations,
   WKT/epochs, controls, and independently authenticated public keys. The LiDAR
   authority separately signs each exact raw-cloud/validation pair.
4. Two independent annotation organizations prepare and reproduce the complete
   fixed feature denominator. A third annotation authority signs their review,
   requests the one-use holdout authorization, and supplies the signed ledger.
5. A separately operated append-only registry supplies its independently
   authenticated HTTPS endpoint and Ed25519 key, publishes the signed prior
   head, and atomically records the exact one-use consumption.
6. The release owner verifies all pinned keys through out-of-band channels,
   completes the non-holdout dress rehearsal, then performs exactly one final
   acceptance invocation. The site-count inventory and genuine artifacts are
   currently absent, so this feasibility gate is explicitly not passed.

The successful independent plan review also identified two wider roots of
trust: manual annotation/LiDAR validation and vertical datum/geoid truth. Their
fail-closed schemas, signature hooks, recomputation, and tests are implemented
above, but genuine producers, pinned production keys, current signed artifacts,
and physical controls are not. Keep deployment ineligible until that external
evidence is obtained and reviewed.

## Map export and comparison corrections

Preserve stable road/section/lane identities and every sampled contiguous
left/right road-mark interval instead of overwriting a lane with its final
marking. Segment and bind markings by exact OpenDRIVE range, type, color,
weight, lane-change, material, width, height, sway coefficients, nested
`type/line` length, space, lateral/longitudinal offsets, rule and width, and
explicit line records; bind sampled world geometry to that complete range.
Give crosswalks a content-derived stable identity rather than an
enumeration index, and namespace environment-object identities. Extend the
offline OpenDRIVE comparator with elevation, lane-offset, superelevation,
lateral-shape, lane-width, road-mark, road-link, explicit road-junction
assignment, and junction/lane-link signatures so old-vs-live topology drift
is explicit.

## Verification and exit gate

Add tests for a synthetic known transform, a local warp that one global model
must reject, exact and near-buffer split leakage despite distinct IDs, raw
deliverable and identity tampering, ineligible landmark categories, retained
CARLA-source and overlay-pixel tampering, complete nested road-mark drift,
tiny/padded/truncated/encrypted/polyglot PDFs, missing/tampered/unpinned authority signatures, caller
`StableLandmark` labels, exact 0.495 m/intersection/degenerate segment cases,
unreconciled EPSG:3857/EPSG:26910, unsigned arbitrary affine transforms,
renamed survey controls reused as CRS checkpoints, and valid signed,
independently executed PROJ pipelines, plus every hash binding, degenerate
geometry, optimizer-bound contact, coarse coordinate resolution, and
old-vs-live map mismatch. Add adversarial registry
deletion/restore/replay/rollback/fork/concurrent-consume tests and vertical
source missing/tamper/substitution/symlink/hardlink/oversize/concurrent-
replacement tests without contacting a real registry. Run
focused and full bridge tests with Python warnings treated as errors and an
explicit asyncio fixture-loop scope. A non-acceptable report exits nonzero by
default; a numeric-only development report may exit zero only with the explicit
`--development-numeric-ok` override and still cannot produce a deployment
artifact. Commit only a clean source/test/doc change. Do not push or deploy.

## Review status

After five earlier authentication failures, a fresh Claude Fable high-effort
read-only review completed successfully on 2026-07-13. Its control-count and
key-management findings are incorporated above. Its broader annotation/
validation trust-anchor and vertical-datum hooks are implemented, while the
absent genuine signed/physical inputs remain explicit deployment blockers
rather than being mislabeled as test evidence.
The sanitized retained review is
`docs/reviews/map-lidar-registration-fable-20260713.md`, SHA-256
`5d18d38e74085194b609f8a27ce1c9e2cd11d27e5e6e6355824ea46d3d044e3a`.
A second final Fable review after the holdout fail-closed change is retained at
`docs/reviews/map-lidar-registration-fable-20260713-final.md`, SHA-256
`7ef5a54f823cf516f647fa66bd4bca0023d056a37f6f7a18dafbf3fe6af38491`;
its feasible findings and explicit external blockers are incorporated above.
The scoped registry/vertical-source/PDF hardening review and final PASS are
retained at
`docs/reviews/map-lidar-registration-fable-20260713-hardening.md`, SHA-256
`4e9f995d6b5eb91171783d91f5990986059513f0e805bf4de95a9cc593309c5d`.
