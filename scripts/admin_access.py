"""Create or recover a server-local administrator without importing business data."""
from pathlib import Path
import os
import secrets
import subprocess
import sys
from datetime import datetime


def administrators():
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    return get_user_model().objects.filter(Q(is_superuser=True) | Q(role='ADMIN')).order_by('username')


def write_credentials(root, username, password):
    root = Path(root)
    # Unique name keeps previous account records intact. Never add this directory to release packages.
    folder = root / '本机账号信息'
    folder.mkdir(exist_ok=True, mode=0o700)
    path = folder / ('管理员登录信息-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3) + '.txt')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8-sig') as handle:
            handle.write('酒店预算统筹系统 · 本机管理员登录信息\n\n'
                         '登录网址：http://127.0.0.1:8768\n'
                         f'用户名：{username}\n密码：{password}\n\n'
                         '请完整复制用户名和密码，不要复制前面的“用户名：”“密码：”。\n'
                         '这是本台电脑独立生成的账号，其他电脑的旧密码不能用于这里。\n'
                         '请妥善保存此文件，不要发给项目人员，不要上传 GitHub。\n'
                         '如果后来修改过密码，以你修改后的密码为准。\n'
                         '忘记密码时，在系统文件夹双击“修复管理员登录-Windows.bat”。\n')
        if sys.platform == 'win32':
            sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True).strip().split(',')[-1].strip('"')
            result = subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', '*' + sid + ':F', '*S-1-5-18:F'], capture_output=True)
            if result.returncode:
                raise OSError('无法保护本机账号文件权限。')
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def provision(root, *, reset_username=None):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import validate_password
    from django.db import transaction
    from scripts.runtime_support import FileLock
    root = Path(root)
    with FileLock(root / '.runtime/admin-account.lock'), transaction.atomic():
        if reset_username is None and administrators().exists():
            return None
        User = get_user_model()
        if reset_username is not None:
            user = administrators().filter(username=reset_username).first()
            if user is None:
                raise ValueError('没有找到这个管理员账号，不会修改项目账号。')
        else:
            username = 'admin'
            suffix = 1
            while User.objects.filter(username=username).exists():
                suffix += 1
                username = 'admin' + str(suffix)
            user = User(username=username, role='ADMIN', is_staff=True, is_superuser=True, is_active=True)
        password = 'Budget-' + secrets.token_urlsafe(20) + '-9aA!'
        validate_password(password, user)
        user.set_password(password)
        user.is_active = True
        user.save()
        # An ACL/write failure rolls back the database transaction, so an unknown password is never installed.
        path = write_credentials(root, user.username, password)
        return {'username': user.username, 'path': path}


def reveal(result):
    if result is None:
        print('已存在管理员账号，原账号和密码保持不变。忘记密码请运行“修复管理员登录-Windows.bat”。')
        return
    print('管理员账号已准备好：' + result['username'])
    print('请打开登录信息文件，复制里面的用户名和密码：' + str(result['path']))
    if sys.platform == 'win32':
        try:
            os.startfile(str(result['path']))
        except OSError:
            print('无法自动打开文件，请在“本机账号信息”文件夹中手动打开。')
