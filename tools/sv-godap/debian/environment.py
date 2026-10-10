import hashlib
import importlib.metadata
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[1]
META = json.loads((ROOT / "upstream.json").read_text())
BUILD = ROOT / ".build"
SOURCE = BUILD / "source"
ENVIRONMENT = BUILD / "environment"
PREFIX = "/opt/" + META["package"]
ENV = os.environ.copy()
for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP", "VIRTUAL_ENV",
            "RUBYLIB", "RUBYOPT", "GEM_HOME", "GEM_PATH", "BUNDLE_GEMFILE", "BUNDLE_PATH", "RUBYGEMS_GEMDEPS"):
    ENV.pop(key, None)
ENV.update(PYTHONNOUSERSITE="1", PIP_CONFIG_FILE="/dev/null", PIP_NO_INPUT="1",
           PIP_REQUIRE_VIRTUALENV="true", PIP_DISABLE_PIP_VERSION_CHECK="1")


def run(arguments, cwd=ROOT, environment=None):
    print("[+] " + " ".join(map(str, arguments)), flush=True)
    subprocess.run(list(map(str, arguments)), cwd=cwd, env=environment or ENV, check=True)


def pip(*arguments):
    run([ENVIRONMENT / "bin/python", "-s", "-E", "-m", "pip", "--no-cache-dir", *arguments])


def download_source():
    request = urllib.request.Request(META["archive_url"], headers={"User-Agent": "Svent-packaging"})
    archive = BUILD / "source.tar.gz"
    with urllib.request.urlopen(request, timeout=90) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != META["sha256"]:
        raise RuntimeError("Upstream archive checksum mismatch")
    destination = BUILD / "unpacked"
    destination.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")
    entries = list(destination.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        raise RuntimeError("Unexpected upstream archive layout")
    entries[0].rename(SOURCE)
    for relative, replacements in META.get("source_replacements", {}).items():
        path = SOURCE / relative
        text = path.read_text()
        for old, new in replacements.items():
            if old not in text:
                raise RuntimeError("Expected upstream dependency missing: " + old)
            text = text.replace(old, new)
        path.write_text(text)


def smoke(arguments):
    result = subprocess.run(list(map(str, arguments)), cwd=SOURCE if SOURCE.exists() else ROOT,
                            env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=90)
    output = result.stdout
    print(output[-12000:], flush=True)
    if any(value in output for value in ("Traceback (most recent call last)", "ModuleNotFoundError",
                                         "ImportError:", "LibraryNotFoundError")):
        raise RuntimeError("Tool import check failed")
    if result.returncode not in (0, 1, 2) or not output.strip():
        raise RuntimeError("Tool help check failed")
    if result.returncode and not re.search(r"usage|options|help|run.*root", output, re.I):
        raise RuntimeError("Tool returned an unexpected help error")


def python_notices():
    paths = [str(path) for path in ENVIRONMENT.glob("lib/python*/site-packages")]
    notices = []
    for distribution in importlib.metadata.distributions(path=paths):
        metadata = distribution.metadata
        licenses = {}
        for relative in distribution.files or []:
            name = str(relative).lower()
            if "license" not in name and "copying" not in name and "notice" not in name:
                continue
            path = pathlib.Path(distribution.locate_file(relative)).resolve()
            if not path.is_relative_to(ENVIRONMENT.resolve()) or not path.is_file() or path.stat().st_size > 150000:
                continue
            licenses[str(relative)] = path.read_text(errors="replace")
        notices.append({"name": metadata.get("Name"), "version": metadata.get("Version"),
                        "license_expression": metadata.get("License-Expression"),
                        "license": metadata.get("License"),
                        "license_classifiers": [value for value in metadata.get_all("Classifier", []) if value.startswith("License ::")],
                        "project_urls": metadata.get_all("Project-URL", []), "license_files": licenses})
    (BUILD / "third-party-notices.json").write_text(json.dumps(notices, indent=2) + "\n")


def go_corresponding_source(environment):
    module = subprocess.run(["go", "mod", "download", "-json", META["source"] + "@" + META["version"]],
                            cwd=ROOT, env=environment, stdout=subprocess.PIPE, text=True, check=True)
    module_data = json.loads(module.stdout)
    project = BUILD / "go-source"
    copy(pathlib.Path(module_data["Dir"]), project)
    project.chmod(0o755)
    for path in project.rglob('*'):
        if path.is_file() and not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)
    result = subprocess.run(["go", "mod", "download", "-json", "all"], cwd=project,
                            env=environment, stdout=subprocess.PIPE, text=True, check=True)
    decoder = json.JSONDecoder()
    remaining = result.stdout.lstrip()
    records = [module_data]
    while remaining:
        record, end = decoder.raw_decode(remaining)
        records.append(record)
        remaining = remaining[end:].lstrip()
    cache = pathlib.Path(subprocess.check_output(["go", "env", "GOMODCACHE"], env=environment, text=True).strip()) / "cache/download"
    destination = BUILD / "corresponding-source"
    proxy = destination / "modules"
    proxy.mkdir(parents=True)
    for record in records:
        for key in ("Info", "GoMod", "Zip"):
            if not record.get(key):
                continue
            path = pathlib.Path(record[key])
            relative = path.relative_to(cache)
            target = proxy / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    instructions = 'GOPROXY="file://$(pwd)/modules" GOSUMDB=off GOTOOLCHAIN=local CGO_ENABLED=0 go install -trimpath -ldflags "-s -w" '
    (destination / "rebuild.sh").write_text("#!/bin/sh\nset -eu\n" + instructions + META["source"] + "@" + META["version"] + "\n")
    shutil.copy2(ROOT / "upstream.json", destination / "upstream.json")
    with tarfile.open(BUILD / "corresponding-source.tar.gz", "w:gz") as archive:
        archive.add(destination, arcname="corresponding-source")


