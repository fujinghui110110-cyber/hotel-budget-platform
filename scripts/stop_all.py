"""Stop public access before shutting down the local budget server."""
import argparse
import socket
import sys
import time

import local_server
import public_access


def public_port_open():
    with socket.socket() as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(('127.0.0.1', public_access.PORT)) == 0


def stop_all(timeout=300):
    deadline = time.monotonic() + timeout
    requested = False
    print('正在关闭公网访问；如正在生成链接，会等待该操作结束后再关闭。', flush=True)
    while time.monotonic() < deadline:
        current = public_access.status()
        if current.get('busy'):
            time.sleep(0.5)
            continue
        if requested and current.get('status') == 'stopped' and not current.get('running'):
            if public_port_open():
                raise RuntimeError('公网回源端口仍被占用，本机服务未关闭。请检查 logs/public-supervisor.log 后重试。')
            local_server.stop()
            print('公网访问和本机预算系统均已停止。', flush=True)
            return
        if requested and current.get('status') == 'failed':
            raise RuntimeError('公网停止失败，本机服务未关闭：' + (current.get('error') or '请检查 logs/public-controller.log'))
        result = public_access.request_action('stop')
        # A concurrent restart can win the controller lock. Do not treat it as
        # our stop request; wait for it to finish, then explicitly request stop.
        requested = result.get('status') in {'stopping', 'stopped', 'failed'}
        time.sleep(0.5)
    raise RuntimeError(f'等待公网停止超过 {timeout} 秒，本机服务未关闭。请查看公网访问页面或 logs/public-controller.log 后重试。')


def main():
    parser = argparse.ArgumentParser(description='先停止公网访问，再停止本机预算系统')
    parser.add_argument('--timeout', type=int, default=300, help='最多等待秒数，默认300')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error('等待时间须在1至600秒之间')
    local_server.load_environment()
    stop_all(args.timeout)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'停止失败：{exc}', file=sys.stderr)
        sys.exit(1)
