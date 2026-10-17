import json
import pathlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / '.build'


def build():
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir()
    source = json.loads((ROOT / 'upstream.json').read_text())
    request = urllib.request.Request(source['archive_url'], headers={'User-Agent': 'Svent-packaging'})
    archive = BUILD / 'spoolsample.tar.gz'
    with urllib.request.urlopen(request, timeout=120) as response, archive.open('wb') as output:
        shutil.copyfileobj(response, output)
    with tarfile.open(archive) as source_archive:
        source_archive.extractall(BUILD / 'source', filter='data')
    projects = list((BUILD / 'source').iterdir())
    if len(projects) != 1 or not projects[0].is_dir():
        raise RuntimeError('Unexpected source archive layout')
    project = projects[0] / 'SpoolSample'
    target = BUILD / 'SpoolSample.exe'
    subprocess.run(['mcs', '-unsafe', '-optimize+', '-platform:x64', '-target:exe',
                    '-out:' + str(target), '-r:System.Core', '-r:System.Xml.Linq',
                    '-r:Microsoft.CSharp', '-r:System.Data', '-r:System.Xml',
                    *[str(project / name) for name in ('Data.cs', 'RDIShellcodeLoader.cs',
                       'Properties/AssemblyInfo.cs', 'SpoolSample.cs')]], check=True)
    data = target.read_bytes()
    if not data.startswith(b'MZ'):
        raise RuntimeError('Invalid compiled Windows executable')
    manifest = json.loads((ROOT / 'armory-manifest.json').read_text())
    manifest['resources'].append({'category': 'spoolsample', 'filename': 'SpoolSample.exe',
                                 'type': 'bundled',
                                 'version': source['version'], 'license': 'BSD-3-Clause',
                                 'license_url': 'https://github.com/leechristensen/SpoolSample/blob/' + source['version'] + '/LICENSE'})
    (BUILD / 'armory-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


def install():
    destination = ROOT / 'debian/sv-armory-ad'
    paths = {'files/armory-ad': 'usr/bin/armory-ad',
             'files/manager.py': 'usr/libexec/svent/armory-ad.py',
             '.build/SpoolSample.exe': 'usr/share/armory/ad/spoolsample/SpoolSample.exe',
             '.build/armory-manifest.json': 'usr/share/armory/ad-manifest.json'}
    for source, relative in paths.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source, target)
        target.chmod(0o755 if relative == 'usr/bin/armory-ad' else 0o644)
    shutil.copytree(ROOT / 'catalog.d', destination / 'usr/share/svent/catalog.d', dirs_exist_ok=True)


if __name__ == '__main__':
    action = sys.argv[1]
    if action == 'build':
        build()
    elif action == 'install':
        install()
    elif action == 'clean':
        if BUILD.exists():
            shutil.rmtree(BUILD)
    else:
        raise RuntimeError('Unknown build action')
