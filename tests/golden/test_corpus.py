"""Converted SRO corpus: record and event counts per file.

How to run
----------
The corpus holds eight files of 12—16 GB each; walking the record headers of
one takes seconds. The module is marked ``slow`` and runs only when
``PYEVIO_CORPUS=1`` is set::

    PYEVIO_CORPUS=1 uv run pytest tests/golden/test_corpus.py -v

Paths: the manifest ``CORPUS_MANIFEST`` (default
``/home/romanov/dev/evio-optim/space-evio-lib-23/golden_manifest/corpus.yaml``)
names each file by absolute path under ``/data/evio-lib/corpus/``. The
module skips itself when the manifest is absent.

Where the data comes from
-------------------------
evio-optim converted the raw SRO streaming files
(``/data/sro_boyarinov_data_2026/sro_000791.evio.0000N``) to clean EVIO v6
with ``evio_convert_format``, uncompressed and LZ4. Each manifest row
records ``records`` (data records written) and ``event_count`` (complete
events plus salvaged tail events) as counted by the converter.

What one test checks
--------------------
``EvioFile.record_count`` equals ``records``, the record header event-count
sum equals ``event_count``, and no event from ``Record.scan_events``
extends past the end of the file. Rows that fail today carry
``xfail(strict=True)`` with the finding number.
"""

import os

import pytest

yaml = pytest.importorskip("yaml")

from pyevio.evio_file import EvioFile  # noqa: E402

pytestmark = pytest.mark.slow

MANIFEST_PATH = os.environ.get(
    "CORPUS_MANIFEST",
    "/home/romanov/dev/evio-optim/space-evio-lib-23/golden_manifest/corpus.yaml",
)

if os.environ.get("PYEVIO_CORPUS") != "1":
    pytest.skip("set PYEVIO_CORPUS=1 to scan the 15 GB corpus files", allow_module_level=True)

if not os.path.isfile(MANIFEST_PATH):
    pytest.skip(f"corpus manifest absent: {MANIFEST_PATH}", allow_module_level=True)

with open(MANIFEST_PATH) as manifest_file:
    MANIFEST = yaml.safe_load(manifest_file)

# Today every corpus file fails the same way: the trailer
# record is counted as a data record, and its index yields two ghost events
# per data record that reach past EOF (F3). The LZ4 files add F4: the data
# records are walked as uncompressed bytes.
XFAIL_BY_COMPRESSION = {
    "none": "F3: trailer counted as a record; trailer index read as events past EOF",
    "lz4": "F4 (and F3): LZ4 records walked as uncompressed bytes; trailer ghosts",
}


def corpus_params():
    params = []
    for row in MANIFEST["files"]:
        reason = XFAIL_BY_COMPRESSION.get(row["compression"])
        marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
        params.append(pytest.param(row, id=os.path.basename(row["path"]), marks=marks))
    return params


@pytest.mark.parametrize("row", corpus_params())
def test_corpus_file(row):
    path = row["path"]
    if not os.path.isfile(path):
        pytest.skip(f"corpus file absent: {path}")

    file_size = os.path.getsize(path)
    with EvioFile(path) as evio_file:
        record_count = evio_file.record_count
        header_events = evio_file.get_total_event_count()
        scanned_events = 0
        past_eof = 0
        for record_i in range(record_count):
            for offset, length in evio_file.get_record(record_i).scan_events():
                scanned_events += 1
                if offset + length > file_size:
                    past_eof += 1

    # The counts are the report numbers: print them so `-s` or the log shows them.
    print(f"{os.path.basename(path)}: records={record_count} (manifest {row['records']}) "
          f"header_events={header_events} scanned={scanned_events} (manifest {row['event_count']}) "
          f"past_eof={past_eof}")

    assert record_count == row["records"], "record count"
    assert header_events == row["event_count"], "record header event counts"
    assert scanned_events == row["event_count"], "events found by scan_events"
    assert past_eof == 0, f"{past_eof} events past EOF"