def build():
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir()
    kind = META["kind"]
    if kind in ("git-python", "perl"):
        download_source()
    if kind in ("pypi", "git-python"):
        run(["/usr/bin/python3", "-m", "venv", ENVIRONMENT])
        pins = META["build_pins"]
        pip("install", "--upgrade", "pip==" + pins["pip"], "wheel==" + pins["wheel"],
            "setuptools==" + pins["setuptools"])
        constraints = BUILD / "constraints.txt"
        constraints.write_text("setuptools==" + pins["setuptools"] + "\n" +
                               "\n".join(META.get("constraints", [])) + "\n")
        ENV["PIP_CONSTRAINT"] = str(constraints)
        if kind == "pypi":
            pip("install", META["source"] + "==" + META["version"])
        else:
            requirements = SOURCE / "requirements.txt"
            if requirements.exists():
                pip("install", "-r", requirements)
            if META.get("install_project"):
                ENV.update(META.get("build_environment", {}))
                pip("install", SOURCE)
            if META.get("requirements"):
                pip("install", *META["requirements"])
        check = subprocess.run([str(ENVIRONMENT / "bin/python"), "-s", "-E", "-m", "pip",
                                "show", "oscrypto"], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        if check.returncode == 0:
            pip("install", "--no-deps", "--force-reinstall", META["oscrypto_source"])
        pip("check")
        python_notices()
        with (BUILD / "requirements-installed.txt").open("w") as output:
            subprocess.run([str(ENVIRONMENT / "bin/python"), "-m", "pip", "freeze", "--all"],
                           env=ENV, cwd=ROOT, stdout=output, check=True)
        commands = META["commands"].copy()
        if META.get("impacket_scripts"):
            for path in sorted((ENVIRONMENT / "bin").glob("*.py")):
                commands["impacket-" + path.stem.lower()] = "bin/" + path.name
        (BUILD / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        first = META["test_command"]
        target = commands[first]
        path = SOURCE / target[7:] if target.startswith("source/") else ENVIRONMENT / target
        if not path.is_file():
            raise RuntimeError("Missing upstream command: " + str(path))
        smoke([ENVIRONMENT / "bin/python", "-s", "-E", path, *META["test_arguments"]])
    elif kind == "go":
        binary_directory = BUILD / "bin"
        binary_directory.mkdir()
        environment = ENV.copy()
        for key in ("GOPRIVATE", "GONOSUMDB", "GONOPROXY", "GOFLAGS"):
            environment.pop(key, None)
        environment.update(GOBIN=str(binary_directory), CGO_ENABLED="0", GOTOOLCHAIN="auto",
                           GOSUMDB="sum.golang.org", GOPROXY="https://proxy.golang.org,direct")
        jobs = os.environ.get("SVENT_BUILD_JOBS", "2")
        if not jobs.isdigit() or int(jobs) < 1:
            raise RuntimeError("SVENT_BUILD_JOBS must be a positive integer")
        run(["go", "install", "-p", jobs, "-trimpath", "-ldflags", "-s -w",
             META["source"] + "@" + META["version"]], environment=environment)
        smoke([binary_directory / META["test_command"], *META["test_arguments"]])
        if META.get("include_corresponding_source"):
            go_corresponding_source(environment)
    elif kind == "gem":
        environment = ENV.copy()
        environment.update(GEM_HOME=str(ENVIRONMENT), GEM_PATH=str(ENVIRONMENT), GEMRC="/dev/null",
                           GEM_SPEC_CACHE=str(BUILD / "gem-spec-cache"))
        run([META.get("gem_command", "gem"), "--norc", "install", "--no-document", "--install-dir", ENVIRONMENT, "--bindir",
             ENVIRONMENT / "bin", "--version", META["version"], META["source"]], environment=environment)
        with (BUILD / "gems-installed.txt").open("w") as output:
            subprocess.run([META.get("gem_command", "gem"), "list", "--local"], env=environment, cwd=ROOT, stdout=output, check=True)
        environment["PATH"] = str(ENVIRONMENT / "bin") + ":" + environment["PATH"]
        result = subprocess.run([str(ENVIRONMENT / "bin" / META["test_command"]), "--help"],
                                env=environment, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, timeout=90)
        print(result.stdout[-12000:], flush=True)
        if result.returncode or "LoadError" in result.stdout:
            raise RuntimeError("Ruby tool help check failed")
    elif kind == "perl":
        run(["perl", "-c", SOURCE / "enum4linux.pl"])
    else:
        raise RuntimeError("Unsupported build type")


def copy(source, destination):
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))


