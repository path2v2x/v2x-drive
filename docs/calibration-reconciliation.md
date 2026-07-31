# M7 Calibration Campaign — Branch Reconciliation Analysis

Prepared 2026-07-16 against `origin/main` = `400c327` (merge of PR #54,
2026-07-13). Supports milestone M7 of `docs/twin-mirroring-roadmap.md`.

## PR #21 status

"Integrate fail-closed V2X calibration and identity gates" — OPEN (draft),
head `codex/v2x-calibration-integration` (`f3abf2e`), created 2026-07-11,
never rebased. **61 ahead / 106 behind** `origin/main`; the 106 behind are
the entire perception PTS/HLS series (PRs #25–#54). A merge simulation
(`git merge-tree --write-tree`) reports **13 conflicted files**, all in the
core perception/bridge surface that the PTS series rewrote.

Content accounting of its 61 commits: 28 are patch-identical on
`codex/v2x-strict-calibration-integration`, 14 more exist there by subject
(reworked), 4 landed in `main` via PRs #44–#46, 14 HLS/web commits shipped
reworked through the merged HLS series, and exactly **one commit is
unlanded anywhere**: `b6d5f30` "Add hash-gated Richmond road migration"
(`scripts/migrate-richmond-road-core.sh` + test).

**Verdict: PR #21 is ~98% superseded. Do not rebase it** — resolving 13
conflicts would reproduce work that already exists newer on the strict
line. Close it in favor of the strict-line PR once reviewed.

## Branch inventory

| Branch | Ahead | Behind | Tip | Theme |
|---|---|---|---|---|
| `codex/v2x-strict-calibration-integration` | 116 | 0 | 07-14 | Apex: full fail-closed calibration + Tier-B + topology + exact-frame detector evidence (214 files, +75k) |
| `codex/v2x-exact-frame-detector-eval` | 114 | 0 | 07-14 | Ancestor of strict |
| `codex/v2x-dynamic-c0-adapter` | 113 | 0 | 07-14 | Ancestor of strict |
| `codex/v2x-dense-sequence-proposals` | 57 | 0 | 07-13 | Fan-out leaf; all 11 unique commits patch-identical in strict |
| `codex/v2x-calibration-pr53-reconcile` | 56 | 0 | 07-13 | Fan-out leaf; all 5 unique commits in strict |
| `codex/v2x-dynamic-placement-contract` | 52 | 0 | 07-13 | Fan-out leaf; all 8 unique commits in strict |
| `codex/v2x-dense-kvs-pagination` | 46 | 0 | 07-13 | **2 unique commits not in strict** (`ec01498`, `6f6ab37`: dense KVS window race-safety) |
| `codex/v2x-calibration-integration` (PR #21) | 61 | 106 | 07-12 | Stale original |
| `codex/v2x-calibration-current` | 40 | 55 | 07-12 | Pre-rebase snapshot; fully represented in strict |
| `codex/v2x-calibration-gates`, `codex/v2x-default-lens-projection`, `codex/v2x-hls-latency` | 0 | — | 07-10 | Already merged |

All seven 0-behind branches share a 44-commit reconciliation trunk ending
at `d05de7a`; strict continues linearly from it and fast-forwards onto
`main` conflict-free.

## Recommended landing order

1. **Review and land `codex/v2x-strict-calibration-integration`** (pushed
   to origin 2026-07-16; was previously local-only — a workstation loss
   would have erased the campaign). Split for review at the natural
   seams: trunk (44 commits, calibration reconciliation) →
   sensor-destruction + AWS prerequisite gates → Tier-B static/dynamic C0
   + OpenDRIVE topology → exact-frame detector evidence.
2. **Rebase `codex/v2x-dense-kvs-pagination`'s 2 leaf commits** onto the
   strict line as a small follow-up PR (expect one conflict in
   `apps/perception/tools/capture_dense_kvs_window.py`).
3. **Decide the Richmond orphan** `b6d5f30`: cherry-pick or drop in favor
   of the newer map-lineage work already on strict.
4. **Close PR #21** with a pointer to the strict-line PR.
5. **Delete superseded branches** after step 1 lands: the merged trio,
   plus `calibration-current`, `dynamic-placement-contract`,
   `calibration-pr53-reconcile`, `dense-sequence-proposals`,
   `dynamic-c0-adapter`, `exact-frame-detector-eval`. Keep
   `dense-kvs-pagination` until step 2 lands.

Deployment reminder: landing code does not deploy calibration. Fitted
poses stay behind the fail-closed acceptance gate (M5/M6 site
measurements) per the operating skill.
