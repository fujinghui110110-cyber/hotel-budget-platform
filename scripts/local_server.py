import argparse
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

try:
    from scripts.runtime_support import FileLock, process_matches, stop_process_tree, detached_popen_kwargs, wsgi_command, process_identity, stop_identities
except ImportError:
    from runtime_support import FileLock, process_matches, stop_process_tree, detached_popen_kwargs, wsgi_command, process_identity, stop_identities


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.getenv("BUDGET_RUNTIME_ROOT", ROOT / ".runtime"))
STATE = RUNTIME / "server.json"
LOGS = Path(os.getenv("BUDGET_LOG_ROOT", ROOT / "logs"))
LABEL = "com.frank.hotel-budget"


def load_environment():
    global RUNTIME, STATE, LOGS
    # Explicit process variables win, then production configuration, then local defaults.
    for name in (".env.production", ".env"):
        path = Path(os.getenv("BUDGET_INSTALL_ROOT", ROOT)) / name
        if path.exists():
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    raise ValueError(f"{name} 中存在无效环境变量行")
                key, value = line.removeprefix("export ").split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("BUDGET_PROCESS_UPLOAD_INLINE", "0")
    RUNTIME = Path(os.getenv("BUDGET_RUNTIME_ROOT", ROOT / ".runtime"))
    STATE = RUNTIME / "server.json"
    LOGS = Path(os.getenv("BUDGET_LOG_ROOT", ROOT / "logs"))


def health(port):
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/healthz")
        with urllib.request.urlopen(request, timeout=2) as response:
            data = json.load(response)
        return data.get("service") == "hotel-budget" and data.get("database") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def owned(pid):
    return process_matches(pid, Path(__file__).resolve(), "serve")


def stop():
    agent = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if sys.platform == "darwin" and agent.exists():
        config = plistlib.loads(agent.read_bytes())
        if str(ROOT / "scripts/local_server.py") in config.get("ProgramArguments", []):
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if STATE.exists():
        state = json.loads(STATE.read_text())
        pid = state.get("pid")
        if owned(pid):
            if state.get("supervisor_identity") and process_identity(pid) != state["supervisor_identity"]:
                raise RuntimeError("服务进程身份已变化，未停止其他进程。")
            if sys.platform == "win32":
                stop_process_tree(pid)
            else:
                os.kill(pid, signal.SIGTERM)
            for _ in range(200):
                if not owned(pid):
                    break
                time.sleep(0.25)
            if owned(pid):
                raise RuntimeError("服务尚未停止，请查看 logs/server.log")
        identities = state.get("children_identity")
        if identities is None:
            identities = legacy_children(state)
        stop_identities(identities)
        STATE.unlink(missing_ok=True)
    print("本项目后台服务已停止。")


def legacy_children(state):
    identities = []
    for key in ("web_pid", "worker_pid"):
        identity = process_identity(state.get(key))
        if not identity:
            continue
        command = identity['cmdline']
        if Path(identity['cwd']).resolve() != ROOT.resolve():
            raise RuntimeError('旧服务记录的进程身份已变化，未结束其他项目的进程；请检查服务记录。')
        if key == 'worker_pid':
            matches = str(ROOT / 'manage.py') in command and 'budget_worker' in command
        else:
            address = f"127.0.0.1:{state.get('port', 8768)}"
            matches = ('config.wsgi:application' in command and
                       (('gunicorn' in command and address in command) or
                        ('waitress' in command and '--listen=' + address in command)))
        if not matches:
            raise RuntimeError("无法确认旧服务记录的子进程身份，本次未清除服务记录。")
        identities.append(identity)
    return identities


def write_server_state(children, port):
    state = {"pid": os.getpid(), "supervisor_identity": process_identity(os.getpid()), "port": port, "web_pid": children[0].pid, "worker_pid": children[1].pid,
             "children_identity": [identity for child in children
                                   if (identity := process_identity(child.pid))]}
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state), encoding='utf-8')
    temporary.replace(STATE)


