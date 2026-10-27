import builtins
import contextlib
from types import SimpleNamespace

import pytest
import yatest.common as common

from ydb.tests.library.harness import daemon


def _daemon(tmp_path, command=None, **kwargs):
    return daemon.Daemon(
        command or ["unused-client"], cwd=str(tmp_path), timeout=2,
        stdout_file=str(tmp_path / "stdout"), stderr_file=str(tmp_path / "stderr"), **kwargs)


@pytest.mark.parametrize("fail_inside", [False, True])
def test_context_closes_logs_without_start(monkeypatch, tmp_path, fail_inside):
    streams = []

    def open_log(*args, **kwargs):
        stream = builtins.open(*args, **kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr(daemon, "open", open_log, raising=False)
    expected = pytest.raises(RuntimeError, match="context failed") if fail_inside else contextlib.nullcontext()
    with expected:
        with _daemon(tmp_path) as instance:
            assert instance.daemon is None
            assert not any(stream.closed for stream in streams)
            if fail_inside:
                raise RuntimeError("context failed")

    assert streams == []


@pytest.mark.parametrize("failed_stream", ["stderr", "aux"])
def test_start_closes_open_streams_on_partial_failure(monkeypatch, tmp_path, failed_stream):
    streams = []

    def open_log(*args, **kwargs):
        stream = builtins.open(*args, **kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr(daemon, "open", open_log, raising=False)
    stderr = str(tmp_path) if failed_stream == "stderr" else str(tmp_path / "stderr")
    aux = str(tmp_path) if failed_stream == "aux" else None
    instance = daemon.Daemon(["unused-client"], cwd=str(tmp_path), timeout=1,
                             stdout_file=str(tmp_path / "stdout"), stderr_file=stderr, aux_file=aux)
    with pytest.raises(IsADirectoryError):
        instance.start()

    assert len(streams) == (1 if failed_stream == "stderr" else 2)
    assert all(stream.closed for stream in streams)


def test_restart_appends_logs_and_reopens_aux_file(tmp_path):
    instance = _daemon(tmp_path, [common.python_path(), "-c", "import time; time.sleep(60)"],
                       aux_file=str(tmp_path / "aux"))
    execution = None
    try:
        instance.start()
        execution = instance.daemon
        first_stdout = instance._Daemon__stdout_file
        first_aux = instance._Daemon__aux_file
        first_stdout.write(b"first\n")
        first_stdout.flush()
        first_aux.write(b"old")
        first_aux.flush()
        instance.kill()
        assert first_stdout.closed and first_aux.closed
        instance.start()
        execution = instance.daemon
        second_stdout = instance._Daemon__stdout_file
        second_aux = instance._Daemon__aux_file
        second_stdout.write(b"second\n")
        second_stdout.flush()
        assert (tmp_path / "stdout").read_bytes() == b"first\nsecond\n"
        assert (tmp_path / "aux").read_bytes() == b""
        instance.kill()
        assert second_stdout.closed and second_aux.closed
    finally:
        if execution is not None:
            if execution.process.poll() is None:
                execution.process.kill()
            execution.process.wait()


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_context_stops_real_process_and_preserves_error(monkeypatch, tmp_path, error_type):
    error = error_type("original error")
    streams = []
    execution = None

    def open_log(*args, **kwargs):
        stream = builtins.open(*args, **kwargs)
        streams.append(stream)
        return stream

    def fail_diagnostics(self):
        raise ValueError("diagnostics failed")

    monkeypatch.setattr(daemon, "open", open_log, raising=False)
    monkeypatch.setattr(daemon.process._Execution, "verify_sanitize_errors", fail_diagnostics)
    try:
        with pytest.raises(error_type) as caught:
            with _daemon(tmp_path, [common.python_path(), "-c", "import time; time.sleep(60)"]) as instance:
                instance.start()
                execution = instance.daemon
                raise error

        assert caught.value is error
        assert execution.process.poll() is not None
        assert all(stream.closed for stream in streams)
    finally:
        if execution is not None:
            if execution.process.poll() is None:
                execution.process.kill()
            execution.process.wait()


def test_successful_context_leaves_real_process_running(tmp_path):
    execution = None
    try:
        with _daemon(tmp_path, [common.python_path(), "-c", "import time; time.sleep(60)"]) as instance:
            instance.start()
            execution = instance.daemon
            stdout = instance._Daemon__stdout_file
            stderr = instance._Daemon__stderr_file
        assert execution.process.poll() is None
        assert stdout.closed and stderr.closed
        assert instance.stdout_file_name == str(tmp_path / "stdout")
        assert instance.stderr_file_name == str(tmp_path / "stderr")
    finally:
        if execution is not None:
            if execution.process.poll() is None:
                execution.process.kill()
            execution.process.wait()


def test_failure_includes_message_paths_and_exit_code(tmp_path):
    with _daemon(tmp_path, stderr_on_error_lines=1, stderr_tail=True) as instance:
        assert "Process exit_code = None." in str(instance.failure("before start"))
        with open(instance.stderr_file_name, "wb") as log:
            log.write(b"first\nlast\n")
        error = instance.failure("failed")
        instance._Daemon__daemon = SimpleNamespace(exit_code=7)
        exited_error = instance.failure("exited")
        assert "Daemon failed with message: failed." in str(error)
        assert "Daemon failed with message: exited." in str(exited_error)
        assert "Process exit_code = 7." in str(exited_error)
        assert str(tmp_path / "stdout") in str(error)
        assert str(tmp_path / "stderr") in str(error)
        assert "Stderr tail:\n\nlast" in str(error)


@pytest.mark.parametrize("separator", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing_newline", [False, True])
def test_tail_caps_lines(tmp_path, separator, trailing_newline):
    log = tmp_path / "stderr"
    lines = ["  log line {}".format(index) for index in range(150)]
    log.write_bytes((separator.join(lines) + (separator if trailing_newline else "")).encode("utf-8"))
    assert daemon._extract_stderr_tail(str(log), 100) == ["Stderr tail:", ""] + lines[-100:]


@pytest.mark.parametrize("content", [
    b"", b"only line", b"only line\n", b"\n", b"first\n\n", b"invalid utf-8: \xff\n",
])
def test_tail_short_log(tmp_path, content):
    log = tmp_path / "stderr"
    log.write_bytes(content)
    assert daemon._extract_stderr_tail(str(log), 100) == (
        ["Stderr tail:", ""] + content.decode("utf-8", errors="replace").splitlines())


def test_tail_reads_only_final_blocks(monkeypatch, tmp_path):
    log = tmp_path / "stderr"
    log.write_bytes(b"startup log\n" * 100000 + b"failure at end\n" * 100)
    bytes_read = []

    class TrackedFile:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def __getattr__(self, name):
            return getattr(self.file, name)

        def read(self, size):
            assert 0 < size <= 8192
            result = self.file.read(size)
            bytes_read.append(len(result))
            return result

    monkeypatch.setattr(daemon, "open", lambda *args, **kwargs: TrackedFile(
        builtins.open(*args, **kwargs)), raising=False)
    assert daemon._extract_stderr_tail(str(log), 100) == (
        ["Stderr tail:", ""] + ["failure at end"] * 100)
    assert sum(bytes_read) < log.stat().st_size


def test_tail_reads_long_unicode_line_across_blocks(tmp_path):
    log = tmp_path / "stderr"
    last_line = "ошибка " * 5000
    log.write_bytes(("old line\n" + last_line + "\n").encode("utf-8"))
    assert daemon._extract_stderr_tail(str(log), 1) == ["Stderr tail:", "", last_line]


def test_tail_disabled_without_opening_file():
    assert daemon._extract_stderr_tail("missing-file", 0) == []
    assert daemon._extract_stderr_tail(None, 100) == []


def test_error_keeps_legacy_head_by_default(tmp_path):
    log = tmp_path / "stderr"
    log.write_text("first\nsecond\nlast\n")
    error = daemon.DaemonError("failed", None, str(log), 1, max_stderr_lines=4)
    assert "Stderr content:\n\nfirst\nsecond" in str(error)
    assert "\nlast" not in str(error)
