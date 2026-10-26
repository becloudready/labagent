"""rag-ide: give each learner a private browser VS Code with the RAG lab, on serverless Modal. No learner sign-up, nothing on their laptop.

The instructor's Modal login stays on the instructor's machine. Each learner gets a URL and a random password (written to a
0600 credentials file, never to state or logs), a persistent volume at work/, and an idle timeout. See docs for the cost model.
"""
from __future__ import annotations

import csv
import os
import re
import secrets
from pathlib import Path
from typing import Any, Dict, List

from ..base import Pack, PackContext, PackError, Param, Step, state_dir
from . import ec2 as ec2mod
from . import local as localmod
from .modal_gateway import ModalGateway, app_name, slug

MAX_LEARNERS = 60
WAIT_S = 1800          # image build on the box plus certificate issuance


def learner_names(spec: Any) -> List[str]:
    """'3' -> learner-01..03; 'ana,ben' -> ana, ben. Names become resource names, so they are slugged and must be unique."""
    text = str(spec).strip()
    if re.fullmatch(r"\d+", text):
        n = int(text)
        names = [f"learner-{i:02d}" for i in range(1, n + 1)]
    else:
        names = [slug(p) for p in text.split(",") if p.strip()]
    if not names:
        raise PackError("learners must be a count (e.g. 5) or a comma list of names (e.g. ana,ben)")
    if len(set(names)) != len(names):
        raise PackError("learner names must be unique after normalising")
    if len(names) > MAX_LEARNERS:
        raise PackError(f"at most {MAX_LEARNERS} learners per cohort in one launch")
    return names


def checkout_root() -> str:
    root = Path(__file__).resolve().parents[3]
    return str(root) if (root / "pyproject.toml").is_file() and (root / "labagent" / "ai" / "rag").is_dir() else ""


def creds_path(name: str) -> Path:
    return state_dir() / "rag-ide" / f"{name}-credentials.csv"


class _LazyModal:
    """Creates the real gateway on first use, inside a step, so a missing `modal` package is a clean step failure
    (and `--dry-run` works without it) instead of a crash while the pack is being set up."""

    def __init__(self) -> None:
        self._gw = None

    def __getattr__(self, name: str):
        if self._gw is None:
            self._gw = ModalGateway()
        return getattr(self._gw, name)


class _LazyEc2:
    """Same idea as _LazyModal; region and profile are applied in preflight, before the first AWS call."""

    def __init__(self) -> None:
        self._gw = None
        self.region = None
        self.profile = None

    def __getattr__(self, name: str):
        if self._gw is None:
            self._gw = ec2mod.Ec2Gateway(region=self.region, profile=self.profile)
        return getattr(self._gw, name)


class _LazyLocal:
    def __init__(self) -> None:
        self._gw = None

    def __getattr__(self, name: str):
        if self._gw is None:
            self._gw = localmod.LocalDockerGateway()
        return getattr(self._gw, name)