def serve(port):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    os.environ["BUDGET_RUNTIME_ROOT"] = str(RUNTIME.resolve())
    with FileLock(RUNTIME / "server.lock"):
        for args in (["check"], ["migrate", "--check", "--noinput"]):
            subprocess.run([sys.executable, str(ROOT / "manage.py"), *args], cwd=ROOT, check=True)
        children = []
        running = True

        def shutdown(signum, frame):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        commands = [
            wsgi_command(port),
            [sys.executable, str(ROOT / "manage.py"), "budget_worker"],
        ]
        handles = [(LOGS / name).open("a", buffering=1) for name in ("web.log", "worker.log")]
        try:
            for command, output in zip(commands, handles):
                children.append(subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT))
            write_server_state(children, port)
            while running:
                for index, child in enumerate(children):
                    if child.poll() is not None:
                        print(f"后台进程 {index} 退出，5秒后重启。", flush=True)
                        time.sleep(5)
                        if running:
                            children[index] = subprocess.Popen(commands[index], cwd=ROOT, stdout=handles[index], stderr=subprocess.STDOUT)
                        write_server_state(children, port)
                time.sleep(1)
        finally:
            for child in children:
                if child.poll() is None:
                    stop_process_tree(child.pid, timeout=10)
                child.wait(timeout=10)
            for output in handles:
                output.close()
            STATE.unlink(missing_ok=True)


def install_autostart(port):
    if sys.platform != "darwin":
        raise RuntimeError("登录后自动运行仅支持 macOS")
    path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = plistlib.loads(path.read_bytes())
        if str(ROOT / "scripts/local_server.py") not in existing.get("ProgramArguments", []):
            raise RuntimeError("同名登录任务属于其他路径，未覆盖")
    config = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(ROOT / "scripts/local_server.py"), "serve", "--port", str(port)],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(LOGS / "server.log"),
        "StandardErrorPath": str(LOGS / "server.log"),
    }
    path.write_bytes(plistlib.dumps(config))
    path.chmod(0o600)
    target = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{target}/{LABEL}"], capture_output=True)
    stop()
    subprocess.run(["launchctl", "bootstrap", target, str(path)], check=True)
    print("已启用登录后自动运行；关闭终端不影响服务。")


def worker_status():
    # Script entrypoints do not normally put the repository root on sys.path.
    sys.path.insert(0, str(ROOT))
    from budgeting.services.worker_heartbeat import read_worker_status
    return read_worker_status(RUNTIME)


def dispatch_active_release():
    sys.path.insert(0, str(ROOT))
    from scripts.bridge_bootstrap import dispatch_active_release as dispatch
    dispatch(ROOT)


def main():
    load_environment()
    dispatch_active_release()
    pointer = RUNTIME / "active-python.json"
    if pointer.exists() and not (Path(os.getenv("BUDGET_INSTALL_ROOT", ROOT)) / "active-release.json").exists():
        selected = Path(json.loads(pointer.read_text(encoding="utf-8"))["path"])
        if not selected.is_file():
            raise RuntimeError("更新运行环境缺失，请按系统更新说明恢复旧版本")
        # Preserve the venv path: resolve() would collapse POSIX Python symlinks.
        if os.path.abspath(selected) != os.path.abspath(sys.executable):
            os.execv(str(selected), [str(selected), str(Path(__file__).resolve()), *sys.argv[1:]])
    load_environment()
    parser = argparse.ArgumentParser(description="预算统筹系统后台服务管理")
    parser.add_argument("action", choices=["start", "serve", "stop", "status", "enable-autostart", "disable-autostart"])
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8768")))
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口须在1024至65535之间")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    if args.action == "serve":
        return serve(args.port)
    if args.action == "enable-autostart":
        return install_autostart(args.port)
    if args.action == "disable-autostart":
        path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        if path.exists():
            if str(ROOT / "scripts/local_server.py") not in plistlib.loads(path.read_bytes()).get("ProgramArguments", []):
                raise RuntimeError("同名登录任务属于其他路径，未停止或删除")
            stop()
            path.unlink()
        return stop()
    if args.action == "stop":
        return stop()
    if args.action == "status":
        print(json.dumps({"web_healthy": health(args.port), "worker": worker_status()}, ensure_ascii=False))
        return 0 if health(args.port) and worker_status()["healthy"] else 1
    if not health(args.port):
        import socket
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", args.port)) == 0:
                raise RuntimeError(f"端口 {args.port} 已被其他服务占用，未停止该服务")
        with (LOGS / "server.log").open("a") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", "--port", str(args.port)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **detached_popen_kwargs())
        for _ in range(60):
            if health(args.port):
                break
            if process.poll() is not None:
                raise RuntimeError("服务启动失败，请查看 logs/server.log")
            time.sleep(0.5)
        else:
            raise RuntimeError("服务尚未就绪，请查看 logs/server.log 和 logs/web.log")
    url = f"http://127.0.0.1:{args.port}/"
    print(f"预算系统已在后台运行：{url}\n可以关闭此终端。数据保存在本电脑。")
    if args.open:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"启动管理失败：{exc}", file=sys.stderr)
        sys.exit(1)
