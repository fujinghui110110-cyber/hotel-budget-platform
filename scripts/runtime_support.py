"""Small cross-platform primitives shared by local and public supervisors."""
from contextlib import AbstractContextManager
import os
from pathlib import Path
import subprocess
import sys

import psutil


class FileLock(AbstractContextManager):
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+b')
        try:
            if sys.platform == 'win32':
                import msvcrt
                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write(b'0')
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise BlockingIOError('服务控制操作正在运行，请稍后重试') from exc
        return self

    def __exit__(self, *args):
        if self.handle is not None:
            if sys.platform == 'win32':
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
        return False


def pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 2:
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def process_matches(pid, script, marker=None):
    if not pid_alive(pid):
        return False
    try:
        command = psutil.Process(pid).cmdline()
        expected = os.path.normcase(os.path.abspath(script))
        return any(os.path.normcase(os.path.abspath(arg)) == expected for arg in command) and (marker is None or marker in command)
    except (psutil.Error, OSError):
        return False


def terminate_process(pid):
    try:
        psutil.Process(pid).terminate()
    except psutil.NoSuchProcess:
        pass


def process_identity(pid):
    if not pid_alive(pid):
        return None
    try:
        process = psutil.Process(pid)
        return {'pid': pid, 'create_time': process.create_time(),
                'cmdline': process.cmdline(), 'cwd': process.cwd()}
    except psutil.Error:
        return None


def identity_matches(identity):
    if not isinstance(identity, dict):
        return False
    current = process_identity(identity.get('pid'))
    return current is not None and current == identity


def stop_identities(identities, timeout=10):
    for identity in identities:
        if identity_matches(identity):
            stop_process_tree(identity['pid'], timeout=timeout)


def stop_process_tree(pid, timeout=10):
    try:
        parent = psutil.Process(pid)
        # Freeze before taking the snapshot so the supervisor cannot respawn.
        parent.suspend()
    except psutil.NoSuchProcess:
        return
    try:
        processes = parent.children(recursive=True)
        for process in processes:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(processes, timeout=timeout)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        # End the frozen parent before waiting so orphaned zombies can be
        # reaped by the OS rather than waiting for a suspended parent to reap.
        parent.kill()
        _, survivors = psutil.wait_procs([*alive, parent], timeout=timeout)
        if survivors:
            raise RuntimeError('后台服务或子进程未能停止，请检查服务日志后重试。')
    except psutil.NoSuchProcess:
        pass
    except BaseException:
        try:
            parent.resume()
        except psutil.NoSuchProcess:
            pass
        raise


def detached_popen_kwargs():
    if sys.platform == 'win32':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    return {'start_new_session': True}


def wsgi_command(port, public=False):
    if sys.platform == 'win32':
        return [sys.executable, '-m', 'waitress', '--listen=127.0.0.1:' + str(port), '--threads=4', '--channel-timeout=60', '--max-request-body-size=134217728', '--no-clear-untrusted-proxy-headers', 'config.wsgi:application']
    command = [sys.executable, '-m', 'gunicorn', 'config.wsgi:application', '--bind', f'127.0.0.1:{port}', '--workers', '1', '--threads', '4', '--timeout', '60', '--access-logfile', '-']
    if not public and os.getenv('TRUST_PROXY', '0') != '1':
        command += ['--forwarded-allow-ips', '']
    return command
