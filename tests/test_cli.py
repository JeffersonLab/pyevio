"""The pyevio command group: error reporting, --version, no-argument help, offset labels (F47, F50, F51, F52)."""
import os

import pytest
from click.testing import CliRunner

import pyevio
from pyevio.cli import cli
from test_record_scanning import make_event, make_record, write_file

GOLDEN_V4_FILE = "/data/evio-lib/golden/cases/cpp/v4_le.evio"

# Every command that opens its file with EvioFile, with the arguments it needs after the
# file; the group converts the EvioFile error for all of them (F47). Absent on purpose:
# ``hex`` dumps raw bytes of any file, ``gui`` is an interactive textual app, ``web`` takes a directory.
FILE_COMMANDS = {
    "info": [],
    "dump": [],
    "record": ["0"],
    "event": ["0"],
    "debug": ["--record", "0"],
    "ana": ["--limit", "1"],
    "stats": ["--workers", "1"],
}


def run(args):
    # catch_exceptions=False: a ValueError that escapes the group fails the test with its traceback;
    # a click error still ends as SystemExit, which the runner records as the exit code.
    return CliRunner().invoke(cli, args, catch_exceptions=False)


@pytest.mark.parametrize("command", sorted(FILE_COMMANDS))
@pytest.mark.skipif(not os.path.exists(GOLDEN_V4_FILE), reason="golden set not present")
def test_unsupported_version_is_a_message(command):
    """An EVIO v4 file ends every command with the version message and exit code 1 (F47)."""
    result = run([command, GOLDEN_V4_FILE] + FILE_COMMANDS[command])
    assert result.exit_code == 1
    assert "Error: EVIO version 4 file is not supported" in result.stderr
    assert "Traceback" not in result.output


@pytest.mark.parametrize("command", sorted(FILE_COMMANDS))
def test_non_evio_file_is_a_message(tmp_path, command):
    """A file without the EVIO magic number ends every command with the message and exit code 1 (F47)."""
    path = tmp_path / "noise.bin"
    path.write_bytes(bytes(range(256)) * 8)
    result = run([command, str(path)] + FILE_COMMANDS[command])
    assert result.exit_code == 1
    assert "Error: Not an EVIO file" in result.stderr
    assert "Traceback" not in result.output


def test_version_option():
    """``pyevio --version`` prints the package version (F51)."""
    result = run(["--version"])
    assert result.exit_code == 0
    assert pyevio.__version__ in result.output


def test_no_arguments_prints_help():
    """``pyevio`` with no arguments prints the help; the file shortcut is gone (F52)."""
    result = run([])
    assert "Usage: " in result.output
    assert "Commands:" in result.output
    assert "info" in result.output


def test_file_as_first_argument_is_an_unknown_command(tmp_path):
    """``pyevio FILE`` is rejected as an unknown command (F52, Q26 B)."""
    path = write_file(tmp_path / "plain.evio", [make_record(1, [make_event(2)], last=True, with_index=True)])
    result = run([path])
    assert result.exit_code == 2
    assert "No such command" in result.stderr


def compressed_and_plain_files(tmp_path):
    events = [make_event(2), make_event(3)]
    lz4 = write_file(tmp_path / "lz4.evio", [make_record(1, events, last=True, with_index=True, compression=1)])
    plain = write_file(tmp_path / "plain.evio", [make_record(1, events, last=True, with_index=True)])
    return lz4, plain


NOTE = "LZ4 compressed record: offsets below are in the decompressed record buffer"


def test_event_command_labels_offsets_of_a_compressed_record(tmp_path):
    """``pyevio event`` on an LZ4 record says its offsets are buffer offsets; a plain record gets no note (F50)."""
    lz4, plain = compressed_and_plain_files(tmp_path)

    result = run(["event", lz4, "1", "--hexdump"])
    assert result.exit_code == 0, result.output
    assert NOTE in result.output
    # Buffer offset: 2-word index array + event 0 (4 words) → word 6.
    assert "Offset: 0x18[6]" in result.output

    result = run(["event", plain, "1"])
    assert result.exit_code == 0, result.output
    assert NOTE not in result.output
    # File offset: 14-word file header + 14-word record header + 2-word index + event 0 (4 words) → word 34.
    assert "Offset: 0x88[34]" in result.output


def test_debug_command_labels_offsets_of_a_compressed_record(tmp_path):
    """``pyevio debug`` prints the note once per record (F50)."""
    lz4, plain = compressed_and_plain_files(tmp_path)

    result = run(["debug", lz4, "--record", "0"])
    assert result.exit_code == 0, result.output
    assert result.output.count(NOTE) == 1

    result = run(["debug", plain, "--record", "0"])
    assert result.exit_code == 0, result.output
    assert NOTE not in result.output


def test_event_command_names_the_record_of_a_global_index(tmp_path):
    """``pyevio event N`` by global index prints the record the event lives in (F53)."""
    _, plain = compressed_and_plain_files(tmp_path)
    result = run(["event", plain, "1", "--hexdump"])
    assert result.exit_code == 0, result.output
    assert "Record #0 Event #1" in result.output
    assert "Record #None" not in result.output
    assert "Record #0 Event rel#1, Length: 20 bytes\n" in result.output


def test_dump_record_out_of_range_is_a_parameter_error(tmp_path):
    """``pyevio dump --record R`` past the last record exits 2 with the range message (F54)."""
    _, plain = compressed_and_plain_files(tmp_path)
    result = run(["dump", plain, "--record", "7"])
    assert result.exit_code == 2
    assert "Record 7 out of range (0-0)" in result.stderr

    result = run(["dump", plain, "--record", "0", "2"])
    assert result.exit_code == 0, result.output
