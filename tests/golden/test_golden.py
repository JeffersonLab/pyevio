"""Golden-set harness: pyevio against the evio-optim golden manifest.

How to run
----------
The golden set lives outside the repository. The module skips itself when
either path is absent:

- manifest: ``GOLDEN_MANIFEST`` (default
  ``/home/romanov/dev/evio-optim/space-evio-lib-23/golden_manifest/manifest.yaml``)
- files: ``golden_root`` from the manifest (``/data/evio-lib/golden``)

Run only this module::

    uv run pytest tests/golden/test_golden.py -v

The sibling module ``test_corpus.py`` covers the converted SRO corpus
(15 GB files); it runs only with ``PYEVIO_CORPUS=1``.

Where the data comes from
-------------------------
The golden set was written by the evio C++ and Java libraries at the
``lib_git_sha`` recorded in the manifest (generators
``evio/src/utils/cpp/golden_gen.cpp``, ``GoldenGen.java``, ``gen_v13.py``).
Each manifest row carries ``path``, ``case``, ``description``, ``sha256``,
``event_count`` and content digests. One test runs per row; the test id is
the path below ``cases/`` because the same ``case`` name exists for both
writers.

What one test checks
--------------------
1. The file is present and its sha256 matches the manifest (a mismatch is a
   broken reference: stop and ask, never regenerate over it).
2. ``.dump`` rows are record streams without a file header: skipped.
3. v4 and v1—3 rows: ``EvioFile`` raises ``ValueError`` whose message names
   the detected version and points at the evio tools (the I9 contract, see
   ``assert_unsupported_version_message``).
4. v6 rows: the header event-count sum equals the manifest count, the
   events found by ``Record.scan_events`` number the same, and no event
   extends past the end of the file.

Rows that fail today carry ``xfail(strict=True)`` with the finding number
from the findings ledger, so a fix flips the test loudly.
"""

import hashlib
import os
import re

import pytest

yaml = pytest.importorskip("yaml")

from pyevio.evio_file import EvioFile  # noqa: E402

MANIFEST_PATH = os.environ.get(
    "GOLDEN_MANIFEST",
    "/home/romanov/dev/evio-optim/space-evio-lib-23/golden_manifest/manifest.yaml",
)

if not os.path.isfile(MANIFEST_PATH):
    pytest.skip(f"golden manifest absent: {MANIFEST_PATH}", allow_module_level=True)

with open(MANIFEST_PATH) as manifest_file:
    MANIFEST = yaml.safe_load(manifest_file)

GOLDEN_ROOT = MANIFEST["golden_root"]

if not os.path.isdir(GOLDEN_ROOT):
    pytest.skip(f"golden root absent: {GOLDEN_ROOT}", allow_module_level=True)


# EVIO version each legacy row carries in word 5 of its first block header.
# These rows must fail with the I9 message instead of being read.
LEGACY_VERSION = {
    "v4_le.evio": 4,
    "v4_be.evio": 4,
    "v4dict_le.evio": 4,
    "v13_legacy.evio": 3,
}

# Failures by case name, decided by running every row with pyevio. Empty
# since PHASE1 fixed F3 (trailer index read as events), F4 (compressed
# records), F9 (version message for legacy files) and F39 (HIPO file type
# id). A new entry carries the finding number.
XFAIL_BY_CASE = {}


def manifest_params():
    params = []
    for row in MANIFEST["files"]:
        test_id = row["path"].split("cases/", 1)[-1]
        marks = []
        if row["path"].endswith(".dump"):
            marks.append(pytest.mark.skip(reason="buffer dump, no file header"))
        elif row["case"] in XFAIL_BY_CASE:
            marks.append(pytest.mark.xfail(strict=True, reason=XFAIL_BY_CASE[row["case"]]))
        params.append(pytest.param(row, id=test_id, marks=marks))
    return params


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_unsupported_version_message(message, version):
    """The I9 contract for files pyevio does not read.

    The message names the detected EVIO version ("version 4") and tells the
    reader where to go: an evio tool (evio2xml, eviocopy) or the evio
    library.
    """
    assert re.search(rf"(?i)\bversion\s*{version}\b", message), \
        f"message does not name EVIO version {version}: {message!r}"
    assert re.search(r"(?i)evio2xml|eviocopy|evio (tools?|librar|utilit)", message), \
        f"message does not point at the evio tools: {message!r}"


@pytest.mark.parametrize("row", manifest_params())
def test_golden_file(row):
    path = os.path.join(GOLDEN_ROOT, row["path"])
    if not os.path.isfile(path):
        pytest.fail(f"golden file missing: {path} (regenerate per the decision table)")
    assert sha256_of(path) == row["sha256"], \
        f"sha256 mismatch for {path}: the golden reference is broken, stop and ask"

    if row["case"] in LEGACY_VERSION:
        with pytest.raises(ValueError) as raised:
            EvioFile(path)
        assert_unsupported_version_message(str(raised.value), LEGACY_VERSION[row["case"]])
        return

    file_size = os.path.getsize(path)
    with EvioFile(path) as evio_file:
        header_events = evio_file.get_total_event_count()
        scanned_events = 0
        past_end = []
        errors = []
        for record_i in range(evio_file.record_count):
            record = evio_file.get_record(record_i)
            # Event offsets are into record.buffer: the file for a plain
            # record, the decompressed bytes for a compressed one.
            buffer_end = file_size if record.buffer is evio_file.mm else len(record.buffer)
            for offset, length in record.scan_events():
                scanned_events += 1
                if offset + length > buffer_end:
                    past_end.append((record_i, offset, length))
            errors += record.errors

    assert header_events == row["event_count"], "record header event counts"
    assert scanned_events == row["event_count"], "events found by scan_events"
    assert past_end == [], f"events past their buffer end: {past_end[:5]}"
    assert errors == [], f"record errors: {errors[:5]}"
    assert evio_file.checks == [], f"file checks: {evio_file.checks}"