def configure():
    if os.geteuid() != 0:
        raise RuntimeError("Environment installation requires root")
    with tempfile.TemporaryDirectory(prefix=META["package"] + "-", dir="/var/tmp") as directory:
        workspace = pathlib.Path(directory)
        (workspace / "debian").mkdir()
        shutil.copy2(ROOT / "upstream.json", workspace / "upstream.json")
        shutil.copy2(ROOT / "debian/environment.py", workspace / "debian/environment.py")
        copy(ROOT / "catalog.d", workspace / "catalog.d")
        run(["/usr/bin/python3", workspace / "debian/environment.py", "configure-local"], cwd=workspace)


def configure_local():
    if os.geteuid() != 0:
        raise RuntimeError("Environment installation requires root")
    META["redistribution"] = "local-installation"
    build()
    staging = ROOT / "debian" / META["package"]
    staging.mkdir()
    install(staging)
    prefix = pathlib.Path(PREFIX)
    candidate = prefix.parent / ("." + META["package"] + "-" + uuid.uuid4().hex)
    previous = prefix.parent / ("." + META["package"] + "-previous-" + uuid.uuid4().hex)
    copy(staging / PREFIX.lstrip("/"), candidate)
    replaced = False
    try:
        if prefix.exists():
            prefix.rename(previous)
            replaced = True
        candidate.rename(prefix)
    except BaseException:
        if replaced and not prefix.exists():
            previous.rename(prefix)
        raise
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)
    if previous.exists():
        if previous.is_symlink():
            previous.unlink()
        else:
            shutil.rmtree(previous)


