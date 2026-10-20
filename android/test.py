#!/usr/bin/env python3
"""Check native navigation policy on a real JVM without a device or third-party dependencies."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--java-home', default=os.environ.get('JAVA_HOME'), required=not bool(os.environ.get('JAVA_HOME')))
    args = parser.parse_args()
    java = Path(args.java_home)/'bin'
    suffix = '.exe' if os.name == 'nt' else ''
    output = ROOT/'build'; output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='policy-test-', dir=output) as work:
        if output.resolve() not in Path(work).resolve().parents:
            raise SystemExit('test directory must remain inside android/build')
        subprocess.run([str(java/('javac'+suffix)), '--release', '8', '-encoding', 'UTF-8', '-d', work,
                        str(ROOT/'src/io/github/takymu/telegent/NavigationPolicy.java'),
                        str(ROOT/'tests/NavigationPolicyTest.java')], check=True)
        subprocess.run([str(java/('java'+suffix)), '-cp', work, 'io.github.takymu.telegent.NavigationPolicyTest'], check=True)


if __name__ == '__main__':
    main()
