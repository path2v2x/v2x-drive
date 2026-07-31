# V2X Richmond UE5.5 map-correction recovery plan

Status: acceptance-blocking, source-only plan. No production or live service
mutation is authorized by this document.

## Proven failure

- The fixed completion contract is
  `docs/v2x-calibration-completion-contract.md`.
- The camera-independent planar test at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T103000Z-ch4-crosswalk-planar-consistency-v1/`
  fits one coplanar Richmond crosswalk exactly and projects other visible
  crosswalks tens to hundreds of pixels from their physical locations.
- The bounded v6 inverse-render corpus at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T104045Z-inverse-render-search-v6/`
  has 895 retained diagnostic evaluations and no passing camera.
- The shared-cluster 19-parameter diagnostic at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T121500Z-joint-visual-selfcal-v1/`
  is full-rank, but five parameters hit bounds and the frozen ch1/ch2 road
  holdouts remain approximately 98/541 px at 640-wide. It is rejected.
- Runtime inspection exposes eight aggregate `RoadLines` objects. Individual
  bad crosswalks cannot be disabled; hiding all aggregates removes the whole
  road-marking layer.
- The shared geodetic/OpenDRIVE projection chain is not the observed failure:
  its retained anchor check is accurate to about 0.015 mm. A single global
  SE(2) adjustment nevertheless fails approach-held-out stability and cannot
  repair the crosswalk/topology contradiction.
- The current diagnostic OpenDRIVE exporter is not correction-grade. Its
  waypoint loop overwrites lane-marking metadata and retains only the final
  left/right marking for a lane; enumeration-only `crosswalk-N` identities are
  also unstable. A corrected export must preserve segmented `roadMark` s/t
  ranges with stable road/lane/range identities and stable OpenDRIVE
  road/object IDs for crosswalks.
- Current QL2 LiDAR comparison supports vertical RMSE/P95 of approximately
  0.044/0.087 m, but it has no horizontal residual or current road-paint truth.
  It cannot authorize a global alignment or road-marking correction.

## Source and capacity audit

- `SimForgeinc/RFS_Reconstruction` main at
  `d14da5b57bbe4356930a2b9a926a675692e18547` is an Unreal 4.26 project. It has
  updated April Richmond `.uasset`/`.umap` files, including
  `New_RFS/Richmond_Field_Station_Richmond_CA.uasset`, but its complete Git tree
  has no `.rrscene`, FBX, OBJ, USD, glTF, OpenDRIVE, GIS, Blender, Maya, or 3ds
  Max source for Richmond.
- The raw authoring export has now been recovered at
  `/home/path/Downloads/entire scene-20260211T002439Z-1-001 (2)/entire scene/`.
  `Richmond.fbx` is 163,879,392 bytes with SHA-256
  `68e889cf8d2ab17cc2005c5e7364fd64608723b819df747c102d95a53757e3e0`.
  The package also contains Richmond GeoJSON, RoadRunner metadata/materials,
  and `Richmond.xodr`; the latter is an older 222-road/29-junction export with
  SHA-256
  `ed2e44492616901fbb20b89191ab03d666c0217620d0247e55235c116f5cf2b1`
  (the local CARLA cache carries that same older file). The deployed UE5.5 map
  instead reports 208 roads/32 junctions and SHA-256
  `0737f3d9f9f344c06b2c63fe669afa8a15f814568ee9c16046795338f56f5ee1`.
  The retained comparison is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T104500Z-opendrive-source-audit/compare-old-richmond.json`.
  The strict transverse-Mercator declaration is available in both, but their
  byte/topology lineage is not reconciled. This recovers an authoring source
  candidate; it does not prove that candidate is the source of the live map,
  or resolve geometry correctness or survey truth.
- The connected `Reconstruction Map Overview` sheet records a completed 158 GB
  Richmond Unreal export dated 2026-03-30, but its linked Drive folder
  `1wWDuWUV6wSuFE3MnPTfTXljezI6xajiI` is no longer accessible to the connected
  account (Drive API 404). Drive and Slack searches found no replacement FBX,
  RoadRunner project, or accessible export archive. That historical Drive
  outage is retained provenance but is no longer the source-availability
  blocker because the local raw package above is hash-verified.
- The approved 42 GB `ghcr.io/simforgeinc/carla-rr-maps:0.10.0` image is a
  cooked runtime. It has no source label or editable CARLA project.
