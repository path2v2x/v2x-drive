import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.bridge.tools import build_map_candidate_lineage_manifest as lineage


PROJECTION = "+proj=tmerc +lat_0=37 +lon_0=-122 +datum=WGS84"


def write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def xodr(roads: int, junctions: int, *, projection: str = PROJECTION) -> bytes:
    road_xml = "".join(
        f'<road id="{index}" length="1" junction="-1">'
        f'<link/><planView><geometry s="0" x="0" y="0" hdg="0" length="1"><line/></geometry></planView>'
        f'<objects><object id="obj-{index}" type="crosswalk" s="0" t="0"/></objects>'
        f'<lanes><laneSection s="0"><right><lane id="-1"><roadMark sOffset="0" type="solid"/>'
        f'<roadMark sOffset="0.5" type="broken"/></lane></right></laneSection></lanes></road>'
        for index in range(roads)
    )
    junction_xml = "".join(f'<junction id="{index}"/>' for index in range(junctions))
    return (
        f'<OpenDRIVE><header><geoReference>{projection}</geoReference></header>'
        f'{road_xml}{junction_xml}</OpenDRIVE>'
    ).encode()


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    package = tmp_path / "package"
    fbx = write(
        package / "Richmond.fbx",
        b"Kaydara FBX Binary  \x00\x1a\x00" + (7400).to_bytes(4, "little") + b"payload",
    )
    old = write(package / "Richmond.xodr", xodr(222, 29))
    geojson = write(
        package / "Richmond.geojson",
        json.dumps({
            "type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {},
                 "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}
            ],
        }).encode(),
    )
    rrdata = write(package / "Richmond.rrdata.xml", b"<RoadRunnerMetadata><Materials/></RoadRunnerMetadata>")
    material_a = write(package / "Richmond.fbm" / "a.png", b"png-a")
    material_b = write(package / "Richmond.fbm" / "nested" / "b.png", b"png-b")
    live = write(tmp_path / "live.xodr", xodr(208, 32))
    monkeypatch.setattr(lineage, "EXPECTED_FBX_BYTES", fbx.stat().st_size)
    monkeypatch.setattr(lineage, "EXPECTED_FBX_SHA256", lineage.hashlib.sha256(fbx.read_bytes()).hexdigest())
    monkeypatch.setattr(lineage, "EXPECTED_OLD_XODR_SHA256", lineage.hashlib.sha256(old.read_bytes()).hexdigest())
    monkeypatch.setattr(lineage, "EXPECTED_LIVE_XODR_SHA256", lineage.hashlib.sha256(live.read_bytes()).hexdigest())
    return SimpleNamespace(
        package_root=str(package), fbx=str(fbx), old_xodr=str(old), live_xodr=str(live),
        geojson=str(geojson), rrdata_xml=[str(rrdata)],
        material_file=[str(material_b), str(material_a)], output=str(tmp_path / "manifest.json"),
    )


def test_build_freezes_inputs_policy_and_blocks_unresolved_lineage(inputs):
    report = lineage.build(inputs)
    assert report["schema"] == lineage.SCHEMA
    assert report["acceptance_eligible"] is False
    assert report["selection"]["status"] == "blocked_unresolved_opendrive_lineage"
    assert report["selection"]["selected_candidate_id"] is None
    assert report["selection_policy"]["lexicographic_score_precedence"] == [
        "worst_camera_road_max_px", "worst_camera_road_rmse_px",
        "worst_camera_point_p95_px", "total_robust_loss",
    ]
    assert "within_2_percent" in report["selection_policy"]["tie_rule"]
    assert report["lineage_reconciliation"]["recovered_topology"] == {
        "roads": 222, "junctions": 29,
    }
    assert report["lineage_reconciliation"]["live_topology"] == {
        "roads": 208, "junctions": 32,
    }
    old = next(
        artifact for candidate in report["candidates"] for artifact in candidate["artifacts"]
        if artifact["label"] == "recovered_old_xodr"
    )
    assert old["summary"]["object_count"] == 222
    assert old["summary"]["road_mark_count"] == 444
    assert old["summary"]["road_mark_segmented_lane_count"] == 222
    assert old["summary"]["projection"] == PROJECTION
    assert report["recovered_material_dependency_graph"]["selection_blocking_until_complete"] is True


