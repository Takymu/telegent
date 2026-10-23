"""Resolve the official Firebase SDK with a verified Gradle distribution."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parent
GRADLE_SHA256='20f1b1176237254a6fc204d8434196fa11a4cfb387567519c61556e8710aed78'


def build(sdk,java,target,version,code):
    tools=ROOT/'build/tools';tools.mkdir(parents=True,exist_ok=True)
    home=tools/'gradle-8.13'
    if not (home/'bin/gradle').is_file():
        archive=tools/'gradle-8.13-bin.zip'
        if not archive.is_file():
            temporary=archive.with_suffix('.download')
            with urllib.request.urlopen('https://services.gradle.org/distributions/gradle-8.13-bin.zip',timeout=60) as response,temporary.open('wb') as output:
                shutil.copyfileobj(response,output)
            temporary.replace(archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=GRADLE_SHA256:
            raise SystemExit('Контрольная сумма Gradle не совпала; архив не запущен')
        with zipfile.ZipFile(archive) as source:
            for name in source.namelist():
                if tools.resolve() not in (tools/name).resolve().parents:raise SystemExit('Недопустимый путь в архиве Gradle')
            source.extractall(tools)
    (ROOT/'local.properties').write_text('sdk.dir='+str(sdk).replace('\\','/').replace(':','\\:')+'\n',encoding='utf-8')
    environment=dict(os.environ,JAVA_HOME=str(java),GRADLE_USER_HOME=str(ROOT/'build/gradle-user'),DEBUG='')
    executable=home/'bin'/('gradle.bat' if os.name=='nt' else 'gradle')
    if os.name!='nt':executable.chmod(0o755)
    subprocess.run([str(executable),'--no-daemon','--console=plain','-p',str(ROOT),
                    '-PtelegentVersionName='+version,'-PtelegentVersionCode='+str(code),'assembleRelease'],env=environment,check=True)
    apks=list((ROOT/'build/outputs/apk/release').glob('*.apk'))
    if len(apks)!=1:raise SystemExit('Ожидался один подписанный APK')
    shutil.copy2(apks[0],target)
