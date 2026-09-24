"""Local development identity; replace this adapter with platform identity in production."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import uuid
from itsdangerous import URLSafeTimedSerializer, BadSignature
from fastapi import HTTPException


class LocalAuth:
    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / 'local-auth.json'
        if not path.exists():
            password = os.environ.get('BRANCH_LOCAL_PASSWORD') or secrets.token_urlsafe(18)
            username = os.environ.get('BRANCH_LOCAL_USER', 'developer')
            salt = secrets.token_hex(16)
            data = {
                'username': username, 'salt': salt,
                'password_hash': hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex(),
                'account_id': 'local-' + str(uuid.uuid4()), 'signing_key': secrets.token_urlsafe(48),
            }
            # Publish a complete 0600 file atomically so simultaneous startup cannot read a partial secret.
            pending=self.root/('.local-auth-'+uuid.uuid4().hex+'.tmp')
            fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            try:
                with os.fdopen(fd,'w') as output:
                    json.dump(data,output)
                    output.flush();os.fsync(output.fileno())
                try:os.link(pending,path)
                except FileExistsError:created=False
                else:created=True
            finally:pending.unlink(missing_ok=True)
            if created:
                login=self.root/'local-login.txt'
                fd=os.open(login,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                with os.fdopen(fd,'w') as output:
                    output.write(f'用户名：{username}\n密码：{password}\n仅供本地开发，请勿提交或公开。\n')
                login.chmod(0o600)
        path.chmod(0o600)
        self.data = json.loads(path.read_text())
        self.signer = URLSafeTimedSerializer(self.data['signing_key'], salt='branch-session')

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str):
            raise HTTPException(400, detail='用户名和密码必须是文本')
        if len(username) > 200 or len(password.encode('utf-8')) > 4096:
            raise HTTPException(400, detail='登录信息长度超出限制')
        key = hashlib.scrypt(password.encode(), salt=self.data['salt'].encode(), n=16384, r=8, p=1).hex()
        username_matches = hmac.compare_digest(username.encode(), self.data['username'].encode())
        password_matches = hmac.compare_digest(key, self.data['password_hash'])
        if not (username_matches and password_matches):
            raise HTTPException(401, detail='用户名或密码错误')
        identity = {
            'account_id': self.data['account_id'], 'display_name': self.data['username'],
            'csrf_token': secrets.token_urlsafe(24),
        }
        return identity, self.signer.dumps(identity)

    def identity(self, request, write=False):
        try:
            data = self.signer.loads(request.cookies.get('branch_session', ''), max_age=86400 * 7)
        except BadSignature:
            raise HTTPException(401, detail='请先登录本地测试账号') from None
        if (not isinstance(data, dict) or data.get('account_id') != self.data['account_id']
                or not isinstance(data.get('csrf_token'), str) or not data['csrf_token']
                or not isinstance(data.get('display_name'), str)):
            raise HTTPException(401, detail='登录状态无效，请重新登录')
        if write:
            csrf = request.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(csrf.encode(), data['csrf_token'].encode()):
                raise HTTPException(403, detail='CSRF验证失败')
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                raise HTTPException(403, detail='来源不匹配')
        return data