def test_candidate_ids_are_independent_of_repeated_argument_order(inputs):
    first = lineage.build(inputs)
    inputs.material_file.reverse()
    second = lineage.build(inputs)
    assert [item["candidate_id"] for item in first["candidates"]] == [
        item["candidate_id"] for item in second["candidates"]
    ]
    assert [item["candidate_id"] for item in first["candidates"]] == [
        "live_deployed_opendrive-sha256-3a6410735b9f06928d800f54d2cb477b07b74aaf7f188847c5a1eb9a53fd1552",
        "recovered_authoring_package-sha256-de4caec2a571f0010417d9f5af6c6e4625bb8aabbf54fba154951b491d9cedb4",
    ]


def test_complete_package_inventory_rejects_omitted_rrdata_or_material(inputs):
    inputs.material_file = inputs.material_file[:1]
    with pytest.raises(lineage.LineageError, match="inventory is incomplete"):
        lineage.build(inputs)
    inputs.material_file = [
        str(Path(inputs.package_root) / "Richmond.fbm" / "a.png"),
        str(Path(inputs.package_root) / "Richmond.fbm" / "nested" / "b.png"),
    ]
    inputs.rrdata_xml = []
    with pytest.raises(lineage.LineageError, match="inventory is incomplete"):
        lineage.build(inputs)


def test_publish_is_exclusive_no_replace_and_fsyncs_complete_json(inputs):
    report = lineage.build(inputs)
    lineage.publish_no_replace(inputs.output, report)
    assert json.loads(Path(inputs.output).read_text())["schema"] == lineage.SCHEMA
    with pytest.raises(lineage.LineageError, match="refusing to replace"):
        lineage.publish_no_replace(inputs.output, report)


def test_rejects_final_and_parent_symlinks(inputs, tmp_path):
    real = Path(inputs.material_file[0])
    final_link = Path(inputs.package_root) / "material-link.png"
    final_link.symlink_to(real)
    inputs.material_file = [str(final_link)]
    with pytest.raises(lineage.LineageError, match="symbolic-link"):
        lineage.build(inputs)
    inputs.material_file = [str(real)]
    package_link = tmp_path / "package-link"
    package_link.symlink_to(Path(inputs.package_root), target_is_directory=True)
    inputs.package_root = str(package_link)
    inputs.fbx = str(package_link / "Richmond.fbx")
    with pytest.raises(lineage.LineageError, match="symbolic-link"):
        lineage.build(inputs)


