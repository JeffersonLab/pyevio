"""The file API on the synthetic stream file and a golden file: numbers equal stream_stats called directly."""
import threading
import time

import numpy as np
import pytest

from conftest import open_and_wait, wait_done
from pyevio import stream_stats
from pyevio.decoders import fadc250_stream as fs
from pyevio.web import jobs


def test_open_progress_summary_match_direct_stats(client, served_root):
    file_id, progress = open_and_wait(client, served_root["synthetic"])
    assert progress["fraction"] == 1.0 and progress["records_done"] == progress["records_total"] == 3
    direct = stream_stats.compute_stream_stats(str(served_root["root"] / served_root["synthetic"]), workers=1)

    summary = client.get(f"/api/files/{file_id}/summary").json()
    totals = summary["totals"]
    assert totals["n_hits"] == direct.n_hits == served_root["expected_hits"]
    assert totals["n_records"] == direct.n_records == 3 and totals["n_trailer_records"] == 1
    assert totals["n_events"] == 13 and totals["n_frames"] == 12 and totals["n_other_events"] == 1
    assert totals["non_fadc_words"] == direct.non_fadc_words and totals["n_empty_payload_banks"] == 24
    assert totals["hit_rate_hz"] == pytest.approx(direct.n_hits / direct.live_time_s)
    assert summary["frames"]["stride"] == 4 and summary["frames"]["missing_frames"] == 1 and summary["frames"]["duplicate_frames"] == 1
    assert [row["roc"] for row in summary["rocs"]] == [7, 44]
    assert summary["rocs"][0]["modules"] == ["fADC250"] and summary["rocs"][1]["modules"] == ["DCRB"]
    assert summary["file"]["header_layout"] == "v6" and summary["file"]["record_count"] == 3 and summary["file"]["has_trailer"]
    assert summary["records"] == {"count": 3, "has_trailer": True, "events": 13, "events_found": 13, "with_errors": 0,
                                  "last_record_bit": 0, "compressed": 0, "without_index": 0, "truncated": 0,
                                  "min_size": summary["records"]["min_size"], "max_size": summary["records"]["max_size"],
                                  "mean_size": summary["records"]["mean_size"]}
    checks = {row["name"]: row for row in summary["checks"]}
    assert checks["missing frames"]["status"] == "warn" and checks["decode errors"]["status"] == "ok"
    assert checks["decode warnings"] == {"name": "decode warnings", "status": "ok", "value": 0, "detail": ""}
    assert totals["warnings"] == 0
    # Every synthetic frame's AIS repeats one entry, as the SRO hardware writes it
    assert totals["frames_with_ais"] == totals["frames_with_degenerate_ais"] == 12
    assert checks["AIS entries identical"]["status"] == "info" and checks["AIS entries identical"]["value"] == 12
    assert "repeats one stream's entry in every slot" in checks["AIS entries identical"]["detail"]
    assert summary["stats"]["cached"] is False


def test_distinct_ais_entries_pass_the_check(client, served_root):
    """A file whose AIS lists 24 distinct streams gets an ok row with no detail."""
    from test_stream_stats import write_synthetic_file
    write_synthetic_file(served_root["root"] / "runs" / "distinct_ais.evio", distinct_ais=True)
    file_id, _ = open_and_wait(client, "runs/distinct_ais.evio")

    summary = client.get(f"/api/files/{file_id}/summary").json()
    assert summary["totals"]["frames_with_ais"] == 12 and summary["totals"]["frames_with_degenerate_ais"] == 0
    checks = {row["name"]: row for row in summary["checks"]}
    assert checks["AIS entries identical"] == {"name": "AIS entries identical", "status": "ok", "value": 0, "detail": ""}


