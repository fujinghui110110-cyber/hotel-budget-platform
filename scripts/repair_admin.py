"""Interactive recovery on the server computer; never resets project passwords."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_server import load_environment


def main():
    load_environment()
    if (ROOT / '.runtime/update-maintenance').exists():
        raise RuntimeError('系统正在更新或等待恢复，请先完成系统更新恢复再修复账号。')
    print('管理员登录修复：只修改你选中的管理员密码，不删除项目或预算。')
    print('系统目录：' + str(ROOT))
    subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'migrate', '--noinput'], cwd=ROOT, check=True)
    import django
    django.setup()
    from scripts.admin_access import administrators, provision, reveal
    names = list(administrators().values_list('username', flat=True))
    if not names:
        print('本机数据库尚无管理员，将新建独立管理员。')
        if input('输入 Y 创建管理员，其他输入退出：').strip().upper() != 'Y':
            print('已取消，没有修改账号。')
            return
        result = provision(ROOT)
    else:
        print('本机管理员：' + '、'.join(names))
        if len(names) == 1:
            username = names[0]
        else:
            username = input('请输入要修复的管理员用户名：').strip()
            if username not in names:
                raise ValueError('用户名不在管理员列表中，未修改任何账号。')
        if input('输入 Y 重置管理员 ' + username + ' 的密码，其他输入退出：').strip().upper() != 'Y':
            print('已取消，原密码保持不变。')
            return
        result = provision(ROOT, reset_username=username)
    reveal(result)
    print('请返回浏览器，用新文件中的用户名和密码登录。其他已登录会话可能需要重新登录。')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('账号修复未完成：' + str(exc))
        sys.exit(1)
