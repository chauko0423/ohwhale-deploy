"""Local form storage. Never put entered credentials in arguments or diagnostics."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from ohwhale.config import _dotenv, reset, root

KEYS = ('DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY', 'ASTRBOT_DASHBOARD_PASSWORD')
LABELS = ('DeepSeek 密钥', '百炼密钥', '面板密码')


def revision():
    return {name: hashlib.sha256((root()/name).read_bytes()).hexdigest()
            for name in ('.env', 'config.json')}


def read_form():
    reset()
    local = json.loads((root()/'config.json').read_text('utf-8-sig'))
    values = _dotenv()
    return {'ok': True, 'revision': revision(), 'owner_qq': str(local.get('master', {}).get('qq', '')),
            **{key: values.get(key, '') for key in KEYS}}


def save_form(request):
    # Re-read before saving: a stale open form must not overwrite edits made elsewhere.
    old = read_form()
    if request.get('revision') != old['revision']:
        raise ValueError('配置文件已被其他窗口修改。请关闭并重新打开填写窗口，再保存。')
    owner = str(request.get('owner_qq', '')).strip()
    if not re.fullmatch(r'[1-9][0-9]{4,11}', owner):
        raise ValueError('主人 QQ：请输入 5～12 位数字，填写你与机器人聊天使用的 QQ 号。')
    values = {}
    for key, label in zip(KEYS, LABELS):
        value = str(request.get(key, '')).strip()
        if not value:
            raise ValueError(f'{label}：还没有填写。')
        if any(c in value for c in '\r\n\x00'):
            raise ValueError(f'{label}：不能包含换行，请重新粘贴完整的一行。')
        values[key] = value
    password = values['ASTRBOT_DASHBOARD_PASSWORD']
    if len(password) < 8 or not all(re.search(pattern, password) for pattern in ('[A-Z]', '[a-z]', '[0-9]')):
        raise ValueError('面板密码：至少 8 位，包含大写字母、小写字母和数字；这是你新设置的密码。')
    changed = owner != old['owner_qq'] or any(values[k] != old[k] for k in KEYS)
    config = json.loads((root()/'config.json').read_text('utf-8-sig'))
    runtime = Path(config.get('paths', {}).get('astrbot', 'runtime/AstrBot'))
    if not runtime.is_absolute():
        runtime = root()/runtime
    if changed and (runtime/'ohwhale-profile.json').exists():
        raise ValueError('此安装已经建立 AstrBot 配置。请在面板修改现有账号设置，本窗口不会覆盖已建立的配置。')
    if not changed:
        return {'ok': True, 'changed': False}
    # Preserve optional settings and comments. Only these three keys are replaced.
    lines = (root()/'.env').read_text('utf-8-sig').splitlines()
    remaining = set(KEYS)
    output = []
    for line in lines:
        key = line.partition('=')[0].strip()
        if key in values:
            if key in remaining:
                output.append(f'{key}="{values[key]}"')
                remaining.remove(key)
        else:
            output.append(line)
    output.extend(f'{key}="{values[key]}"' for key in KEYS if key in remaining)
    config.setdefault('master', {})['qq'] = owner
    contents = {'.env': '\n'.join(output)+'\n', 'config.json': json.dumps(config, ensure_ascii=False, indent=2)+'\n'}
    original = {name: (root()/name).read_bytes() for name in contents}
    pending = []
    replaced = []
    try:
        for name, content in contents.items():
            fd, temp = tempfile.mkstemp(prefix='.account-', suffix='.tmp', dir=root())
            pending.append((name, Path(temp)))
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(content)
        if revision() != old['revision']:
            raise ValueError('保存期间配置文件发生变化。请重新打开填写窗口。')
        for name, temp in pending:
            os.replace(temp, root()/name)
            replaced.append(name)
    except Exception:
        for name in replaced:
            (root()/name).write_bytes(original[name])
        raise
    finally:
        for _, temp in pending:
            temp.unlink(missing_ok=True)
    reset()
    return {'ok': True, 'changed': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('read', 'save'))
    args = parser.parse_args()
    try:
        report = read_form() if args.action == 'read' else save_form(json.loads(sys.stdin.read().lstrip('\ufeff')))
    except ValueError as exc:
        # Only intentional validation errors may be displayed. JSON exceptions can
        # contain private input, so never forward their raw text.
        detail = '配置文件格式不正确，请检查或恢复本地配置。' if isinstance(exc, json.JSONDecodeError) else str(exc)
        report = {'ok': False, 'detail': detail}
    except Exception:
        report = {'ok': False, 'detail': '无法读写本地配置。请确认文件未被占用、安装目录可写。'}
    # PowerShell 5.1 decodes native stdout using the console code page (often
    # GBK), independently of $OutputEncoding. ASCII JSON escapes preserve both
    # Chinese validation messages and saved non-ASCII values on every code page.
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