def test_summary_before_done_is_409(client, served_root, monkeypatch):
    release = threading.Event()

    def slow_compute(path, workers=None, progress=None, cancel=None, **_):
        release.wait(5)
        return stream_stats.compute_stream_stats(path, workers=1)

    monkeypatch.setattr(jobs, "compute_stream_stats", slow_compute)
    file_id = client.post("/api/open", json={"path": served_root["synthetic"]}).json()["file_id"]
    assert client.get(f"/api/files/{file_id}/summary").status_code == 409
    assert client.get(f"/api/files/{file_id}/hist/rate").status_code == 409
    release.set()
    assert wait_done(client, file_id)["state"] == "done"


def test_records_paging(client, served_root):
    file_id, _ = open_and_wait(client, served_root["synthetic"])
    first = client.get(f"/api/files/{file_id}/records", params={"from": 0, "count": 2}).json()
    assert first["total"] == 4 and [row["index"] for row in first["rows"]] == [0, 1]
    assert first["rows"][0]["event_count"] == 4 and first["rows"][0]["events_found"] == 4 and first["rows"][0]["errors"] == []
    assert first["rows"][0]["header_type"] == 0 and not first["rows"][0]["is_trailer"]
    assert first["rows"][1]["offset"] == first["rows"][0]["offset"] + first["rows"][0]["size"]
    rest = client.get(f"/api/files/{file_id}/records", params={"from": 2, "count": 10}).json()
    assert [row["index"] for row in rest["rows"]] == [2, 3]
    assert rest["rows"][1]["is_trailer"] and rest["rows"][1]["header_type"] == 3 and rest["rows"][1]["bit_info"]["is_last_record"]
    assert first["rows"][1]["event_count"] == 5 and rest["rows"][0]["event_count"] == 4  # record 1 holds the extra non-stream event
    assert client.get(f"/api/files/{file_id}/records", params={"from": 99}).json()["rows"] == []


