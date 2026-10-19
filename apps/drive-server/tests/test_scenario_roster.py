"""Scenario roster: scenario XML parsing, DT config resolution, live Scenario merge."""

import json
import os
import time

from digital_twin_bridge.scenario_roster import (
    ScenarioRoster,
    _norm_bsm_id,
    parse_scenario_xml,
    resolve_scenario_xml,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "v2x")

# the attribute set of DT's scenario_files/ch2_erv.xml
XML = """<?xml version="1.0"?>
<scenario>
  <vehicles>
    <vehicle name="PATH-M-1" bsmid="11111110" type="vehicle.ambulance.ford" simulationIdentifier="CARLA-PATH-1" material="Aluminum" lvcIndicator="Constructive" generate_bsms="true" organization="PATH"/>
    <vehicle name="PATH-BG-1" bsmid="11111111" type="vehicle.lincoln.mkz" simulationIdentifier="CARLA-PATH-1" lvcIndicator="Virtual" generate_bsms="true" organization="PATH"/>
    <vehicle name="VW-M-1" bsmid="00000000" type="vehicle.lincoln.mkz" simulationIdentifier="CARLA-VW-1" lvcIndicator="Constructive" generate_bsms="false" organization="VW"/>
    <vehicle name="AMG-BG-1" bsmid="22222221" type="vehicle.lincoln.mkz"/>
  </vehicles>
</scenario>
"""


def scenario_record():
    with open(os.path.join(FIXTURES, "streamer_sample.raw"), "rb") as f:
        parts = [p for p in f.read().split(b"\x1e") if p.strip()]
    for p in parts:
        r = json.loads(p)
        if r["metadata"]["type_name"] == "VUG::Configuration::Scenario":
            return r
    raise AssertionError("no Scenario record in the sample")


def test_norm_bsm_id_handles_hex_strings_and_byte_lists():
    assert _norm_bsm_id("11111110") == "11111110"
    assert _norm_bsm_id("ABC") == "00000abc"
    assert _norm_bsm_id([{"0": 17}, {"1": 17}, {"2": 17}, {"3": 16}]) == "11111110"
    assert _norm_bsm_id([0, 0, 0, 0]) == "00000000"
    assert _norm_bsm_id(None) == "" and _norm_bsm_id("") == ""


def test_parse_scenario_xml(tmp_path):
    path = tmp_path / "scn.xml"
    path.write_text(XML)
    entries = parse_scenario_xml(str(path))
    assert sorted(entries) == ["AMG-BG-1", "PATH-BG-1", "PATH-M-1", "VW-M-1"]
    amb = entries["PATH-M-1"]
    assert amb.bsm_id == "11111110" and amb.org == "PATH" and amb.vehicle_type == "vehicle.ambulance.ford"
    assert amb.sim_id == "CARLA-PATH-1" and amb.lvc == "Constructive" and amb.generate_bsms is True
    assert entries["VW-M-1"].generate_bsms is False
    assert entries["AMG-BG-1"].org == "AMG"  # derived from the name when the attribute is missing
    assert entries["AMG-BG-1"].to_dict()["bsm_id"] == "22222221"


def test_resolve_scenario_xml_from_dt_config(tmp_path):
    repo = tmp_path / "dt"
    (repo / "scenario_files").mkdir(parents=True)
    xml = repo / "scenario_files" / "ch2_erv.xml"
    xml.write_text(XML)
    cfg = tmp_path / ".dt_scenario_config"
    cfg.write_text("# SCENARIO CONFIG\nexport VUG_CARLA_MAP_NAME='Richmond_Field_Station_Richmond_CA'\n"
                   "export VUG_SCENARIO_FILE='ch2_erv.xml'\n")
    assert resolve_scenario_xml("", str(cfg), str(repo)) == str(xml)
    cfg.write_text("VUG_SCENARIO_FILE=%s\n" % xml)   # absolute path form
    assert resolve_scenario_xml("", str(cfg), str(repo)) == str(xml)
    cfg.write_text("export VUG_SCENARIO_FILE='missing.xml'\n")
    assert resolve_scenario_xml("", str(cfg), str(repo)) is None
    cfg.write_text("export VUG_EM_PORT='55100'\n")
    assert resolve_scenario_xml("", str(cfg), str(repo)) is None
    assert resolve_scenario_xml("", str(tmp_path / "nope"), str(repo)) is None
    assert resolve_scenario_xml(str(xml)) == str(xml)
    assert resolve_scenario_xml(str(tmp_path / "nope.xml")) is None


