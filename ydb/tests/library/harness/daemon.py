# -*- coding: utf-8 -*-
import abc
import contextlib
import logging
import os
import signal
import sys
import subprocess
import time

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
        aux_file=None,
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
        self.__stdout_file_name = stdout_file
        self.__stderr_file_name = stderr_file
        self.__aux_file_name = aux_file
        self.__stdout_file = None
        self.__stderr_file = None
        self.__aux_file = None

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
                self.__close_output_files()
            except BaseException:
                if exc_type is None:
                    raise
                self.logger.exception("Could not close daemon output files")
        return False

    def failure(self, message):
        return DaemonError(
            message,
            stdout=self.stdout_file_name,
            stderr=self.stderr_file_name,
            exit_code=self.__daemon.exit_code if self.__daemon is not None else None,
            max_stderr_lines=self.__stderr_on_error_lines if os.path.exists(self.stderr_file_name) else 0,
            stderr_tail=self.__stderr_tail,
        )

    def update_command(self, new_command):
        new_command_tuple = tuple(new_command)
        if self.__command != new_command_tuple:
            self.__command = new_command_tuple

    def update_aux_file(self, new_aux_file_name):
        self.__aux_file_name = new_aux_file_name

        if self.__aux_file is not None:
            self.__aux_file.close()
            self.__aux_file = open(self.__aux_file_name, mode='w+b')

    def __open_output_files(self):
        self.__close_output_files()
        with contextlib.ExitStack() as files:
            stdout = files.enter_context(open(self.__stdout_file_name, mode='ab'))
            stderr = files.enter_context(open(self.__stderr_file_name, mode='ab'))
            aux = files.enter_context(open(self.__aux_file_name, mode='w+b')) if self.__aux_file_name is not None else None
            files.pop_all()
        self.__stdout_file = stdout
        self.__stderr_file = stderr
        self.__aux_file = aux

    def __close_output_files(self):
        if self.__stdout_file is not None:
            self.__stdout_file.close()
            self.__stdout_file = None

        if self.__stderr_file is not None:
            self.__stderr_file.close()
            self.__stderr_file = None

        if self.__aux_file is not None:
            self.__aux_file.close()
            self.__aux_file = None

    @property
    def daemon(self):
        return self.__daemon

    @property
    def stdout_file_name(self):
        if self.__stdout_file is not sys.stdout:
            return os.path.abspath(self.__stdout_file_name)
        else:
            return None

    @property
    def stderr_file_name(self):
        if self.__stderr_file is not sys.stderr:
            return os.path.abspath(self.__stderr_file_name)
        else:
            return None

    def is_alive(self):
        return self.__daemon is not None and self.__daemon.running

    def start(self):
        if self.is_alive():
            return
        self.__open_output_files()
        try:
            self.__daemon = process.execute(
                self.__command,
                check_exit_code=False,
                cwd=self.__cwd,
                stdout=self.__stdout_file,
                stderr=self.__stderr_file,
                wait=False,
                core_pattern=self.__core_pattern,
            )
        except BaseException:
            self.__close_output_files()
            raise
        wait_for(self.is_alive, self.__timeout)

        if not self.is_alive():
            self.__check_before_fail()
            self.__close_output_files()
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
            self.__close_output_files()
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

        stop_start_at = time.time()
        self.__daemon.process.terminate()
        wait_for(lambda: not self.is_alive(), self.__timeout, step_seconds=0.001, max_step_seconds=1)

        is_killed = False
        if self.is_alive():
            self.logger.info("Force stop daemon, still alive after %s s" % self.__timeout)
            self.__daemon.process.send_signal(signal.SIGKILL)
            wait_for(lambda: not self.is_alive(), self.__timeout)
            is_killed = True

        self.logger.info("Daemon stopped in %s s" % (time.time() - stop_start_at))
        self.__check_before_end_stop("stop")
        self.__close_output_files()

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
        self.__close_output_files()


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

        self.logger.info("SSH command: %s", ' '.join(args))
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
        self.ssh_command(
            'sudo rm -rf {}/* && sudo service rsyslog restart'.format(self.logs_directory), raise_on_error=True
        )

    def send_signal(self, signal):
        # First, let's see what processes we're trying to find
        ps_command = "ps aux | grep %d | grep -v daemon | grep -v grep" % int(self.ic_port)
        self.logger.info("Looking for processes with command: %s", ps_command)

        # Get the list of processes first
        try:
            ps_output = self.ssh_command(ps_command, raise_on_error=False)
            self.logger.info("Process list output: %s", ps_output.decode("utf-8", errors="replace") if ps_output else "No output")
        except Exception as e:
            self.logger.error("Failed to get process list: %s", str(e))

        # Now execute the kill command
        kill_command = "ps aux | grep %d | grep -v daemon | grep -v grep | awk '{ print $2 }' | xargs -r sudo kill -%d" % (
            int(self.ic_port),
            int(signal),
        )
        self.logger.info("Executing kill command: %s", kill_command)

        try:
            result = self.ssh_command(kill_command, raise_on_error=False)
            self.logger.info("Kill command result: %s", result.decode("utf-8", errors="replace") if result else "No output")
        except Exception as e:
            self.logger.error("Kill command failed: %s", str(e))

    def kill_process_and_daemon(self):
        self.logger.info("Starting kill_process_and_daemon for port %d", int(self.ic_port))

        # Kill daemon processes first
        daemon_command = "ps aux | grep daemon | grep %d | grep -v grep | awk '{ print $2 }' | xargs -r sudo kill -%d" % (
            int(self.ic_port),
            int(signal.SIGKILL),
        )
        self.logger.info("Executing daemon kill command: %s", daemon_command)

        try:
            daemon_result = self.ssh_command(daemon_command, raise_on_error=False)
            self.logger.info("Daemon kill result: %s", daemon_result.decode("utf-8", errors="replace") if daemon_result else "No output")
        except Exception as e:
            self.logger.error("Daemon kill command failed: %s", str(e))

        # Kill regular processes
        process_command = "ps aux | grep %d | grep -v grep | awk '{ print $2 }' | xargs -r sudo kill -%d" % (
            int(self.ic_port),
            int(signal.SIGKILL),
        )
        self.logger.info("Executing process kill command: %s", process_command)

        try:
            process_result = self.ssh_command(process_command, raise_on_error=False)
            self.logger.info("Process kill result: %s", process_result.decode("utf-8", errors="replace") if process_result else "No output")
        except Exception as e:
            self.logger.error("Process kill command failed: %s", str(e))

    def kill(self):
        self.send_signal(9)

    def _run_in_subprocess(self, command, raise_on_error=False):
        self.logger.info("Executing command = %s", str(command))
        try:
            ret_str = subprocess.check_output(command, stderr=subprocess.STDOUT)
            output_str = ret_str.decode("utf-8", errors="replace")
            self.logger.info("Command succeeded with output = %s", output_str)
            return ret_str
        except subprocess.CalledProcessError as e:
            output_str = e.output.decode("utf-8", errors="replace") if e.output else "No output"
            self.logger.info("Command failed with exit code %d and output = %s", e.returncode, output_str)
            if raise_on_error:
                self.logger.exception("Ssh command failed with output = " + output_str)
                raise
            else:
                self.logger.info("Ssh command failed with output (it was ignored) = " + output_str)
