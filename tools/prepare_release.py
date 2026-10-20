#!/usr/bin/env python3
"""Prepare public APK and client metadata from a committed revision; no credentials needed."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def git(*parts):
    return subprocess.check_output(['git', '-C', str(ROOT), *parts])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', default='HEAD')
    parser.add_argument('--apk', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    commit = git('rev-parse', args.commit+'^{commit}').decode().strip()
    source = git('show', commit+':tg.py')
    version = re.search(rb'^CLIENT_VERSION = "([^"]+)"', source, re.M)
    if not version: raise SystemExit('в коммите нет версии клиента')
    android = ET.fromstring(git('show', commit+':android/AndroidManifest.xml'))
    namespace = '{http://schemas.android.com/apk/res/android}'
    name, code = android.attrib[namespace+'versionName'], int(android.attrib[namespace+'versionCode'])
    apk = Path(args.apk).resolve()
    info = json.loads((apk.parent/'build-info.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    if info['sha256'] != digest or info['version'] != name or info['version_code'] != code:
        raise SystemExit('APK не соответствует выбранной версии; сначала собери её')
    files = {}
    for path in ('tg.py', 'AGENTS.md', 'MANIFEST.md'):
        body = git('show', commit+':'+path)
        previous = set()
        for revision in git('log', '-40', '--format=%H', commit, '--', path).decode().splitlines():
            old = git('show', revision+':'+path).replace(b'\r\n', b'\n')
            previous.update(hashlib.sha256(value).hexdigest() for value in (old, old.replace(b'\n', b'\r\n')))
        files[path] = {'sha256': hashlib.sha256(body).hexdigest(), 'previous_sha256': sorted(previous)}
    doc = {'client': {'version': version[1].decode(), 'commit': commit, 'files': files},
           'android': {'version_code': code, 'version': name, 'bytes': apk.stat().st_size,
                       'sha256': digest, 'url': 'https://github.com/Takymu/telegent/releases/download/v'+name+'/telegent.apk'}}
    output = Path(args.out); output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(apk, output/'telegent.apk')
    (output/'releases.json').write_text(json.dumps(doc, indent=2)+'\n', encoding='utf-8')
    print('Подготовлены telegent.apk и releases.json для v'+name+' / '+commit[:7])


if __name__ == '__main__':
    main()