def test_structure_walk_to_hits(client, served_root):
    file_id, _ = open_and_wait(client, served_root["synthetic"])
    root = client.get(f"/api/files/{file_id}/structure").json()
    assert root["node"]["kind"] == "file" and root["total"] == 4
    assert [child["id"] for child in root["children"]] == ["r0", "r1", "r2", "t"]
    assert root["children"][0]["has_children"] and not root["children"][3]["has_children"]

    record = client.get(f"/api/files/{file_id}/structure", params={"node": "r1"}).json()
    assert record["node"]["kind"] == "record" and record["total"] == 5
    event = record["children"][0]
    assert event["id"] == "r1/e0" and event["kind"] == "event" and event["tag"] == fs.STREAM_EVENT_TAG and event["has_children"]
    assert record["children"][4]["tag"] == 0xFF50  # the non-stream event

    banks = client.get(f"/api/files/{file_id}/structure", params={"node": "r1/e0"}).json()
    roles = [(child["tag"], child["role"]) for child in banks["children"]]
    assert roles == [(fs.SIB_TAG, "stream_info"), (7, "roc_bank"), (44, "roc_bank")]
    roc_7 = banks["children"][1]
    assert roc_7["roc"] == 7 and roc_7["stream"] == 1

    payloads = client.get(f"/api/files/{file_id}/structure", params={"node": roc_7["id"]}).json()
    by_port = {child.get("port"): child for child in payloads["children"] if child["role"] != "roc_stream_info"}
    assert payloads["children"][0]["role"] == "roc_stream_info" and payloads["children"][0]["tag"] == fs.ROC_STREAM_INFO_TAG
    assert by_port[1]["role"] == "payload_hits" and by_port[1]["n_words"] == 16 and by_port[1]["module"] == "fADC250" and by_port[1]["has_children"]
    assert by_port[5]["role"] == "payload_empty" and by_port[5]["n_words"] == 0 and not by_port[5]["has_children"]

    hits = client.get(f"/api/files/{file_id}/structure", params={"node": by_port[1]["id"]}).json()
    assert hits["total"] == 16 and [hit["channel"] for hit in hits["children"]] == list(range(16))
    n = 1 * 4  # record 1, event 0 → frame index 4: the builder writes time_raw = 100 * ch + n, charge = 10 + n
    assert [hit["time_raw"] for hit in hits["children"]] == [100 * ch + n for ch in range(16)]
    assert [hit["charge"] for hit in hits["children"]] == [10 + n] * 16
    assert hits["children"][1]["t_ns"] == (100 + n) * fs.HIT_TIME_TICK_NS and not hits["children"][1]["flag"]
    assert hits["children"][0]["word"] == f"{fs_hit_word(0, n, 10 + n):08x}"

    # The 0x41 payload-port map arrives decoded by the decoder (uint16 entries in memory order, zero entries kept).
    info = client.get(f"/api/files/{file_id}/structure", params={"node": payloads["children"][0]["id"]}).json()
    assert [child["role"] for child in info["children"]] == ["time_slice", "payload_map"]
    assert info["children"][1]["entries"] == [{"port": 1, "module_id": 0, "module": "fADC250"}, {"port": 3, "module_id": 0, "module": "fADC250"},
                                              {"port": 5, "module_id": 0, "module": "fADC250"}]
    sib = client.get(f"/api/files/{file_id}/structure", params={"node": banks["children"][0]["id"]}).json()
    assert [child["role"] for child in sib["children"]] == ["time_slice", "aggregation_info"] and "entries" not in sib["children"][1]
    assert record["node"]["compression"] == "none"

    dcrb = client.get(f"/api/files/{file_id}/structure", params={"node": banks["children"][2]["id"]}).json()
    dcrb_ports = {child.get("port"): child for child in dcrb["children"] if child["role"] != "roc_stream_info"}
    assert dcrb_ports[4]["role"] == "payload_words" and dcrb_ports[4]["module"] == "DCRB" and not dcrb_ports[4]["has_children"]
    assert client.get(f"/api/files/{file_id}/structure", params={"node": dcrb_ports[4]["id"]}).json()["children"] == []

    paged = client.get(f"/api/files/{file_id}/structure", params={"node": by_port[1]["id"], "from": 14, "count": 5}).json()
    assert [hit["index"] for hit in paged["children"]] == [14, 15] and paged["total"] == 16

    assert client.get(f"/api/files/{file_id}/structure", params={"node": "r9"}).status_code == 404
    assert client.get(f"/api/files/{file_id}/structure", params={"node": "r1/e0/b0"}).status_code == 404
    assert client.get(f"/api/files/{file_id}/structure", params={"node": "r1/e0/zz"}).status_code == 400
    assert client.get(f"/api/files/{file_id}/structure", params={"node": "t"}).json()["node"]["is_trailer"]


def fs_hit_word(channel, time_raw, charge):
    return (time_raw << fs.HIT_TIME_SHIFT) | (channel << fs.HIT_CHANNEL_SHIFT) | charge