def install(destination):
    destination = pathlib.Path(destination).resolve()
    if destination != (ROOT / "debian" / META["package"]).resolve():
        raise RuntimeError("Unexpected package staging directory")
    target = destination / PREFIX.lstrip("/")
    target.mkdir(parents=True, exist_ok=True)
    kind = META["kind"]
    commands = META["commands"].copy()
    downloader = META.get("redistribution") == "download-at-install"
    if downloader:
        assets = destination / "usr/libexec/svent/tool-environments" / META["package"]
        (assets / "debian").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "debian/environment.py", assets / "debian/environment.py")
        shutil.copy2(ROOT / "upstream.json", assets / "upstream.json")
        copy(ROOT / "catalog.d", assets / "catalog.d")
    elif kind in ("pypi", "git-python"):
        copy(ENVIRONMENT, target)
        if kind == "git-python" and not META.get("install_project"):
            copy(SOURCE, target / "source")
        commands = json.loads((BUILD / "commands.json").read_text())
        for path in (target / "bin").iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            if data.startswith(b"#!") and b"\x00" not in data:
                data = data.replace(str(ENVIRONMENT).encode(), PREFIX.encode())
                path.write_bytes(data)
                path.chmod(0o755)
        configuration = target / "pyvenv.cfg"
        configuration.write_text(configuration.read_text().replace(str(ENVIRONMENT), PREFIX))
        for path in target.rglob("*.pth"):
            if str(BUILD) in path.read_text(errors="replace"):
                raise RuntimeError("Build path leaked into Python search configuration")
        shutil.copy2(BUILD / "requirements-installed.txt", target / "requirements-installed.txt")
        shutil.copy2(BUILD / "third-party-notices.json", target / "third-party-notices.json")
        if SOURCE.exists():
            for path in SOURCE.iterdir():
                if path.is_file() and path.name.lower().startswith(("license", "copying", "notice")):
                    licenses = target / "licenses/upstream"
                    licenses.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, licenses / path.name)
    elif kind == "gem":
        copy(ENVIRONMENT, target)
        shutil.copy2(BUILD / "gems-installed.txt", target / "gems-installed.txt")
    elif kind == "perl":
        copy(SOURCE, target / "source")
    elif kind == "go":
        binary_directory = destination / "usr/bin"
        binary_directory.mkdir(parents=True, exist_ok=True)
        for command in commands:
            shutil.copy2(BUILD / "bin" / command, binary_directory / command)
            (binary_directory / command).chmod(0o755)
        if META.get("include_corresponding_source"):
            documentation = destination / "usr/share/doc" / META["package"]
            documentation.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BUILD / "corresponding-source.tar.gz", documentation / "corresponding-source.tar.gz")
    if kind != "go":
        for command, relative in commands.items():
            wrapper = destination / "usr/bin" / command
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            header = "#!/bin/sh\nset -eu\n"
            if kind in ("pypi", "git-python"):
                header += "unset PYTHONPATH PYTHONHOME PYTHONUSERBASE PYTHONSTARTUP\nexport PYTHONNOUSERSITE=1\n"
                if relative.startswith("source/"):
                    if not downloader and not (target / relative).is_file():
                        raise RuntimeError("Missing vendored script: " + relative)
                    execution = f'exec {PREFIX}/bin/python -s -E {PREFIX}/{relative} "$@"\n'
                else:
                    if not downloader and not (target / relative).is_file():
                        raise RuntimeError("Missing private entry point: " + relative)
                    execution = f'exec {PREFIX}/bin/python -s -E {PREFIX}/{relative} "$@"\n'
                if command == "responder":
                    header += 'umask 077\nstate_directory="${XDG_STATE_HOME:-$HOME/.local/state}/svent/responder"\n'
                    header += 'mkdir -p "$state_directory/logs"\nexport SVENT_RESPONDER_STATE="$state_directory"\n'
                    header += f'cd {PREFIX}/source\n' 
            elif kind == "gem":
                header += "unset RUBYLIB RUBYOPT BUNDLE_GEMFILE BUNDLE_PATH RUBYGEMS_GEMDEPS\n"
                header += f'export GEM_HOME={PREFIX}\nexport GEM_PATH={PREFIX}\n'
                header += f'export PATH="{PREFIX}/bin:$PATH"\n'
                execution = f'exec {META.get("ruby_command", "/usr/bin/ruby")} {PREFIX}/bin/{command} "$@"\n'
            else:
                header += "unset PERL5LIB PERL5OPT PERLLIB\n"
                execution = f'exec /usr/bin/perl {PREFIX}/source/enum4linux.pl "$@"\n'
            wrapper.write_text(header + execution)
            wrapper.chmod(0o755)
    copy(ROOT / "catalog.d", destination / "usr/share/svent/catalog.d")
    shutil.copy2(ROOT / "upstream.json", target / "upstream.json")


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "build":
        build()
    elif action == "install":
        install(sys.argv[2])
    elif action == "clean":
        if BUILD.exists():
            shutil.rmtree(BUILD)
    elif action == "configure":
        configure()
    elif action == "configure-local":
        configure_local()
    else:
        raise RuntimeError("Unknown build action")
