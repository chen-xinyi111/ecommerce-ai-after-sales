"""Check files selected for Git publication without displaying matched secrets."""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'provider token': re.compile(rb'\b(?:sk-(?:proj-|ant-|lf-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})\b'),
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'credential URL': re.compile(rb'https?://[^\s/]+:[^\s/@]+@'),
    'local user path': re.compile(rb'(?:[A-Za-z]:[\\/]+[Uu]sers[\\/]+|/[Uu]sers/|/[Hh]ome/)[^\s\"\'<>/\\]+', re.I),
}
EXCLUDED = {'.git', '.runtime', '.venv', 'node_modules', 'dist', '__pycache__', '.pytest_cache', '.ruff_cache'}

def files():
    probe = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if probe.returncode == 0 and Path(probe.stdout.strip()).resolve() == ROOT.resolve():
        names = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'])
        return [ROOT / name.decode('utf-8') for name in names.split(b'\0') if name]
    return [p for p in ROOT.rglob('*') if p.is_file() and not EXCLUDED.intersection(p.relative_to(ROOT).parts)]

def main():
    known = {}
    for name, value in os.environ.items():
        if re.search(r'API_?KEY|TOKEN|SECRET|PASSWORD|(?:^|_)PAT(?:$|_)', name, re.I) and len(value) >= 12:
            known[name] = value.encode()
    errors = []
    count = 0
    for path in files():
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if (path.name.startswith('.env') and not path.name.endswith('.example')) or EXCLUDED.intersection(path.relative_to(ROOT).parts):
            errors.append(f'{rel}: local configuration or generated files must not be tracked')
        data = path.read_bytes()
        count += 1
        for label, pattern in PATTERNS.items():
            if pattern.search(data):
                errors.append(f'{rel}: possible {label}')
        for name, value in known.items():
            if value in data:
                errors.append(f'{rel}: configured value for {name}')
    for error in errors:
        print(error)
    print(f'Scanned {count} selected files; findings: {len(errors)}. Matched values are never printed.')
    return bool(errors)

if __name__ == '__main__':
    sys.exit(main())