def test_roster_reloads_when_the_file_changes(tmp_path):
    path = tmp_path / "scn.xml"
    path.write_text(XML)
    roster = ScenarioRoster(xml_path=str(path), reload_interval_s=0.0)
    assert roster.get("PATH-M-1").org == "PATH" and roster.get("nope") is None
    assert roster.is_local(roster.get("PATH-BG-1")) and not roster.is_local(roster.get("VW-M-1"))
    assert not roster.is_local(None)
    assert roster.by_bsm_id("11111111").name == "PATH-BG-1"
    assert roster.by_bsm_id("00000000") is None  # the null id never matches
    assert roster.by_bsm_id("") is None
    assert roster.names() == ["AMG-BG-1", "PATH-BG-1", "PATH-M-1", "VW-M-1"]
    path.write_text(XML.replace('name="AMG-BG-1"', 'name="AMG-BG-9"'))
    later = time.time() + 5
    os.utime(path, (later, later))
    assert roster.get("AMG-BG-9") is not None and roster.get("AMG-BG-1") is None
    status = roster.status()
    assert status["file"] == str(path) and status["vehicles"] == 4 and status["local_org"] == "PATH"


def test_broken_xml_keeps_the_last_roster(tmp_path):
    path = tmp_path / "scn.xml"
    path.write_text(XML)
    roster = ScenarioRoster(xml_path=str(path), reload_interval_s=0.0)
    path.write_text("<scenario><vehicles>")
    later = time.time() + 5
    os.utime(path, (later, later))
    assert roster.get("PATH-M-1") is not None


def test_merge_live_scenario_from_the_stream(tmp_path):
    roster = ScenarioRoster(xml_path="", dt_scenario_config=str(tmp_path / "none"), dt_repo=str(tmp_path))
    assert roster.status()["file"] is None
    rec = scenario_record()
    assert roster.merge_live(rec["attributes"]) == 18
    assert roster.scenario_name == "Cohort 2 - ERV"
    amb = roster.get("PATH-M-1")
    assert amb.bsm_id == "11111110" and amb.source == "live" and amb.vehicle_type == "vehicle.ambulance.ford"
    assert roster.get("PATH-BG-5").bsm_id == "11111115"
    assert roster.get("VW-M-1").org == "VW" and roster.get("VW-M-1").lvc == "Constructive"
    assert roster.get("PATH-BG-1").lvc == "Virtual"
    assert roster.by_bsm_id("33333331").name == "UTC-BG-1"
    assert roster.by_bsm_id([{"0": 68}, {"1": 68}, {"2": 68}, {"3": 64}]).name == "FHWA-M-1"
    assert len(roster.names()) == 18 and roster.status()["live_vehicles"] == 18
    assert roster.merge_live({}) == 0 and roster.scenario_name == "Cohort 2 - ERV"


def test_xml_entry_wins_but_takes_the_live_bsm_id(tmp_path):
    path = tmp_path / "scn.xml"
    path.write_text(XML.replace('bsmid="11111110"', 'bsmid=""'))
    roster = ScenarioRoster(xml_path=str(path))
    assert roster.get("PATH-M-1").bsm_id == ""
    roster.merge_live(scenario_record()["attributes"])
    entry = roster.get("PATH-M-1")
    assert entry.source == "xml" and entry.bsm_id == "11111110"
    assert roster.status()["vehicles"] == 18  # the four XML names are all in the live scenario too
