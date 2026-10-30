"""`labagent rag init | destroy | ide | playground`. Wired into the main parser by labagent.cli (argparse, like launch/destroy)."""
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import samples

IMAGE = "labagent-rag-lab:local"
CONTAINER_LAB = "/home/coder/lab"


def add_parser(sub) -> None:
    rp = sub.add_parser("rag", help="Local, zero-cost RAG lab: init a workspace, open the web IDE (ide), or the visual playground")
    rs = rp.add_subparsers(dest="rag_cmd", required=True)
    i = rs.add_parser("init", help="Create the lab folder: README.md, .env, data/sample_docs, nine notebooks and src/main.py")
    i.add_argument("--dir", default=".", help="Target folder (default: current)")
    i.add_argument("--force", action="store_true", help="Overwrite files that already exist")
    i.add_argument("--solutions", action="store_true", help="Instructors: also write the solved notebooks to solutions/")
    x = rs.add_parser("destroy", help="Delete a lab folder created by `rag init` (asks first)")
    x.add_argument("--dir", required=True, help="The lab folder to delete")
    x.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    d = rs.add_parser("ide", help="Start a browser VS Code (code-server) on the workspace")
    d.add_argument("--dir", default=".", help="Workspace folder (initialised if it has no .env)")
    d.add_argument("--port", type=int, default=8080)
    d.add_argument("--mode", choices=("auto", "local", "docker"), default="auto",
                   help="local runs the code-server on your PATH; docker runs the lab image; auto prefers docker")
    d.add_argument("--image", default=IMAGE, help="Docker image to run (built from Dockerfile.lab in a labagent checkout if missing)")
    d.add_argument("--print-command", action="store_true", help="Show the command and exit")
    s = rs.add_parser("playground", aliases=["serve"], help="Visual playground on port 8501: chunks, scores and answers side by side")
    s.add_argument("--dir", default=".", help="Project folder with .env and data/")
    s.add_argument("--port", type=int, default=8501)


def init(target: Path, force: bool = False, solutions: bool = False) -> int:
    files = {target / rel: content for rel, content in samples.workspace(solutions).items()}
    clash = [str(p) for p in files if p.exists()]
    if clash and not force:
        print("error: would overwrite " + ", ".join(clash) + " (use --force)", file=sys.stderr)
        return 2
    for p, content in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    (target / ".env").chmod(0o600)
    print(f"created {len(files)} files in {target.resolve()}")
    print(f"next: open {target / 'README.md'} and start with notebooks/01_chunking.ipynb")
    return 0


def _is_lab(path: Path) -> bool:
    return all((path / rel).exists() for rel in (".env", "README.md", "notebooks/01_chunking.ipynb"))


def _rmtree_onerror(func, path, _exc):
    """Windows refuses to delete read-only files (.env is chmod 600); clear the flag and retry."""
    os.chmod(path, 0o700)
    func(path)


def destroy(target: Path, yes: bool = False) -> int:
    """Delete a lab folder. Refuses anything that does not look like a `rag init` lab, and the home or current folder."""
    target = target.resolve()
    if not target.is_dir():
        print(f"error: {target} does not exist or is not a folder", file=sys.stderr)
        return 2
    cwd = Path.cwd().resolve()
    if target == Path.home().resolve() or target.parent == target or target == cwd or target in cwd.parents:
        print(f"error: refusing to delete {target} (it is your home folder, a drive root, or contains the current folder). cd out of it first.", file=sys.stderr)
        return 2
    if not _is_lab(target):
        print(f"error: {target} does not look like a labagent RAG lab (needs .env, README.md and notebooks/01_chunking.ipynb). Nothing deleted.", file=sys.stderr)
        return 2
    if not yes:
        if input(f"Delete {target} and everything in it? Type yes to confirm: ").strip().lower() != "yes":
            print("cancelled, nothing deleted")
            return 1
    shutil.rmtree(target, onerror=_rmtree_onerror)
    if target.exists():
        print(f"error: {target} could not be fully deleted. Close programs using it (Jupyter, VS Code) and retry.", file=sys.stderr)
        return 1
    print(f"deleted {target}")
    return 0


