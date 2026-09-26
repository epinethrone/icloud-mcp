"""mac-helper/ops/health.py against synthetic exports in a throwaway folder (ICLOUD_HEALTH_DIR) and store (ICLOUD_HEALTH_DB). It runs
under Apple's Python when present, because that is what the helper uses on a Mac. The refresh command is a local stand-in script."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

SCRIPT = pathlib.Path(__file__).parent.parent / "mac-helper" / "ops" / "health.py"
PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def export(generated, sections, inp=""):
    """An export as the iPhone shortcut writes it. sections: (header, {column: [lines]})."""
    out = ["#health v1 generated=%s input=%s" % (generated, inp)]
    for header, cols in sections:
        out.append("##type=" + header)
        for name in ("start", "end", "value", "unit", "source"):
            out.append("##" + name)
            out.extend(cols.get(name, []))
    return "\n".join(out) + "\n"


def hourly(day, values, offset="+02:00"):
    starts = ["%sT%02d:00:00%s" % (day, h, offset) for h in range(len(values))]
    ends = ["%sT%02d:59:59%s" % (day, h, offset) for h in range(len(values))]
    return {"start": starts, "end": ends, "value": [str(v) for v in values], "unit": ["count"] * len(values)}


@pytest.fixture()
def env(tmp_path):
    folder = tmp_path / "Health"
    folder.mkdir()
    return {"dir": folder, "db": tmp_path / "store" / "health.sqlite", "refresh": tmp_path / "health-refresh.json", "tmp": tmp_path}


def run(env, op, args=None, ok=True):
    e = {**os.environ, "ICLOUD_HEALTH_DIR": str(env["dir"]), "ICLOUD_HEALTH_DB": str(env["db"]),
         "ICLOUD_HEALTH_REFRESH": str(env["refresh"])}
    p = subprocess.run([PY, "-I", str(SCRIPT), op, json.dumps(args or {})], capture_output=True, text=True, env=e, timeout=60)
    if ok:
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)
    assert p.returncode != 0
    return p.stderr


def write(env, name, text):
    (env["dir"] / name).write_text(text)


def test_script_parses_as_python_39():
    import ast
    ast.parse(SCRIPT.read_text(), feature_version=(3, 9))


def test_overlapping_exports_are_not_counted_twice(env):
    # two snapshots of the same hours: the newer one wins inside its window, the older one only counts before it
    write(env, "health-20260910-120000.txt", export("2026-09-10T12:00:00+02:00", [
        ("Steps grouped=hour", hourly("2026-09-10", [0, 0, 0, 0, 0, 0, 0, 100, 200, 300, 50, 10]))]))
    write(env, "health-20260910-230000.txt", export("2026-09-10T23:00:00+02:00", [
        ("Steps grouped=hour", {k: v[9:] for k, v in hourly("2026-09-10", [0] * 9 + [320, 60, 10, 0, 500]).items()})]))
    got = run(env, "health_summary", {"start": "2026-09-10"})
    day = got["days"][0]
    assert day["steps"] == {"total": 100 + 200 + 320 + 60 + 10 + 500, "hours_recorded": 6}
    assert got["units"]["steps"] == "count"
    assert got["freshness"]["latest_export"] == "2026-09-10T23:00"


def test_zero_daily_value_means_no_reading_and_empty_columns_are_dropped(env):
    # Shortcuts leaves Source empty for grouped types: that column must be dropped, not zipped out of line
    write(env, "health-20260911-080000.txt", export("2026-09-11T08:00:00+02:00", [
        ("Resting Heart Rate grouped=day", {"start": ["2026-09-11T00:00:00+02:00", "2026-09-10T03:42:56+02:00"],
                                            "end": ["2026-09-11T00:00:00+02:00", "2026-09-10T09:12:34+02:00"],
                                            "value": ["0", "58"], "unit": ["count/min", "count/min"], "source": []})]))
    got = run(env, "health_summary", {"start": "2026-09-10", "end": "2026-09-11"})
    assert got["days"] == [{"date": "2026-09-10", "resting_heart_rate": 58.0}]


def test_heart_rate_readings_report_their_real_span(env):
    starts = ["2026-09-12T12:%02d:00+02:00" % m for m in range(26, 60)]
    write(env, "health-20260912-130000.txt", export("2026-09-12T13:00:00+02:00", [
        ("Heart Rate", {"start": starts, "end": starts, "value": [str(60 + i % 27) for i in range(len(starts))],
                        "unit": ["count/min"] * len(starts), "source": ["Watch"] * len(starts)})]))
    hr = run(env, "health_summary", {"start": "2026-09-12"})["days"][0]["heart_rate"]
    assert (hr["min"], hr["max"], hr["readings"], hr["from"], hr["to"]) == (60.0, 86.0, 34, "12:26", "12:59")
    detail = run(env, "health_day", {"date": "2026-09-12", "metric": "heart_rate"})
    assert len(detail["readings_shown"]) == 34 and detail["unit"] == "count/min"


def test_sleep_across_noon_is_one_sleep_dated_by_its_end_with_overlaps_merged(env):
    rows = [("2026-09-13T23:40:00+02:00", "2026-09-14T02:00:00+02:00", "Core"),
            ("2026-09-14T01:30:00+02:00", "2026-09-14T03:00:00+02:00", "Deep"),       # overlaps the Core interval by 30 minutes
            ("2026-09-14T03:00:00+02:00", "2026-09-14T03:20:00+02:00", "Awake"),
            ("2026-09-14T03:20:00+02:00", "2026-09-14T12:10:00+02:00", "REM"),
            ("2026-09-14T16:00:00+02:00", "2026-09-14T16:30:00+02:00", "Core")]      # a nap, hours later: a separate sleep
    write(env, "health-20260914-180000.txt", export("2026-09-14T18:00:00+02:00", [
        ("Sleep", {"start": [r[0] for r in rows], "end": [r[1] for r in rows], "value": [r[2] for r in rows]})]))
    sleep = run(env, "health_summary", {"start": "2026-09-14"})["sleep"]
    assert [s["start"] for s in sleep] == ["2026-09-13T23:40", "2026-09-14T16:00"]
    night = sleep[0]
    assert night["date"] == "2026-09-14" and night["end"] == "2026-09-14T12:10"
    assert night["asleep_minutes"] == 200 + 530          # 23:40-03:00 merged, plus REM; never 230 + 90
    assert night["awake_minutes"] == 20
    stages = run(env, "health_day", {"date": "2026-09-14", "metric": "sleep"})["stages"]
    assert stages[0] == {"stage": "Core", "start": "23:40", "end": "02:00"}


def test_half_written_section_and_non_exports_are_ignored(env):
    write(env, "health-20260915-090000.txt", export("2026-09-15T09:00:00+02:00", [
        ("Steps grouped=hour", {"start": ["2026-09-15T08:00:00+02:00"], "end": []}),                 # still syncing
        ("Walking Heart Rate Average grouped=day", {"start": ["2026-09-15T00:00:00+02:00"], "end": ["2026-09-15T00:00:00+02:00"],
                                                    "value": ["97"], "unit": ["count/min"]})]))
    write(env, "notes.txt", "#health v1 generated=2026-09-15T09:00:00+02:00\n")
    write(env, "health-20260915-100000.txt", "not an export")
    got = run(env, "health_summary", {"start": "2026-09-15"})
    assert got["days"] == [{"date": "2026-09-15", "walking_heart_rate": 97.0}]
    assert run(env, "health_status")["exports"] == 1


def test_changed_file_is_read_again(env):
    name = "health-20260916-090000.txt"
    write(env, name, export("2026-09-16T09:00:00+02:00", [("Steps grouped=hour", hourly("2026-09-16", [0, 0, 0, 0, 0, 0, 0, 0, 40]))]))
    assert run(env, "health_summary", {"start": "2026-09-16"})["days"][0]["steps"]["total"] == 40
    write(env, name, export("2026-09-16T09:00:00+02:00", [("Steps grouped=hour", hourly("2026-09-16", [0, 0, 0, 0, 0, 0, 0, 0, 45]))])
          + "\n")
    assert run(env, "health_summary", {"start": "2026-09-16"})["days"][0]["steps"]["total"] == 45


def test_arguments_are_checked(env):
    assert "at most 92 days" in run(env, "health_summary", {"start": "2026-01-01", "end": "2026-06-01"}, ok=False)
    assert "before start" in run(env, "health_summary", {"start": "2026-02-02", "end": "2026-02-01"}, ok=False)
    assert "unknown metric" in run(env, "health_day", {"date": "2026-02-02", "metric": "nope"}, ok=False)


def test_store_is_private(env):
    run(env, "health_status")
    assert oct(env["db"].stat().st_mode & 0o777) == "0o600"


def test_refresh_needs_the_macs_own_setting(env):
    got = run(env, "health_refresh", {"budget": 8})
    assert got["refreshed"] is False and "not set up" in got["reason"]


def test_refresh_runs_the_configured_command_once_and_reads_the_new_export(env):
    # stand-in for the message to the iPhone: a script that writes the export the phone would send
    marker = env["tmp"] / "ran"
    fake = env["tmp"] / "phone.sh"
    body = export("2026-09-17T10:00:00+02:00", [("Steps grouped=hour", hourly("2026-09-17", [0, 0, 0, 0, 0, 0, 0, 0, 0, 12]))])
    (env["tmp"] / "body.txt").write_text(body)
    fake.write_text('#!/bin/sh\necho x >> "%s"\ncp "%s" "%s"\n' % (marker, env["tmp"] / "body.txt", env["dir"] / "health-20260917-100000.txt"))
    fake.chmod(0o755)
    env["refresh"].write_text(json.dumps({"command": [str(fake)], "min_minutes": 10}))
    got = run(env, "health_refresh", {"budget": 20})
    assert got["refreshed"] is True and marker.read_text().count("x") == 1
    # asked again at once: the latest export is recent in file time, and the command is not run a second time within min_minutes
    (env["dir"] / "health-20260917-100000.txt").unlink()
    again = run(env, "health_refresh", {"budget": 8})
    assert again["refreshed"] is False and marker.read_text().count("x") == 1


def test_refresh_command_cannot_come_from_the_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location("helper", SCRIPT.parent.parent / "icloud_mac_helper.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    assert helper.OPS["health_refresh"] == {}
    with pytest.raises(helper.HelperError):
        helper.validate_args("health_refresh", {"command": ["/bin/sh"]})


def test_drive_operations_cannot_reach_the_health_folder(tmp_path):
    """The exports live in the Shortcuts app's iCloud folder, a sibling of the Drive root: every drive_* path is refused there."""
    root = tmp_path / "Mobile Documents" / "com~apple~CloudDocs"
    health = tmp_path / "Mobile Documents" / "iCloud~is~workflow~my~workflows" / "Documents" / "Health"
    root.mkdir(parents=True)
    health.mkdir(parents=True)
    (health / "health-20260918-100000.txt").write_text("#health v1 generated=2026-09-18T10:00:00+02:00\nsecret heart data\n")
    drive = pathlib.Path(__file__).parent.parent / "mac-helper" / "ops" / "drive.py"
    e = {**os.environ, "ICLOUD_DRIVE_ROOT": str(root)}
    for op, args in (("drive_read_file", {"path": "../iCloud~is~workflow~my~workflows/Documents/Health/health-20260918-100000.txt"}),
                     ("drive_list_folder", {"path": "../iCloud~is~workflow~my~workflows"})):
        p = subprocess.run([PY, "-I", str(drive), op, json.dumps(args)], capture_output=True, text=True, env=e, timeout=30)
        assert p.returncode != 0 and "secret" not in p.stdout
    p = subprocess.run([PY, "-I", str(drive), "drive_search_content", json.dumps({"query": "secret", "download": False})],
                       capture_output=True, text=True, env=e, timeout=30)
    assert "health-2026" not in p.stdout


