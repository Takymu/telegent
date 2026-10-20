#!/usr/bin/env python3
"""Build a small Android APK with the official SDK and JDK; no Gradle or third-party libraries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent


def run(command):
    subprocess.run([str(part) for part in command], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', default=os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT'))
    parser.add_argument('--java-home', default=os.environ.get('JAVA_HOME'))
    args = parser.parse_args()
    if not args.sdk or not args.java_home:
        parser.error('укажи --sdk и --java-home или ANDROID_HOME/JAVA_HOME')
    sdk, java = Path(args.sdk).resolve(), Path(args.java_home).resolve()
    suffix = '.exe' if os.name == 'nt' else ''
    tools = sdk/'build-tools/36.0.0'
    platform = sdk/'platforms/android-36/android.jar'
    for required in (tools/('aapt2'+suffix), tools/('zipalign'+suffix), tools/'lib/d8.jar',
                     tools/'lib/apksigner.jar', platform, java/'bin'/('javac'+suffix)):
        if not required.is_file():
            raise SystemExit('нет инструмента сборки: '+str(required))
    jvm, javac, keytool = [java/'bin'/(name+suffix) for name in ('java','javac','keytool')]
    output = ROOT/'build'; output.mkdir(exist_ok=True)
    manifest = ET.parse(ROOT/'AndroidManifest.xml').getroot()
    version = manifest.attrib['{http://schemas.android.com/apk/res/android}versionName']
    target = output/('telegent-'+version+'.apk')
    signing = ROOT/'.signing'; signing.mkdir(mode=0o700, exist_ok=True)
    store, password = signing/'telegent.p12', signing/'password'
    if store.exists() and not password.is_file():
        raise SystemExit('ключ подписи существует, но файл пароля отсутствует; ключ не заменён')
    if not store.exists():
        if target.exists():
            raise SystemExit('APK уже существует, но ключ подписи потерян; восстанови прежний ключ для обновлений')
        if not password.exists():
            with password.open('x', encoding='utf-8') as stream:
                stream.write(secrets.token_urlsafe(32))
            os.chmod(password, 0o600)
        run([keytool, '-genkeypair', '-keystore', store, '-storetype', 'PKCS12', '-alias', 'telegent',
             '-storepass:file', password, '-keypass:file', password, '-keyalg', 'RSA', '-keysize', '2048',
             '-validity', '10000', '-dname', 'CN=telegent'])
        os.chmod(store, 0o600)
    with tempfile.TemporaryDirectory(prefix='apk-', dir=output) as temporary:
        work = Path(temporary).resolve()
        if output.resolve() not in work.parents:
            raise SystemExit('временная папка сборки должна находиться внутри android/build')
        generated, classes, dex = [work/name for name in ('generated','classes','dex')]
        for directory in (generated, classes, dex): directory.mkdir()
        resources, base, aligned, signed = [work/name for name in ('resources.zip','base.apk','aligned.apk','signed.apk')]
        run([tools/('aapt2'+suffix), 'compile', '--dir', ROOT/'res', '-o', resources])
        run([tools/('aapt2'+suffix), 'link', '-I', platform, '--manifest', ROOT/'AndroidManifest.xml',
             '--java', generated, '--auto-add-overlay', '-o', base, resources])
        sources = sorted((ROOT/'src').rglob('*.java')) + sorted(generated.rglob('*.java'))
        run([javac, '-encoding', 'UTF-8', '--release', '8', '-classpath', platform, '-d', classes, *sources])
        run([jvm, '-cp', tools/'lib/d8.jar', 'com.android.tools.r8.D8', '--release', '--min-api', '26',
             '--lib', platform, '--output', dex, *sorted(classes.rglob('*.class'))])
        with zipfile.ZipFile(base, 'a', compression=zipfile.ZIP_DEFLATED) as apk:
            for item in sorted(dex.glob('*.dex')): apk.write(item, item.name)
        run([tools/('zipalign'+suffix), '-f', '4', base, aligned])
        signer = [jvm, '-jar', tools/'lib/apksigner.jar']
        run([*signer, 'sign', '--ks', store, '--ks-key-alias', 'telegent', '--ks-pass', 'file:'+str(password),
             '--out', signed, aligned])
        run([*signer, 'verify', '--verbose', '--print-certs', signed])
        run([tools/('zipalign'+suffix), '-c', '4', signed])
        shutil.copy2(signed, target)
    metadata = {'file':target.name, 'version':version, 'package':manifest.attrib['package'],
                'bytes':target.stat().st_size, 'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
    (output/'build-info.json').write_text(json.dumps(metadata, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == '__main__':
    main()