def _find_dockerfile() -> Optional[Path]:
    """Dockerfile.lab sits at the root of a labagent checkout (three levels above this file). Not shipped in the wheel."""
    root = Path(__file__).resolve().parents[3]
    return root / "Dockerfile.lab" if (root / "Dockerfile.lab").is_file() else None


def ide_command(mode: str, workspace: Path, port: int, password: str, image: str = IMAGE) -> List[str]:
    """The command that serves the web IDE. The port is published on 127.0.0.1 only, and a password is always required."""
    if mode == "local":
        return ["code-server", "--bind-addr", f"127.0.0.1:{port}", "--auth", "password", str(workspace)]
    return ["docker", "run", "--rm", "-p", f"127.0.0.1:{port}:8080", "-e", "PASSWORD", "-v", f"{workspace}:{CONTAINER_LAB}", image]


def _pick_mode(mode: str) -> str:
    if mode != "auto":
        return mode
    return "docker" if shutil.which("docker") else "local"


def _docker_ready(image: str) -> Optional[str]:
    """None when docker can run `image` (building it if needed), else an error message."""
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return "docker is installed but its daemon is not running. Start Docker Desktop and retry."
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
        return None
    dockerfile = _find_dockerfile()
    if dockerfile is None:
        return (f"image '{image}' not found and no Dockerfile.lab is nearby. Run this from a labagent checkout "
                "(git clone https://github.com/becloudready/labagent) or pass --image with a prebuilt image.")
    print(f"building {image} from {dockerfile} (first time takes several minutes) ...")
    rc = subprocess.call(["docker", "build", "-f", str(dockerfile), "-t", image, str(dockerfile.parent)])
    return None if rc == 0 else "docker build failed"


def ide(workspace: Path, port: int, mode: str, image: str, print_only: bool = False) -> int:
    mode = _pick_mode(mode)
    if mode == "local" and not shutil.which("code-server"):
        print("error: code-server is not on PATH. Install it (https://coder.com/docs/code-server/install) or use --mode docker.", file=sys.stderr)
        return 2
    if not (workspace / ".env").exists():
        rc = init(workspace)
        if rc:
            return rc
    workspace = workspace.resolve()
    password = os.environ.get("CODE_SERVER_PASSWORD") or secrets.token_urlsafe(12)
    cmd = ide_command(mode, workspace, port, password, image)
    if print_only:
        print(" ".join(cmd))
        return 0
    if mode == "docker":
        err = _docker_ready(image)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
    print(f"web IDE: http://127.0.0.1:{port}\npassword: {password}\nnotebook: notebooks/01_chunking.ipynb  (Ctrl+C to stop)", flush=True)
    return subprocess.call(cmd, env={**os.environ, "PASSWORD": password})


def playground(target: Path, port: int) -> int:
    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("error: streamlit is not installed. Fix: pip install 'labagent[rag-local]'", file=sys.stderr)
        return 2
    script = Path(__file__).with_name("playground.py")
    cmd = [sys.executable, "-m", "streamlit", "run", str(script), "--server.port", str(port), "--server.headless", "true", "--", "--dir", str(target.resolve())]
    print(f"playground: http://localhost:{port}")
    return subprocess.call(cmd)


def run(args) -> int:
    # PowerShell and cmd do not expand "~" before the program sees it
    target = Path(args.dir).expanduser()
    if args.rag_cmd == "init":
        return init(target, args.force, getattr(args, "solutions", False))
    if args.rag_cmd == "destroy":
        return destroy(target, args.yes)
    if args.rag_cmd == "ide":
        return ide(target, args.port, args.mode, args.image, args.print_command)
    return playground(target, args.port)
