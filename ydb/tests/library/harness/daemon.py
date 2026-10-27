# -*- coding: utf-8 -*-
import abc
import contextlib
import logging
import os
import signal
import sys
import subprocess

from yatest.common import process
import six

from ydb.tests.library.common.wait_for import wait_for


logger = logging.getLogger(__name__)


def _extract_stderr_details(stderr_file, max_lines=0):
    if max_lines == 0:
        return []

    if stderr_file is None:
        return []

    result = ["Stderr content:", ""]
    with open(stderr_file, 'r') as r:
        for line in r.readlines():
            if len(result) >= max_lines:
                break
            result.append(line.strip())
    return result


def _extract_stderr_tail(stderr_file, max_lines=0):
    if max_lines <= 0 or stderr_file is None:
        return []

    blocks = []
    newline_count = 0
    with open(stderr_file, 'rb') as log:
        log.seek(0, os.SEEK_END)
        position = log.tell()
        while position > 0 and newline_count <= max_lines:
            size = min(position, 8192)
            position -= size
            log.seek(position)
            block = log.read(size)
            blocks.append(block)
            newline_count += block.count(b'\n')
    lines = b''.join(reversed(blocks)).splitlines()[-max_lines:]
    return ["Stderr tail:", ""] + [line.decode('utf-8', errors='replace') for line in lines]


class DaemonError(RuntimeError):
    def __init__(self, message, stdout, stderr, exit_code, max_stderr_lines=0, stderr_tail=False):
        extract_stderr = _extract_stderr_tail if stderr_tail else _extract_stderr_details
        super(DaemonError, self).__init__(
            '\n'.join(
                [
                    "Daemon failed with message: {message}.".format(message=message),
                    "Process exit_code = {exit_code}.".format(exit_code=exit_code),
                    "Stdout file name: \n{}".format(stdout if stdout is not None else "is not present."),
                    "Stderr file name: \n{}".format(stderr if stderr is not None else "is not present."),
                ]
                + extract_stderr(stderr, max_stderr_lines)
            )
        )


class SeveralDaemonErrors(RuntimeError):
    def __init__(self, exceptions):
        super(SeveralDaemonErrors, self).__init__("\n".join(str(x) for x in exceptions))


class Daemon(object):
    """Local process executed as process in current host"""
    def __init__(
        self,
        command,
        cwd,
        timeout,
        stdout_file="/dev/null",
        stderr_file="/dev/null",
        stderr_on_error_lines=0,
        core_pattern=None,
        stderr_tail=False,
    ):
        self.__cwd = cwd
        self.__timeout = timeout
        self.__command = tuple(command)
        self.__stderr_on_error_lines = stderr_on_error_lines
        self.__stderr_tail = stderr_tail
        self.__daemon = None
        self.killed = False
        self.__core_pattern = core_pattern
        self.logger = logger.getChild(self.__class__.__name__)
        with contextlib.ExitStack() as log_files:
            self.__stdout_file = log_files.enter_context(open(stdout_file, mode='wb'))
            self.__stderr_file = log_files.enter_context(open(stderr_file, mode='wb'))
            self.__log_files = log_files.pop_all()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is not None:
                if self.__daemon is not None:
                    try:
                        self.__daemon.terminate()
                        self.__daemon.wait(check_exit_code=False, timeout=min(self.__timeout, 10))
                    except BaseException:
                        self.logger.exception("Daemon cleanup or diagnostics failed")
                        try:
                            self.__daemon.verify_sanitize_errors()
                        except BaseException:
                            self.logger.exception("Daemon sanitizer diagnostics failed")
                try:
                    self.logger.error("%s", self.failure(str(exc_value)))
                except BaseException:
                    self.logger.exception("Could not read daemon failure details")
        finally:
            try:
                self.__log_files.close()
            except BaseException:
                if exc_type is None:
                    raise
                self.logger.exception("Could not close daemon log files")
        return False

    def failure(self, message):
        return DaemonError(
            message,
            stdout=self.stdout_file_name,
            stderr=self.stderr_file_name,
            exit_code=self.__daemon.exit_code if self.__daemon is not None else None,
            max_stderr_lines=self.__stderr_on_error_lines,
            stderr_tail=self.__stderr_tail,
        )

    @property
    def daemon(self):
        return self.__daemon

    @property
    def stdout_file_name(self):
        if self.__stdout_file is not sys.stdout:
            return os.path.abspath(self.__stdout_file.name)
        else:
            return None

    @property
    def stderr_file_name(self):
        if self.__stderr_file is not sys.stderr:
            return os.path.abspath(self.__stderr_file.name)
        else:
            return None

    def is_alive(self):
        return self.__daemon is not None and self.__daemon.running

    def start(self):
        if self.is_alive():
            return
        stderr_stream = self.__stderr_file
        self.__daemon = process.execute(
            self.__command,
            check_exit_code=False,
            cwd=self.__cwd,
            stdout=self.__stdout_file,
            stderr=stderr_stream,
            wait=False,
            core_pattern=self.__core_pattern,
        )
        wait_for(self.is_alive, self.__timeout)

        if not self.is_alive():
            self.__check_before_fail()
            raise self.failure("Unexpectedly finished on start")

        self.killed = False

        return self

    def __check_before_fail(self):
        self.__daemon.verify_no_coredumps()
        self.__daemon.verify_sanitize_errors()

    @property
    def _acceptable_exit_codes(self):
        return 0, -signal.SIGTERM

    def __check_can_launch_stop(self, stop_type):
        if self.__daemon is None or self.killed:
            return False

        if self.__daemon is not None and self.__daemon.exit_code == 0:
            return False

        if not self.is_alive():
            self.__check_before_fail()
            raise self.failure("Unexpectedly finished before %s" % stop_type)

        return True

    def __check_before_end_stop(self, stop_type):
        if self.is_alive():
            msg = "Cannot {stop_type} daemon cmd = {cmd}".format(cmd=' '.join(self.__command), stop_type=stop_type)
            self.logger.error(msg)
            raise self.failure(msg)

    def stop(self):
        if not self.__check_can_launch_stop("stop"):
            return

        self.__daemon.process.terminate()
        wait_for(lambda: not self.is_alive(), self.__timeout)

        is_killed = False
        if self.is_alive():
            self.__daemon.process.send_signal(signal.SIGKILL)
            wait_for(lambda: not self.is_alive(), self.__timeout)
            is_killed = True
        self.__check_before_end_stop("stop")

        if not is_killed:
            exit_code = self.__daemon.exit_code
            self.__check_before_fail()

            if exit_code not in self._acceptable_exit_codes:
                raise self.failure("Bad exit_code.")
        else:
            self.logger.warning("Exit code is not checked, cos binary was stopped by sigkill")

    def kill(self):
        if not self.__check_can_launch_stop("kill"):
            return

        self.__daemon.process.send_signal(signal.SIGKILL)
        wait_for(lambda: not self.is_alive(), self.__timeout)
        self.killed = True

        self.__check_before_end_stop("kill")


