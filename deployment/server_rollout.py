"""Run from a server-side Git checkout; never accepts uploaded source archives."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

SOURCE = Path(__file__).resolve().parent.parent
CONTAINER = "turb-gpt-free-register"
BASE_IMAGE = "turb-gpt-free-register:20261126-roxy"
SUITES = [
    "tests/test_camoufox_integration.py", "tests/test_registration_push.py",
    "tests/test_registration_email_allocation.py", "tests/test_roxybrowser_id_precision.py",
    "tests/test_roxybrowser_client_retry.py", "tests/test_browser_traffic.py",
    "tests/test_browser_data_saver.py",
]


def run(*args, capture=False, timeout=None):
    result = subprocess.run(
        list(map(str, args)), check=True, text=True, timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def inspect():
    return json.loads(run("docker", "inspect", CONTAINER, capture=True))[0]


def source_paths():
    # Only application code/resources are installed; runtime files stay in place.
    entries = run("git", "-C", SOURCE, "ls-files", "-z", capture=True).split("\0")
    allowed_files = {"main.py", "web.py", "requirements.txt", "README.md", ".env.example", "webui.sh"}
    allowed_dirs = {"config", "core", "webui", "sentinel", "static"}
    paths = []
    for name in entries:
        if not name:
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Unsafe Git path")
        if name in allowed_files or relative.parts[0] in allowed_dirs:
            if (SOURCE / relative).is_symlink():
                raise RuntimeError(f"Application source must be a regular file: {name}")
            paths.append(relative)
    return paths


def active_jobs(root):
    database = root / "app" / "turb.sqlite3"
    if not database.is_file():
        raise RuntimeError("Expected live database is missing")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM registration_jobs WHERE status IN ('pending','running','stopping')",
        ).fetchone()[0]


def wait_idle(root):
    for _ in range(180):
        count = active_jobs(root)
        if not count:
            return
        print(f"Waiting for {count} active registration job(s) before rollout", flush=True)
        time.sleep(10)
    raise RuntimeError("Active jobs did not drain; running service was left intact")


def verify(root, revision, paths):
    state = inspect()
    if state["Config"]["Labels"].get("org.opencontainers.image.revision") != revision:
        raise RuntimeError("Running image revision differs from Git checkout")
    if state["State"].get("Health", {}).get("Status") != "healthy":
        raise RuntimeError("Container is not healthy")
    for relative in paths:
        expected = hashlib.sha256((SOURCE / relative).read_bytes()).digest()
        if hashlib.sha256((root / "app" / relative).read_bytes()).digest() != expected:
            raise RuntimeError(f"Deployed source differs from Git: {relative}")
    run("docker", "exec", CONTAINER, "python", "-c",
        "from webui.config_editor import EDITABLE_FIELDS; "
        "assert any(f['key']=='CAMOUFOX_HEADLESS' for f in EDITABLE_FIELDS); "
        "import config.roxybrowser as c; print('REGISTRATION_DRIVER='+c.REGISTRATION_DRIVER)")
    run("docker", "exec", CONTAINER, "sh", "-c",
        "supervisorctl -c /etc/turb/supervisord.conf status roxy | grep -q RUNNING")
    print(f"DEPLOYMENT_VERIFIED revision={revision} files={len(paths)}", flush=True)


def rollout(root, revision, paths):
    initial = inspect()
    labels = initial["Config"].get("Labels", {})
    override = root / "compose.git.json"
    compose_files = [
        Path(value).resolve()
        for value in labels["com.docker.compose.project.config_files"].split(",")
        if Path(value).resolve() != override
    ]
    if not compose_files or any(not path.is_relative_to(root) for path in compose_files):
        raise RuntimeError("Compose configuration is outside the deployment root")
    compose = ["docker", "compose", "--project-directory", str(root),
               "--project-name", labels["com.docker.compose.project"]]
    for path in compose_files:
        compose += ["-f", str(path)]
    if override.exists():
        compose += ["-f", str(override)]

    image = f"turb-gpt-free-register:git-{revision}"
    run("docker", "image", "inspect", BASE_IMAGE, capture=True)
    run("docker", "build", "--progress=plain", "-f", SOURCE / "deployment" / "Dockerfile",
        "--build-arg", f"BASE_IMAGE={BASE_IMAGE}", "--build-arg", f"GIT_REVISION={revision}",
        "-t", image, SOURCE)
    common = ["docker", "run", "--rm", "--init", "--shm-size=2g",
              "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
              "--entrypoint", "python", "-w", "/opt/turb-source",
              "-e", "PYTHONPATH=/opt/turb-source"]
    # The test containers have no live state, credentials, published ports or job queue.
    run(*common, image, "-m", "pytest", "-q", *SUITES, timeout=240)
    run(*common, image, "deployment/browser_smoke.py", timeout=240)
    wait_idle(root)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = root / "backups" / f"{stamp}-{revision[:12]}"
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup, 0o700)
    previous_override = override.read_bytes() if override.exists() else None
    (backup / "container.json").write_text(json.dumps(initial), encoding="utf-8")
    os.chmod(backup / "container.json", 0o600)
    existed = {}
    stopped = False
    changed = False
    try:
        # Recheck immediately before shutdown; never interrupt an observed active job.
        if active_jobs(root):
            raise RuntimeError("New jobs arrived before shutdown; retry rollout later")
        run("docker", "stop", "-t", "130", CONTAINER)
        stopped = True
        if active_jobs(root):
            raise RuntimeError("Job state changed during shutdown; restoring original service")
        for relative in paths:
            destination = root / "app" / relative
            if destination.is_symlink() or not destination.resolve().is_relative_to(root / "app"):
                raise RuntimeError(f"Unsafe deployment destination: {relative}")
            existed[str(relative)] = destination.exists()
            if destination.exists():
                saved = backup / "source" / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved)
        for name in (".env", "remail_state.json"):
            if (root / "app" / name).exists():
                shutil.copy2(root / "app" / name, backup / name)
                os.chmod(backup / name, 0o600)
        with sqlite3.connect(root / "app" / "turb.sqlite3") as live:
            with sqlite3.connect(backup / "turb.sqlite3") as saved_db:
                live.backup(saved_db)
        changed = True
        for relative in paths:
            destination = root / "app" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / relative, destination)
            shutil.copymode(SOURCE / relative, destination)
            os.chown(destination, 10001, 10001)
        override.write_text(json.dumps({"services": {"web": {"image": image}}}), encoding="utf-8")
        if "-f" not in compose[-2:] or str(override) != compose[-1]:
            compose += ["-f", str(override)]
        run(*compose, "up", "-d", "--no-build", "--force-recreate", "web")
        for _ in range(60):
            state = inspect()["State"]
            if state.get("Health", {}).get("Status") == "healthy":
                break
            if not state.get("Running"):
                raise RuntimeError("New container exited")
            time.sleep(3)
        verify(root, revision, paths)
        (root / "deployed-version.json").write_text(
            json.dumps({"revision": revision, "release": str(SOURCE),
                        "image": image, "backup": str(backup)}, indent=2), encoding="utf-8",
        )
    except BaseException:
        if changed:
            subprocess.run(["docker", "stop", "-t", "130", CONTAINER], check=False)
            for relative in paths:
                destination = root / "app" / relative
                saved = backup / "source" / relative
                if existed.get(str(relative)):
                    shutil.copy2(saved, destination)
                    os.chown(destination, 10001, 10001)
                elif destination.exists():
                    destination.unlink()
            if previous_override is not None:
                override.write_bytes(previous_override)
            else:
                override.write_text(
                    json.dumps({"services": {"web": {"image": initial["Config"]["Image"]}}}),
                    encoding="utf-8",
                )
            run(*compose, "up", "-d", "--no-build", "--force-recreate", "web")
        elif stopped:
            run("docker", "start", CONTAINER)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment-root", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    root = args.deployment_root.resolve()
    if root != Path("/opt/turb-gpt-free-register"):
        raise RuntimeError("Unexpected deployment root")
    revision = run("git", "-C", SOURCE, "rev-parse", "HEAD", capture=True)
    if SOURCE != root / "releases" / revision:
        raise RuntimeError("Run from the exact revision's server-side Git checkout")
    run("git", "-C", SOURCE, "diff", "--quiet", "HEAD")
    paths = source_paths()
    with (root / "deployment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.verify_only:
            verify(root, revision, paths)
        else:
            rollout(root, revision, paths)


if __name__ == "__main__":
    main()