def test_an_empty_export_is_not_fresh_data(env):
    # what a run on a locked iPhone leaves: the header and nothing else
    write(env, "health-20260919-080000.txt", export("2026-09-19T08:00:00+02:00", [("Steps grouped=hour", hourly("2026-09-19", [5]))]))
    write(env, "health-20260919-090000.txt", export("2026-09-19T09:00:00+02:00", []))
    assert run(env, "health_status")["latest_export"] == "2026-09-19T08:00"


EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE HealthData [
<!ELEMENT HealthData (ExportDate,Me,Record*)>
]>
<HealthData locale="en_NL">
 <ExportDate value="2026-09-20 12:00:00 +0200"/>
 <Me HKCharacteristicTypeIdentifierDateOfBirth=""/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Phone" unit="count" startDate="2026-01-10 09:05:00 +0100" endDate="2026-01-10 09:15:00 +0100" value="300"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Phone" unit="count" startDate="2026-01-10 09:20:00 +0100" endDate="2026-01-10 09:30:00 +0100" value="200"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Watch" unit="count" startDate="2026-01-10 09:04:00 +0100" endDate="2026-01-10 09:31:00 +0100" value="480"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Watch" unit="count" startDate="2026-01-10 14:00:00 +0100" endDate="2026-01-10 14:10:00 +0100" value="100">
  <MetadataEntry key="HKMetadataKeySyncVersion" value="2"/>
 </Record>
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" sourceName="Watch" unit="count/min" startDate="2026-01-10 03:00:00 +0100" endDate="2026-01-10 09:00:00 +0100" value="55"/>
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" sourceName="Watch" unit="count/min" startDate="2026-01-10 15:00:00 +0100" endDate="2026-01-10 21:00:00 +0100" value="57"/>
 <Record type="HKQuantityTypeIdentifierOxygenSaturation" sourceName="Watch" unit="%" startDate="2026-01-10 04:00:00 +0100" endDate="2026-01-10 04:00:00 +0100" value="0.97"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch" unit="count/min" startDate="2026-01-10 10:00:00 +0100" endDate="2026-01-10 10:00:00 +0100" value="70"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Phone" unit="count/min" startDate="2026-01-10 10:00:00 +0100" endDate="2026-01-10 10:00:00 +0100" value="70"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch" unit="count/min" startDate="2026-01-10 11:00:00 +0100" endDate="2026-01-10 11:00:00 +0100" value="90"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Phone" startDate="2026-01-09 23:00:00 +0100" endDate="2026-01-10 09:00:00 +0100" value="HKCategoryValueSleepAnalysisAsleepUnspecified"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" startDate="2026-01-10 01:00:00 +0100" endDate="2026-01-10 04:00:00 +0100" value="HKCategoryValueSleepAnalysisAsleepCore"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" startDate="2026-01-10 04:00:00 +0100" endDate="2026-01-10 05:00:00 +0100" value="HKCategoryValueSleepAnalysisAsleepDeep"/>
 <Record type="HKQuantityTypeIdentifierDietaryWater" sourceName="App" unit="mL" startDate="2026-01-10 10:00:00 +0100" endDate="2026-01-10 10:00:00 +0100" value="250"/>