def test_read_rejects_ancestor_replacement_after_open(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    package = parent / "package"
    target = write(package / "artifact.bin", b"original-bytes")
    original_read = lineage.os.read
    replaced = False

    def replacing_read(descriptor, count):
        nonlocal replaced
        value = original_read(descriptor, count)
        if not replaced:
            replaced = True
            package.rename(parent / "package-old")
            package.mkdir()
            (package / "artifact.bin").write_bytes(b"replacement")
        return value

    monkeypatch.setattr(lineage.os, "read", replacing_read)
    with pytest.raises(lineage.LineageError, match="absolute path ancestry changed"):
        lineage.read_input(str(target), "raced_artifact")


def test_build_rejects_nested_material_directory_replacement_during_inventory(
    inputs, monkeypatch,
):
    material_directory = Path(inputs.package_root) / "Richmond.fbm"
    original_listdir = lineage.os.listdir
    replaced = False

    def replacing_listdir(descriptor):
        nonlocal replaced
        values = original_listdir(descriptor)
        try:
            opened_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            return values
        if not replaced and opened_path == material_directory:
            replaced = True
            material_directory.rename(Path(inputs.package_root).parent / "Richmond.fbm-old")
            write(material_directory / "a.png", b"attacker-replacement")
            write(material_directory / "nested" / "b.png", b"png-b")
        return values

    monkeypatch.setattr(lineage.os, "listdir", replacing_listdir)
    with pytest.raises(lineage.LineageError, match="directory identity changed before inventory completed"):
        lineage.build(inputs)


def _open_fd_count():
    return len(os.listdir("/proc/self/fd"))


def _descriptor_path(descriptor):
    try:
        return Path(os.readlink(f"/proc/self/fd/{descriptor}"))
    except OSError:
        return None


def test_inventory_closes_root_fd_when_initial_ancestry_fstat_raises(
    tmp_path, monkeypatch,
):
    root = tmp_path / "package"
    write(root / "artifact.bin", b"payload")
    original_fstat = lineage.os.fstat
    baseline = _open_fd_count()
    injected = False

    def failing_fstat(descriptor):
        nonlocal injected
        if not injected:
            injected = True
            raise OSError("injected initial ancestry fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(lineage.os, "fstat", failing_fstat)
    with pytest.raises(OSError, match="injected initial ancestry fstat failure"):
        lineage.inventory_package_tree(root)
    assert _open_fd_count() == baseline


def test_inventory_closes_root_fd_when_postwalk_fstat_raises(tmp_path, monkeypatch):
    root = tmp_path / "package"
    write(root / "artifact.bin", b"payload")
    original_fstat = lineage.os.fstat
    baseline = _open_fd_count()
    root_fstats = 0

    def failing_fstat(descriptor):
        nonlocal root_fstats
        if _descriptor_path(descriptor) == root:
            root_fstats += 1
            if root_fstats == 2:
                raise OSError("injected postwalk root fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(lineage.os, "fstat", failing_fstat)
    with pytest.raises(OSError, match="injected postwalk root fstat failure"):
        lineage.inventory_package_tree(root)
    assert _open_fd_count() == baseline


def test_inventory_closes_child_directory_fd_when_initial_fstat_raises(
    tmp_path, monkeypatch,
):
    root = tmp_path / "package"
    child = root / "nested"
    write(child / "artifact.bin", b"payload")
    original_fstat = lineage.os.fstat
    baseline = _open_fd_count()
    injected = False

    def failing_fstat(descriptor):
        nonlocal injected
        if not injected and _descriptor_path(descriptor) == child:
            injected = True
            raise OSError("injected child directory fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(lineage.os, "fstat", failing_fstat)
    with pytest.raises(OSError, match="injected child directory fstat failure"):
        lineage.inventory_package_tree(root)
    assert _open_fd_count() == baseline


def test_inventory_closes_nested_file_fd_when_read_raises(tmp_path, monkeypatch):
    root = tmp_path / "package"
    target = write(root / "nested" / "artifact.bin", b"payload")
    original_read = lineage.os.read
    baseline = _open_fd_count()

    def failing_read(descriptor, count):
        if _descriptor_path(descriptor) == target:
            raise OSError("injected read failure")
        return original_read(descriptor, count)

    monkeypatch.setattr(lineage.os, "read", failing_read)
    with pytest.raises(OSError, match="injected read failure"):
        lineage.inventory_package_tree(root)
    assert _open_fd_count() == baseline


def test_inventory_closes_nested_file_fd_when_fstat_raises(tmp_path, monkeypatch):
    root = tmp_path / "package"
    target = write(root / "nested" / "artifact.bin", b"payload")
    original_fstat = lineage.os.fstat
    baseline = _open_fd_count()
    injected = False

    def failing_fstat(descriptor):
        nonlocal injected
        if not injected and _descriptor_path(descriptor) == target:
            injected = True
            raise OSError("injected fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(lineage.os, "fstat", failing_fstat)
    with pytest.raises(OSError, match="injected fstat failure"):
        lineage.inventory_package_tree(root)
    assert _open_fd_count() == baseline


def test_inventory_closes_nested_file_fd_when_hash_update_raises(tmp_path, monkeypatch):
    root = tmp_path / "package"
    write(root / "nested" / "artifact.bin", b"payload")
    original_sha256 = lineage.hashlib.sha256
    baseline = _open_fd_count()

    class FailingDigest:
        def update(self, _chunk):
            raise RuntimeError("injected hash failure")

    monkeypatch.setattr(lineage.hashlib, "sha256", lambda *args, **kwargs: FailingDigest())
    with pytest.raises(RuntimeError, match="injected hash failure"):
        lineage.inventory_package_tree(root)
    monkeypatch.setattr(lineage.hashlib, "sha256", original_sha256)
    assert _open_fd_count() == baseline

def test_rejects_hardlinks_duplicate_paths_and_outside_package(inputs, tmp_path):
    source = Path(inputs.material_file[0])
    hardlink = source.with_name("hardlink.png")
    os.link(source, hardlink)
    inputs.material_file = [str(source)]
    with pytest.raises(lineage.LineageError, match="single-link"):
        lineage.build(inputs)

    hardlink.unlink()
    inputs.material_file = [str(source), str(source)]
    with pytest.raises(lineage.LineageError, match="unique labels and paths"):
        lineage.build(inputs)

    inputs.material_file = [str(write(tmp_path / "outside.png", b"outside"))]
    with pytest.raises(lineage.LineageError, match="outside package root"):
        lineage.build(inputs)
    inputs.material_file = [str(source)]
    inputs.rrdata_xml = [str(write(tmp_path / "outside.xml", b"<RoadRunnerMetadata/>"))]
    with pytest.raises(lineage.LineageError, match="outside package root"):
        lineage.build(inputs)


@pytest.mark.parametrize("old_counts,live_counts", [((221, 29), (208, 32)), ((222, 29), (208, 31))])
def test_rejects_unexpected_topology_counts(inputs, old_counts, live_counts):
    Path(inputs.old_xodr).write_bytes(xodr(*old_counts))
    Path(inputs.live_xodr).write_bytes(xodr(*live_counts))
    with pytest.raises(lineage.LineageError, match="expected"):
        lineage.build(inputs)


def test_rejects_missing_projection_and_malformed_inputs(inputs):
    Path(inputs.live_xodr).write_bytes(xodr(208, 32, projection=""))
    with pytest.raises(lineage.LineageError, match="explicit geoReference"):
        lineage.build(inputs)
    Path(inputs.live_xodr).write_bytes(xodr(208, 32))
    Path(inputs.geojson).write_text("not-json")
    with pytest.raises(lineage.LineageError, match="valid GeoJSON"):
        lineage.build(inputs)


def test_rejects_each_pinned_identity_mismatch(inputs, monkeypatch):
    for name in (
        "EXPECTED_FBX_SHA256", "EXPECTED_OLD_XODR_SHA256", "EXPECTED_LIVE_XODR_SHA256"
    ):
        with monkeypatch.context() as scoped:
            scoped.setattr(lineage, name, "0" * 64)
            with pytest.raises(lineage.LineageError, match="pinned"):
                lineage.build(inputs)
    monkeypatch.setattr(lineage, "EXPECTED_FBX_BYTES", 1)
    with pytest.raises(lineage.LineageError, match="pinned"):
        lineage.build(inputs)


def test_malformed_lane_topology_fails_with_lineage_error():
    missing_s = xodr(1, 1).replace(b'<laneSection s="0">', b"<laneSection>")
    with pytest.raises(lineage.LineageError, match="laneSection with blank s"):
        lineage.summarize_xodr(missing_s, "missing-s")
    duplicate = xodr(1, 1).replace(
        b'</lane></right>', b'</lane><lane id="-1"/></right>'
    )
    with pytest.raises(lineage.LineageError, match="duplicate lane ID"):
        lineage.summarize_xodr(duplicate, "duplicate")


def test_blank_road_and_junction_lane_link_ids_fail_closed():
    blank_road_link = xodr(1, 1).replace(
        b"<link/>", b'<link><successor elementType="road"/></link>', 1
    )
    with pytest.raises(lineage.LineageError, match="road link with blank elementId"):
        lineage.summarize_xodr(blank_road_link, "blank-road-link")

    blank_from = xodr(1, 1).replace(
        b'<junction id="0"/>',
        b'<junction id="0"><connection id="c" incomingRoad="0" connectingRoad="0">'
        b'<laneLink to="-1"/></connection></junction>',
    )
    with pytest.raises(lineage.LineageError, match="laneLink with blank from/to"):
        lineage.summarize_xodr(blank_from, "blank-junction-from")

    blank_to = blank_from.replace(b'<laneLink to="-1"/>', b'<laneLink from="-1"/>')
    with pytest.raises(lineage.LineageError, match="laneLink with blank from/to"):
        lineage.summarize_xodr(blank_to, "blank-junction-to")


def test_cli_refuses_relative_input_and_relative_output(inputs, tmp_path):
    relative = SimpleNamespace(**vars(inputs))
    relative.fbx = "Richmond.fbx"
    with pytest.raises(lineage.LineageError, match="absolute NFC"):
        lineage.build(relative)
    with pytest.raises(lineage.LineageError, match="output path must be absolute"):
        lineage.publish_no_replace("manifest.json", {"safe": True})


def test_topology_digest_binds_road_marks_objects_lanes_and_junction_lane_links():
    baseline = xodr(1, 1)
    changed_mark = baseline.replace(b'type="broken"', b'type="solid"')
    changed_object = baseline.replace(b'id="obj-0"', b'id="obj-other"')
    assert lineage.summarize_xodr(baseline, "base")["topology_sha256"] != lineage.summarize_xodr(
        changed_mark, "mark"
    )["topology_sha256"]
    assert lineage.summarize_xodr(baseline, "base")["topology_sha256"] != lineage.summarize_xodr(
        changed_object, "object"
    )["topology_sha256"]
    with_lane_links = baseline.replace(
        b'<junction id="0"/>',
        b'<junction id="0"><connection id="c" incomingRoad="0" connectingRoad="0">'
        b'<laneLink from="-1" to="-1"/></connection></junction>',
    )
    without_lane_links = with_lane_links.replace(b'<laneLink from="-1" to="-1"/>', b"")
    assert lineage.summarize_xodr(with_lane_links, "with")["topology_sha256"] != lineage.summarize_xodr(
        without_lane_links, "without"
    )["topology_sha256"]
    lane_successor = baseline.replace(
        b'<lane id="-1">', b'<lane id="-1"><link><successor id="-1"/></link>'
    )
    changed_successor = lane_successor.replace(b'<successor id="-1"', b'<successor id="-2"')
    assert lineage.summarize_xodr(lane_successor, "successor-a")["topology_sha256"] != lineage.summarize_xodr(
        changed_successor, "successor-b"
    )["topology_sha256"]


def test_platform_without_no_follow_support_fails_closed(inputs, monkeypatch):
    monkeypatch.delattr(lineage.os, "O_NOFOLLOW")
    with pytest.raises(lineage.LineageError, match="lacks required"):
        lineage.build(inputs)


def test_main_end_to_end_and_no_replace(inputs):
    argv = [
        "--package-root", inputs.package_root,
        "--fbx", inputs.fbx,
        "--old-xodr", inputs.old_xodr,
        "--live-xodr", inputs.live_xodr,
        "--geojson", inputs.geojson,
        "--rrdata-xml", inputs.rrdata_xml[0],
    ]
    for value in inputs.material_file:
        argv.extend(("--material-file", value))
    argv.extend(("--output", inputs.output))
    assert lineage.main(argv) == 0
    assert json.loads(Path(inputs.output).read_text())["selection"]["selected_candidate_id"] is None
    with pytest.raises(SystemExit, match="refusing to replace"):
        lineage.main(argv)