- The local `/home/path/V2XCarla/Carla` checkout is a dirty UE4 development
  project and is not an eligible UE5.5 map source.
- The separate UE6 comparison workspace remains excluded. Dedicated V2X UE5.5
  source/build capacity is now resolved at `/mnt/v2x-ue5` on the 500 GB
  loop-backed ext4 image `/mnt/v2x-capacity/v2x-ue5-build.ext4`. Continue to
  verify both mounts after reboot and keep every source/build/evidence artifact
  inside that V2X-owned workspace.

## Accepted recovery routes

### Route A — recover the original map-authoring source (preferred)

The recovered Richmond package is now a Route A candidate. Verify the exact FBX,
GeoJSON, RoadRunner metadata/material graph, and both the older bundle and live
OpenDRIVE identities as one immutable lineage record. Reconcile why the bundle
has 222 roads/29 junctions while live has 208/32 before selecting an import or
replacement fingerprint. It
must contain the physical road edges, lane markings, and every visible
crosswalk. Preserve the current map origin and the accepted OpenDRIVE hash
unless an independent survey explicitly selects and versions a replacement.

### Route B — independently reauthor the complete road layer

If the original source no longer exists, acquire an independently surveyed
site control network: at least six stable landmarks and ten non-collinear
distances with datum/elevation uncertainty, plus surveyed road edges, lane
center/edge lines, and crosswalk vertices. Rebuild the complete road/marking
layer—not a camera-specific overlay—from that common world geometry.

Neither route may infer world truth from persisted detection GPS, current
CARLA actor positions, lane snapping, proposal camera poses, or per-camera
homographies.

## Implementation gate

1. Revalidate the existing dedicated `/mnt/v2x-ue5` ext4 capacity and its clean
   V2X CARLA `ue5-dev`/Unreal Engine 5.5 source fingerprints after reboot. It
   may not share content, build products, processes, ports, or evidence with
   UE6.
2. Freeze a no-replace hash inventory of the recovered FBX, older bundle XODR,
   live XODR evidence, GeoJSON, RoadRunner metadata, and material dependencies
   before import. Keep the `ed2e444...` versus `0737f3...` lineage gate open
   until the 222/29 versus 208/32 topology difference is explained and a
   surveyed, versioned target fingerprint is selected.
3. Correct and regression-test the geometry exporter so lane markings retain
   all OpenDRIVE `roadMark` ranges with stable road/lane/s/t identities and
   crosswalks use stable road/object IDs. Reject global SE(2) proposals that
   fail any approach-held-out partition.
4. Import the complete source/dependency graph into the actual CARLA UE5.5
   project. Keep the map, static ground, road-line materials, traffic controls,
   semantic tags, OpenDRIVE, and package manifest versioned together.
5. Cook a complete fingerprinted CARLA package/image. A loose mount, isolated
   `.uasset`, debug drawing, or partial package transplant is an automatic
   failure.
6. In a rollback-captured zero-session V2X maintenance window, boot only the
   approved isolated UE5.5 worker on ports 2300–2302. Apply the 180-second hard
   map-load deadline and fail on crash, Vulkan/OOM signature, map/hash drift,
   unexpected sensors, or any production restart-counter change.
7. Render fit/dev views for all four cameras, then evaluate one newly frozen,
   untouched time-disjoint holdout. Burn that holdout after first inspection.
8. Pass at 1280×960: road-polyline RMSE/max ≤6/12 px and static landmark
   RMSE/P95/max ≤10/16/24 px for every camera, with no parameter bound hit,
   full data Jacobian rank, condition ≤1e8, and independent map survey
   RMSE/max ≤0.25/0.50 m.
9. Only after the static gate passes may the existing exact same-car/contact
   corpus enter world localization, identity, UE5 actor placement, replay,
   deployment, Playwright, and 30-minute/24-hour stability gates.

## Current executable action

Remain fail-closed. A source candidate and dedicated UE5 capacity now exist;
the next source-only action is to freeze the recovered package inventory,
reconcile the older-bundle/live OpenDRIVE lineage, validate the complete
dependency/material graph and coordinate conventions, fix the export identity
and segmented-marking contract, and prepare a reviewed import/cook plan inside
`/mnt/v2x-ue5`. Do not import or deploy from this planning update. Independent
survey truth, measured physical
intrinsics, corrected four-camera static geometry, fresh untouched holdouts,
and the complete cook/runtime gates remain open; do not deploy a camera pose or
place acceptance actors.