def test_words_are_hex_and_capped(client, served_root):
    file_id, _ = open_and_wait(client, served_root["synthetic"])
    head = client.get(f"/api/files/{file_id}/words", params={"offset": 0, "count": 8}).json()
    assert head["words"][0] == "4556494f" and head["words"][7] == "c0da0100" and head["count"] == 8
    capped = client.get(f"/api/files/{file_id}/words", params={"offset": 0, "count": 100000}).json()
    size = (served_root["root"] / served_root["synthetic"]).stat().st_size
    assert capped["count"] == min(4096, size // 4) and capped["max_count"] == 4096
    assert client.get(f"/api/files/{file_id}/words", params={"offset": 2, "count": 1}).status_code == 400
    assert client.get(f"/api/files/{file_id}/words", params={"offset": size + 4, "count": 1}).status_code == 404
    tail = client.get(f"/api/files/{file_id}/words", params={"offset": size - 8, "count": 10}).json()
    assert tail["count"] == 2


@pytest.mark.parametrize("name,total_field", [("hits_per_frame", "n_frames"), ("rate", "n_hits"), ("occupancy", "n_hits"),
                                              ("charge", "n_hits"), ("time_in_frame", "n_hits")])
def test_histogram_sums_equal_totals(client, served_root, name, total_field):
    file_id, _ = open_and_wait(client, served_root["synthetic"])
    totals = client.get(f"/api/files/{file_id}/summary").json()["totals"]
    hist = client.get(f"/api/files/{file_id}/hist/{name}").json()
    assert int(np.sum(hist["counts"])) == totals[total_field]
    edges_key = "edges" if "edges" in hist else "edges_ns"
    if name != "occupancy":
        assert len(hist[edges_key]) == len(hist["counts"]) + 1
    else:
        assert np.asarray(hist["counts"]).shape == (2, 5, 16)


def test_histogram_selection(client, served_root):
    file_id, _ = open_and_wait(client, served_root["synthetic"])
    roc_7 = client.get(f"/api/files/{file_id}/hist/charge", params={"roc": 7}).json()
    assert sum(roc_7["counts"]) == served_root["expected_hits"]
    port_3 = client.get(f"/api/files/{file_id}/hist/charge", params={"roc": 7, "port": 3}).json()
    assert sum(port_3["counts"]) == sum(range(1, 13)) and port_3["counts"][-1] == sum(range(1, 13))
    channel_3 = client.get(f"/api/files/{file_id}/hist/time_in_frame", params={"roc": 7, "port": 3, "ch": 3}).json()
    assert sum(channel_3["counts"]) == sum(range(1, 13))
    assert sum(client.get(f"/api/files/{file_id}/hist/time_in_frame", params={"roc": 7, "port": 3, "ch": 4}).json()["counts"]) == 0
    rate = client.get(f"/api/files/{file_id}/hist/rate", params={"roc": 7, "port": 1, "ch": 0}).json()
    assert sum(rate["counts"]) == 12 and len(rate["rate_hz"]) == len(rate["counts"]) == len(rate["frames_per_bin"])
    occupancy = client.get(f"/api/files/{file_id}/hist/occupancy", params={"roc": 7, "port": 1}).json()
    assert occupancy["counts"] == [12] * 16
    assert client.get(f"/api/files/{file_id}/hist/charge", params={"roc": 8}).status_code == 404
    assert client.get(f"/api/files/{file_id}/hist/charge", params={"roc": 7, "port": 2}).status_code == 404
    assert client.get(f"/api/files/{file_id}/hist/charge", params={"port": 1}).status_code == 400
    assert client.get(f"/api/files/{file_id}/hist/charge", params={"roc": 7, "port": 1, "ch": 16}).status_code == 404
    assert client.get(f"/api/files/{file_id}/hist/nonsense").status_code == 404


def test_second_open_is_a_cache_hit(client, served_root, monkeypatch):
    calls = []
    real_compute = stream_stats.compute_stream_stats

    def counting_compute(path, **kwargs):
        calls.append(path)
        return real_compute(path, **kwargs)

    monkeypatch.setattr(jobs, "compute_stream_stats", counting_compute)
    file_id, _ = open_and_wait(client, served_root["synthetic"])
    assert len(calls) == 1
    cached_files = list(served_root["cache_dir"].glob("*.npz"))
    assert len(cached_files) == 1 and cached_files[0].stem.startswith(file_id)  # file_id is the cache key's prefix

    again = client.post("/api/open", json={"path": served_root["synthetic"]}).json()
    assert again["file_id"] == file_id and again["state"] == "done" and len(calls) == 1
    assert client.get(f"/api/files/{file_id}/summary").json()["totals"]["n_hits"] == served_root["expected_hits"]

    # A fresh server (same cache dir) loads the result instead of recomputing.
    from conftest import TOKEN
    from fastapi.testclient import TestClient
    from pyevio.web.app import create_app
    second = TestClient(create_app(served_root["root"], TOKEN, served_root["cache_dir"], stats_workers=1),
                        base_url="http://localhost", headers={"X-Token": TOKEN})
    opened = second.post("/api/open", json={"path": served_root["synthetic"]}).json()
    assert opened["state"] == "done" and opened["cached"] and len(calls) == 1
    assert second.get(f"/api/files/{file_id}/summary").json()["stats"]["cached"] is True


def test_stale_cache_entry_is_recomputed_in_place(client, served_root, monkeypatch):
    """The key leaves the stats version out, so an old entry is overwritten, never orphaned."""
    from pyevio.web.cache import StatsCache, cache_key

    path = served_root["root"] / served_root["synthetic"]
    key = cache_key(path.resolve())
    stale = stream_stats.compute_stream_stats(str(path), workers=1)
    stale.version = "1"
    StatsCache(served_root["cache_dir"]).save(key, stale)
    assert StatsCache(served_root["cache_dir"]).load(key) is None  # a miss, not an exception

    calls = []
    real_compute = stream_stats.compute_stream_stats
    monkeypatch.setattr(jobs, "compute_stream_stats", lambda p, **kw: calls.append(p) or real_compute(p, **kw))
    file_id, _ = open_and_wait(client, served_root["synthetic"])
    assert key.startswith(file_id) and len(calls) == 1
    cached_files = list(served_root["cache_dir"].glob("*.npz"))
    assert len(cached_files) == 1 and stream_stats.StreamStats.load(str(cached_files[0])).version == stream_stats.STATS_VERSION


def test_opening_another_file_cancels_the_running_job(client, served_root, monkeypatch):
    def blocking_compute(path, workers=None, progress=None, cancel=None, **_):
        """Blocks on the synthetic file until cancelled; any other file computes for real."""
        if not path.endswith("synthetic.evio"):
            return stream_stats.compute_stream_stats(path, workers=1)
        progress(0, 100)
        deadline = time.monotonic() + 10
        while not cancel():
            assert time.monotonic() < deadline, "cancel flag never set"
            time.sleep(0.005)
        raise stream_stats.StatsCancelled("test")

    monkeypatch.setattr(jobs, "compute_stream_stats", blocking_compute)
    first = client.post("/api/open", json={"path": served_root["synthetic"]}).json()
    assert first["state"] in ("queued", "running")

    other_path = served_root["root"] / "runs" / "other.evio"
    from test_stream_stats import write_synthetic_file
    write_synthetic_file(other_path, n_records=1)
    second = client.post("/api/open", json={"path": "runs/other.evio"}).json()
    assert second["file_id"] != first["file_id"]

    assert wait_done(client, first["file_id"])["state"] == "cancelled"
    assert wait_done(client, second["file_id"])["state"] == "done"
    reopened = client.post("/api/open", json={"path": served_root["synthetic"]}).json()
    assert reopened["state"] in ("queued", "running")  # a cancelled file gets a new job on reopen


def test_errors_share_one_json_shape(client, served_root):
    for response in (client.post("/api/open", json={}), client.get("/api/files/x/records", params={"from": "abc"}),
                     client.post("/api/open", json={"path": "runs/missing.evio"})):
        assert response.status_code in (404, 422) and list(response.json()) == ["error"], response.text


def test_unwritable_cache_dir_still_finishes(served_root, tmp_path):
    from conftest import TOKEN
    from fastapi.testclient import TestClient
    from pyevio.web.app import create_app
    blocked = tmp_path / "blocked.file"
    blocked.write_text("a file where the cache folder should be")
    client = TestClient(create_app(served_root["root"], TOKEN, blocked, stats_workers=1), base_url="http://localhost",
                        headers={"X-Token": TOKEN})
    file_id, _ = open_and_wait(client, served_root["synthetic"])
    assert client.get(f"/api/files/{file_id}/summary").json()["totals"]["n_hits"] == served_root["expected_hits"]


def test_open_rejects_unreadable_files(client, served_root):
    (served_root["root"] / "runs" / "garbage.evio").write_bytes(b"\0" * 64)
    response = client.post("/api/open", json={"path": "runs/garbage.evio"})
    assert response.status_code == 400 and "EVIO" in response.json()["error"]
    assert client.post("/api/open", json={"path": "runs/missing.evio"}).status_code == 404
    assert client.get("/api/files/nosuchid/progress").status_code == 404


def test_golden_file_opens_with_no_frames(client, served_root):
    if not served_root["golden"]:
        pytest.skip("golden set absent")
    file_id, _ = open_and_wait(client, served_root["golden"])
    summary = client.get(f"/api/files/{file_id}/summary").json()
    assert summary["totals"]["n_frames"] == 0 and summary["totals"]["n_hits"] == 0
    assert summary["totals"]["hit_rate_hz"] == "n/a" and summary["totals"]["hits_per_frame"] == "n/a"
    assert summary["records"]["events"] == summary["records"]["events_found"] > 0
    root = client.get(f"/api/files/{file_id}/structure").json()
    events = client.get(f"/api/files/{file_id}/structure", params={"node": root["children"][0]["id"]}).json()
    assert events["total"] == root["children"][0]["event_count"]
    banks = client.get(f"/api/files/{file_id}/structure", params={"node": events["children"][0]["id"]}).json()
    assert banks["node"]["kind"] == "event" and "children" in banks
    hist = client.get(f"/api/files/{file_id}/hist/occupancy").json()
    assert hist["counts"] == [] and hist["rocs"] == []


def test_compressed_golden_file_events_come_from_the_record_buffer(client, served_root):
    """Events of an LZ4 record live in the decompressed buffer; the tree and /words?record= read it."""
    if not served_root["golden_lz4"]:
        pytest.skip("golden set absent")
    file_id, _ = open_and_wait(client, served_root["golden_lz4"])
    summary = client.get(f"/api/files/{file_id}/summary").json()
    assert summary["records"]["compressed"] >= 1 and summary["records"]["events_found"] == summary["records"]["events"] > 0
    root = client.get(f"/api/files/{file_id}/structure").json()
    record = next(row for row in root["children"] if row["kind"] == "record" and row["compression_type"] != 0)
    events = client.get(f"/api/files/{file_id}/structure", params={"node": record["id"]}).json()
    assert events["total"] == record["event_count"] and events["children"][0]["compressed"]
    event = events["children"][0]
    banks = client.get(f"/api/files/{file_id}/structure", params={"node": event["id"]}).json()
    assert banks["node"]["length"] == event["length"]
    # The event's first word is its length word, readable only through the record buffer.
    words = client.get(f"/api/files/{file_id}/words", params={"offset": event["offset"], "count": 2, "record": record["index"]}).json()
    assert int(words["words"][0], 16) == event["length"] and words["record"] == record["index"]
    for child in banks["children"]:
        assert child["structure"] in ("Bank", "Segment", "TagSegment")


def test_truncated_tail_is_a_file_check(client, served_root):
    """A final record cut by EOF keeps its complete events and shows up in the checks with byte counts."""
    whole = (served_root["root"] / served_root["synthetic"]).read_bytes()
    from pyevio import EvioFile
    with EvioFile(str(served_root["root"] / served_root["synthetic"])) as evio_file:
        last = evio_file.get_record(evio_file.record_count - 1)
        cut = last.offset + last.size - 300  # drop the trailer and the tail of the last record
    (served_root["root"] / "runs" / "truncated.evio").write_bytes(whole[:cut])
    file_id, _ = open_and_wait(client, "runs/truncated.evio")
    summary = client.get(f"/api/files/{file_id}/summary").json()
    checks = {row["name"]: row for row in summary["checks"]}
    assert checks["truncated tail"]["status"] == "error" and checks["truncated tail"]["value"] == 300
    assert "complete events kept" in checks["truncated tail"]["detail"]
    assert summary["records"]["truncated"] == 1 and summary["records"]["has_trailer"] is False
    assert checks["trailer"]["status"] == "warn"
    assert summary["scan_checks"][0][0] == "truncated_tail"
    rows = client.get(f"/api/files/{file_id}/records").json()["rows"]
    assert rows[-1]["is_truncated"] and rows[-1]["events_found"] < rows[-1]["event_count"]
    assert summary["totals"]["n_frames"] == summary["records"]["events_found"] - 1  # minus the non-stream event