class RagIde(Pack):
    id = "rag-ide"
    title = "Browser VS Code lab per learner (Modal)"
    summary = "Serverless web IDE with the RAG lab for each learner: one Modal login, nothing for learners to install"
    aliases = ("ide", "web-ide", "rag-web-ide")
    platforms = ("local", "ec2", "modal")
    cost_tier = "metered"
    default_name = "cohort"
    params = [
        Param("platform", "modal", "modal (one serverless sandbox per learner) | ec2 (one shared instance per cohort, much cheaper at scale) | local (Docker on this machine, for demos and tests)"),
        Param("port", 8080, "local: first port; learner N gets port + N - 1 on 127.0.0.1"),
        Param("learners", "1", "Count (5) or comma list of names (ana,ben)"),
        Param("instance_type", "auto", "ec2: instance type, or auto"),
        Param("spot", False, "ec2: use a spot instance (cheaper, can be reclaimed)"),
        Param("region", None, "ec2: AWS region (default from labagent config)"),
        Param("profile", None, "ec2: AWS profile"),
        Param("tls", "letsencrypt", "ec2: letsencrypt | internal (self-signed, browser warning)"),
        Param("ref", ec2mod.PINNED_REF, "ec2: labagent commit (40-hex SHA) the lab image is built from"),
        Param("hours", 4, "Hard cap per session, 1 to 24"),
        Param("idle_minutes", 15, "Shut a session down after this long with no browser connected"),
        Param("cpu", 2.0, "CPU cores per learner"),
        Param("memory_mib", 4096, "Memory per learner"),
        Param("image", "auto", "auto | pypi | path to a labagent checkout"),
        Param("llm", "none", "none (mock answers) | groq-shared (copies GROQ_API_KEY from your shell into each learner's IDE)"),
        Param("groq_model", "openai/gpt-oss-20b", "Used when llm=groq-shared"),
        Param("show_passwords", "auto", "auto (print them for 3 learners or fewer) | yes | no. They are always in the credentials file"),
        Param("keep_work", False, "destroy: keep the learners' volumes"),
    ]

    def services(self, profile):
        return {"modal": _LazyModal(), "ec2": _LazyEc2(), "local": _LazyLocal()}

    def outputs(self, state):
        """URLs only. State also holds sandbox ids and file paths that a dashboard has no use for."""
        out = {"learners": {n: {"url": i.get("url")} for n, i in sorted((state.get("learners") or {}).items())}}
        if state.get("host"):
            out["host"] = state["host"]
        return out

    def _validate(self, ctx: PackContext) -> None:
        p = ctx.params
        if p["platform"] not in ("modal", "ec2", "local"):
            raise PackError("platform must be modal, ec2 or local")
        if p["tls"] not in ("letsencrypt", "internal"):
            raise PackError("tls must be letsencrypt or internal")
        names = learner_names(p["learners"])
        if not 1 <= int(p["hours"]) <= 24:
            raise PackError("hours must be between 1 and 24")
        if p["show_passwords"] not in ("auto", "yes", "no"):
            raise PackError("show_passwords must be auto, yes or no")
        if p["llm"] not in ("none", "groq-shared"):
            raise PackError("llm must be 'none' or 'groq-shared'")
        if p["llm"] == "groq-shared" and not os.environ.get("GROQ_API_KEY"):
            raise PackError("llm=groq-shared needs GROQ_API_KEY in your shell. The key is visible to every learner: use a throwaway key with a low limit.")
        ctx.mem["names"] = names

    def _preflight(self, ctx: PackContext):
        self._validate(ctx)
        workspace = ctx.services["modal"].check_auth()
        src = ctx.params["image"]
        if src == "auto":
            src = checkout_root() or "pypi"
        elif src != "pypi" and not (Path(src) / "pyproject.toml").is_file():
            raise PackError(f"image path '{src}' is not a labagent checkout")
        ctx.log(f"  Modal workspace: {workspace} | image source: {src} | learners: {len(ctx.mem['names'])}")
        return {"workspace": workspace, "image_source": src, "app": app_name(ctx.name)}

    def _build(self, ctx: PackContext):
        gw = ctx.services["modal"]
        app = gw.app(ctx.state["app"])
        ctx.mem["app"] = app
        ctx.mem["image"] = gw.image(ctx.state["image_source"])
        gw.build(app, ctx.mem["image"])       # first build takes several minutes; later runs hit Modal's cache
        return {"image_built": True}

    def _learners(self, ctx: PackContext):
        gw = ctx.services["modal"]
        p = ctx.params
        names = ctx.mem.get("names") or learner_names(p["learners"])
        learners: Dict[str, Dict[str, str]] = dict(ctx.state.get("learners", {}))
        passwords: Dict[str, str] = {}
        env = {"GROQ_API_KEY": os.environ["GROQ_API_KEY"], "GROQ_MODEL": p["groq_model"], "LLM_PROVIDER": "groq"} if p["llm"] == "groq-shared" else {}
        app = ctx.mem.get("app") or gw.app(ctx.state["app"])
        image = ctx.mem.get("image") or gw.image(ctx.state["image_source"])
        for n in names:
            if n in learners and gw.is_alive(learners[n]["sandbox_id"]):
                ctx.log(f"  {n}: already running")
                continue
            pw = secrets.token_urlsafe(12)
            ctx.log(f"  starting {n} ...")
            learners[n] = gw.launch(app, ctx.name, n, pw, image, float(p["cpu"]), int(p["memory_mib"]), int(p["hours"]) * 3600, int(p["idle_minutes"]) * 60, env)
            passwords[n] = pw
            ctx.state["learners"] = learners        # persist progress so a failure part-way resumes
        ctx.mem["passwords"] = passwords
        self._write_creds(ctx)
        return {"learners": learners}

    def _write_creds(self, ctx: PackContext) -> Path:
        """Write the 0600 credentials file. Called as soon as learners exist (so a later failure cannot lose their passwords)
        and again as the last step. Passwords already on file are kept."""
        path = creds_path(ctx.name)
        old: Dict[str, str] = {}
        if path.exists():
            old = {r["learner"]: r["password"] for r in csv.DictReader(path.open())}
        pw = {**old, **ctx.mem.get("passwords", {})}
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["learner", "url", "password"])
            for n, info in sorted(ctx.state["learners"].items()):
                w.writerow([n, info["url"], pw.get(n, "(lost: destroy and relaunch this learner)")])
        ctx.mem["passwords"] = pw
        return path

    def _credentials(self, ctx: PackContext):
        path = self._write_creds(ctx)
        pw = ctx.mem.get("passwords", {})
        learners = sorted(ctx.state["learners"].items())
        sp = ctx.params["show_passwords"]
        show = sp == "yes" or (sp == "auto" and len(learners) <= 3)
        ctx.log("\n  Open each URL in a browser and sign in with the password:")
        for n, info in learners:
            ctx.log(f"    {n}: {info['url']}" + (f"   password: {pw.get(n, '(lost: destroy and relaunch)')}" if show else ""))
        ctx.log(f"\n  Passwords are in {path} (0600, one row per learner)" + ("" if show else f"\n  View them: cat {path}"))
        return {"credentials_file": str(path)}

    # ---- EC2 platform -------------------------------------------------------------------------------------------------------
    def _ec2_preflight(self, ctx: PackContext):
        self._validate(ctx)
        gw = ctx.services["ec2"]
        gw.region, gw.profile = ctx.params["region"], ctx.params["profile"]
        where = gw.check_auth()
        itype = ec2mod.pick_instance_type(len(ctx.mem["names"]), int(ctx.params["memory_mib"]), ctx.params["instance_type"])
        if not re.fullmatch(r"[0-9a-f]{40}", str(ctx.params["ref"])):
            raise PackError("ref must be a full 40-character commit SHA")
        ctx.log(f"  AWS: {where} | instance type: {itype}{' (spot)' if ctx.params['spot'] else ''} | learners: {len(ctx.mem['names'])}")
        return {"aws": where, "instance_type": itype}

    def _ec2_network(self, ctx: PackContext):
        return {"security_group": ctx.services["ec2"].security_group(ctx.name)}

    def _ec2_instance(self, ctx: PackContext):
        gw, p = ctx.services["ec2"], ctx.params
        names = ctx.mem.get("names") or learner_names(p["learners"])
        iid = ctx.state.get("instance_id")
        if iid and gw.alive(iid):
            ctx.log(f"  instance {iid} already running")
            ctx.mem["passwords"] = {}
            return {}
        pw = {n: secrets.token_urlsafe(12) for n in names}
        env = {"GROQ_API_KEY": os.environ["GROQ_API_KEY"], "GROQ_MODEL": p["groq_model"], "LLM_PROVIDER": "groq"} if p["llm"] == "groq-shared" else {}
        ud = ec2mod.build_user_data(pw, ref=p["ref"], hours=int(p["hours"]), idle_minutes=int(p["idle_minutes"]), cpu=float(p["cpu"]),
                                    memory_mib=int(p["memory_mib"]), tls=p["tls"], env=env)
        iid = gw.run(ctx.name, ctx.state["security_group"], ud, ctx.state["instance_type"], bool(p["spot"]))
        ctx.log(f"  launched {iid}, waiting for a public address ...")
        ip = gw.public_ip(iid)
        ctx.mem["passwords"] = pw
        host = ec2mod.sslip_host(ip)
        out = {"instance_id": iid, "host": host, "learners": {n: {"url": ec2mod.learner_url(host, n)} for n in names}}
        ctx.state.update(out)
        self._write_creds(ctx)          # before the long wait: a failure from here on must not lose the passwords
        return out

    def _ec2_wait(self, ctx: PackContext):
        gw = ctx.services["ec2"]
        host, names = ctx.state["host"], sorted(ctx.state["learners"])
        ctx.log("  the box is building the lab image (about 8 to 12 minutes the first time) ...")
        for _ in range(WAIT_S // 15):
            if all(gw.ready(host, n, verify=ctx.params["tls"] == "letsencrypt") for n in names):
                ctx.log("  all learner IDEs answer over HTTPS")
                return {"ready": True}
            gw.pause(15)
        raise PackError("learner IDEs were not reachable in time. Check the instance console log (/var/log/labagent-boot.log). Re-running resumes.")

    def _ec2_steps(self) -> List[Step]:
        return [
            Step("preflight", "Check AWS credentials, account allow list and settings", self._ec2_preflight),
            Step("network", "Create the HTTP/HTTPS-only security group", self._ec2_network),
            Step("instance", "Launch the shared instance with one container per learner", self._ec2_instance),
            Step("wait", "Wait for every learner's IDE over HTTPS", self._ec2_wait),
            Step("credentials", "Write the credentials file", self._credentials),
        ]

    def _ec2_destroy(self) -> List[Step]:
        def preflight(c):
            gw = c.services["ec2"]
            gw.region, gw.profile = c.params["region"], c.params["profile"]
            return {"aws": gw.check_auth()}

        def terminate(c):
            gw = c.services["ec2"]
            ids = sorted(({c.state["instance_id"]} if c.state.get("instance_id") else set()) | set(gw.find(c.name)))
            gw.terminate(ids)
            return {"instance_id": None, "host": None, "learners": {}, "terminated": len(ids)}

        def sg(c):
            c.services["ec2"].delete_security_group(c.name)
            return {"security_group": None}

        return [Step("preflight", "Check AWS credentials and account allow list", preflight),
                Step("terminate", "Terminate the cohort instance (also finds orphans by tag)", terminate),
                Step("network", "Delete the security group", sg),
                Step("credentials", "Remove the credentials file", self._forget)]

    # ---- local Docker platform ------------------------------------------------------------------------------------------------
    def _local_image(self, ctx: PackContext):
        image = ctx.params["image"]
        image = localmod.IMAGE if image in ("auto", "pypi") else image          # anything else is an existing image tag
        ctx.services["local"].ensure_image(image, checkout_root())
        return {"image": image}

    def _local_preflight(self, ctx: PackContext):
        self._validate(ctx)
        where = ctx.services["local"].check_auth()
        if not 1024 <= int(ctx.params["port"]) <= 65000 - len(ctx.mem["names"]):
            raise PackError("port must be between 1024 and 65000")
        ctx.log(f"  {where} | learners: {len(ctx.mem['names'])} | first port: {ctx.params['port']}")
        return {"docker": where}

    def _local_learners(self, ctx: PackContext):
        gw, p = ctx.services["local"], ctx.params
        names = ctx.mem.get("names") or learner_names(p["learners"])
        learners: Dict[str, Dict[str, str]] = dict(ctx.state.get("learners", {}))
        passwords: Dict[str, str] = {}
        env = {"GROQ_API_KEY": os.environ["GROQ_API_KEY"], "GROQ_MODEL": p["groq_model"], "LLM_PROVIDER": "groq"} if p["llm"] == "groq-shared" else {}
        for i, n in enumerate(names):
            if n in learners and gw.alive(learners[n]["container"]):
                ctx.log(f"  {n}: already running")
                continue
            gw.remove(localmod.container_name(ctx.name, n))          # a stopped container of the same name would block the new one
            pw = secrets.token_urlsafe(12)
            ctx.log(f"  starting {n} ...")
            learners[n] = gw.launch(ctx.name, n, pw, int(p["port"]) + i, image=ctx.state["image"], cpu=float(p["cpu"]), memory_mib=int(p["memory_mib"]),
                                    hours=int(p["hours"]), idle_s=int(p["idle_minutes"]) * 60, env=env)
            passwords[n] = pw
            ctx.state["learners"] = learners
        ctx.mem["passwords"] = passwords
        self._write_creds(ctx)
        return {"learners": learners}

    def _local_wait(self, ctx: PackContext):
        gw = ctx.services["local"]
        for _ in range(60):
            if all(gw.ready(info["url"]) for info in ctx.state["learners"].values()):
                ctx.log("  all learner IDEs answer")
                return {"ready": True}
            gw.pause(2)
        raise PackError("learner IDEs did not answer in time. Check: docker logs <container>")

    def _local_steps(self) -> List[Step]:
        return [
            Step("preflight", "Check Docker and settings", self._local_preflight),
            Step("image", "Use or build the lab image", self._local_image),
            Step("learners", "Start one container per learner on loopback", self._local_learners),
            Step("wait", "Wait for every learner's IDE", self._local_wait),
            Step("credentials", "Write the credentials file", self._credentials),
        ]

    def _local_destroy(self) -> List[Step]:
        def remove(c):
            gw = c.services["local"]
            names = {i["container"] for i in c.state.get("learners", {}).values()} | set(gw.find(c.name))
            for n in names:
                gw.remove(n)
            return {"learners": {}, "removed": len(names), "volumes": sorted({i["volume"] for i in c.state.get("learners", {}).values()} | set(c.state.get("volumes", [])))}

        def volumes(c):
            if not c.params["keep_work"]:
                for v in c.state.get("volumes", []):
                    c.services["local"].delete_volume(v)
                return {"volumes": []}
            return {}

        return [Step("preflight", "Check Docker", lambda c: {"docker": c.services["local"].check_auth()}),
                Step("remove", "Remove every learner's container", remove),
                Step("volumes", "Delete learner volumes (unless keep_work)", volumes),
                Step("credentials", "Remove the credentials file", self._forget)]

    def deploy_steps(self, ctx: PackContext) -> List[Step]:
        if ctx.params["platform"] == "local":
            return self._local_steps()
        if ctx.params["platform"] == "ec2":
            return self._ec2_steps()
        return [
            Step("preflight", "Check the Modal login and settings", self._preflight),
            Step("image", "Build the lab image (first time: several minutes)", self._build),
            Step("learners", "Start one private IDE per learner", self._learners),
            Step("credentials", "Write the credentials file", self._credentials),
        ]

    def _stop_all(self, ctx: PackContext):
        gw = ctx.services["modal"]
        ids = {i["sandbox_id"] for i in ctx.state.get("learners", {}).values()} | set(gw.find(ctx.name))
        for sid in ids:
            gw.stop(sid)
        return {"learners": {}, "stopped": len(ids)}

    def _volumes(self, ctx: PackContext):
        if ctx.params["keep_work"]:
            return {}
        gw = ctx.services["modal"]
        for vol in ctx.state.get("volumes", []):
            gw.delete_volume(vol)
        return {"volumes": []}

    def _forget(self, ctx: PackContext):
        creds_path(ctx.name).unlink(missing_ok=True)
        return {"credentials_file": None}

    def destroy_steps(self, ctx: PackContext) -> List[Step]:
        if ctx.params["platform"] == "local":
            return self._local_destroy()
        if ctx.params["platform"] == "ec2":
            return self._ec2_destroy()
        # remember volume names before the learner records are cleared
        ctx.state["volumes"] = sorted({i["volume"] for i in ctx.state.get("learners", {}).values() if i.get("volume")} | set(ctx.state.get("volumes", [])))
        return [
            Step("preflight", "Check the Modal login", lambda c: {"workspace": c.services["modal"].check_auth()}),
            Step("stop", "Stop every learner's IDE", self._stop_all),
            Step("volumes", "Delete learner volumes (unless keep_work)", self._volumes),
            Step("credentials", "Remove the credentials file", self._forget),
        ]