@six.add_metaclass(abc.ABCMeta)
class ExternalNodeDaemon(object):
    """External daemon, executed as process in separate host, managed via ssh"""
    def __init__(self, host, ssh_username):
        self._host = host
        self._ssh_username = ssh_username
        self._ssh_options = [
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "LogLevel=ERROR",
        ]
        self.logger = logger.getChild(self.__class__.__name__)

    @property
    def _username_at_host(self):
        return self._host if self._ssh_username is None else self._ssh_username + '@' + self._host

    @property
    def _artifacts_path(self):
        return "/home/%s/kikimr-dir" % self._ssh_username

    def _path_at_host(self, path):
        return ":".join([self._username_at_host, path])

    def ssh_command(self, command_and_params, raise_on_error=False, executable='ssh'):
        if not isinstance(command_and_params, list):
            command_and_params = [command_and_params]

        args = [executable, "-A"] + self._ssh_options + [self._username_at_host]
        args += command_and_params
        return self._run_in_subprocess(args, raise_on_error)

    def copy_file_or_dir(self, file_or_dir, target_path):
        self.ssh_command(['mkdir -p %s' % self._artifacts_path])

        transit_path = os.path.join(self._artifacts_path, os.path.basename(file_or_dir))

        self.ssh_command(['sudo', 'rm', '-rf', target_path], raise_on_error=True)

        self._run_in_subprocess(
            ["scp"] + self._ssh_options + ['-r', file_or_dir, self._path_at_host(transit_path)], raise_on_error=True
        )

        self.ssh_command(["sudo", "mv", transit_path, target_path], raise_on_error=True)

    @abc.abstractproperty
    def logs_directory(self):
        pass

    def cleanup_logs(self):
        self.ssh_command("sudo dmesg --clear", raise_on_error=True)
        self.ssh_command(
            'sudo rm -rf {}/* && sudo service rsyslog restart'.format(self.logs_directory), raise_on_error=True
        )

    def send_signal(self, signal):
        self.ssh_command(
            "ps aux | grep %d | grep -v daemon | grep -v grep | awk '{ print $2 }' | xargs sudo kill -%d"
            % (
                int(self.ic_port),
                int(signal),
            )
        )

    def kill_process_and_daemon(self):
        self.ssh_command(
            "ps aux | grep daemon | grep %d | grep -v grep | awk '{ print $2 }' | xargs sudo kill -%d"
            % (
                int(self.ic_port),
                int(signal.SIGKILL),
            )
        )
        self.ssh_command(
            "ps aux | grep %d | grep -v grep | awk '{ print $2 }' | xargs sudo kill -%d"
            % (
                int(self.ic_port),
                int(signal.SIGKILL),
            )
        )

    def kill(self):
        self.send_signal(9)

    def _run_in_subprocess(self, command, raise_on_error=False):
        self.logger.info("Executing command = " + str(command))
        try:
            ret_str = subprocess.check_output(command, stderr=subprocess.STDOUT)
            self.logger.info("Command returned stdout + stderr = " + ret_str.decode("utf-8", errors="replace"))
            return ret_str
        except subprocess.CalledProcessError as e:
            if raise_on_error:
                self.logger.exception("Ssh command failed with output = " + e.output.decode("utf-8", errors="replace"))
                raise
            else:
                self.logger.info(
                    "Ssh command failed with output (it was ignored) = " + e.output.decode("utf-8", errors="replace")
                )