</HealthData>
"""


def test_full_export_import_does_not_double_count_sources(env):
    import zipfile
    z = env["tmp"] / "export.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("apple_health_export/export.xml", EXPORT_XML)
    e = {**os.environ, "ICLOUD_HEALTH_DIR": str(env["dir"]), "ICLOUD_HEALTH_DB": str(env["db"])}
    p = subprocess.run([PY, "-I", str(SCRIPT), "import", str(z)], capture_output=True, text=True, env=e, timeout=60)
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout)
    assert got["export_date"] == "2026-09-20T12:00:00+02:00" and got["history_from"] == "2026-01-10"
    day = run(env, "health_summary", {"start": "2026-01-10"})
    d = day["days"][0]
    assert d["steps"] == {"total": 500 + 100, "hours_recorded": 2}          # the larger source total wins the hour, never 300 + 200 + 480
    assert d["resting_heart_rate"] == 56.0 and d["blood_oxygen"] == 97.0
    assert d["heart_rate"]["readings"] == 2                                  # the same reading from two devices counts once
    night = day["sleep"][0]
    assert night["asleep_minutes"] == 240 and night["start"] == "2026-01-10T01:00"   # the Watch's staged sleep, not the iPhone's guess
    # a newer iPhone export still wins inside its own window
    write(env, "health-20260921-100000.txt", export("2026-09-21T10:00:00+02:00", [("Steps grouped=hour", hourly("2026-09-21", [0, 7]))]))
    assert run(env, "health_summary", {"start": "2026-09-21"})["days"][0]["steps"]["total"] == 7
