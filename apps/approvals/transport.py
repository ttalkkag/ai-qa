import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from django.conf import settings

from apps.errors import DomainError


@dataclass(frozen=True)
class RemoteObservation:
    repository_id: str
    repository_full_name: str
    base_sha: str
    branch_sha: str = ""
    pr_number: int | None = None
    pr_url: str = ""
    pr_head_sha: str = ""
    pr_draft: bool = False


def new_ref_push_args(remote_url, branch, commit_sha):
    if not re.fullmatch(r"aiqa/run-[a-f0-9-]{36}", branch) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit_sha):
        raise DomainError("INVALID_PUBLICATION_TARGET")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", remote_url):
        raise DomainError("INVALID_PUBLICATION_TARGET")
    ref = f"refs/heads/{branch}"
    return ["push", "--porcelain", "--no-verify", "--no-follow-tags", "--recurse-submodules=no", f"--force-with-lease={ref}:", remote_url, f"{commit_sha}:{ref}"]


def public_summary(value):
    from apps.artifacts.storage import redact_text

    text = redact_text(value)
    text = re.sub(r"(?<![\w:/])/(?!/)[^\s<>()`]+", "[LOCAL_PATH]", text)
    return re.sub(r"(?<!\w)[A-Za-z]:\\[^\s<>`]+", "[LOCAL_PATH]", text)


class GitHubTransport:
    def __init__(self):
        self.git = self._binary("git", "AIQA_GIT_BINARY")
        self.gh = self._binary("gh", "AIQA_GH_BINARY")
        self.root = Path(settings.AIQA_DATA_DIR) / "publishing"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise DomainError("UNSAFE_PUBLICATION_STORAGE")
        self.auth_dir = Path(getattr(settings, "AIQA_GH_CONFIG_DIR", Path.home() / ".config" / "gh")).resolve()

    @staticmethod
    def _binary(name, setting):
        value = getattr(settings, setting, None) or shutil.which(name)
        if not value:
            raise DomainError("PUBLICATION_TOOL_MISSING")
        path = Path(value).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise DomainError("PUBLICATION_TOOL_MISSING")
        return str(path)

    def _environment(self, directory):
        return {
            "PATH": os.defpath, "HOME": str(directory), "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/usr/bin/false", "SSH_ASKPASS": "/usr/bin/false",
            "GIT_OPTIONAL_LOCKS": "0", "GH_CONFIG_DIR": str(self.auth_dir), "GH_PROMPT_DISABLED": "1", "GH_HOST": "github.com",
        }

    def _run(self, job, directory, args, *, stdin=None, env=None, allow_absent=False):
        from apps.runs.processes import launch_process

        result = launch_process(job=job, argv=args, cwd=directory, env=env or self._environment(directory), timeout=120, stdin_text=stdin)
        if result.timed_out or not result.output_complete:
            raise DomainError("REMOTE_OUTCOME_UNKNOWN")
        if result.returncode:
            if allow_absent and "HTTP 404" in result.stderr:
                return None
            raise DomainError("REMOTE_COMMAND_FAILED")
        return result.stdout

    def _git(self, job, directory, *args, stdin=None, env=None):
        config = [
            "core.hooksPath=/dev/null", "core.attributesFile=/dev/null", "core.fsmonitor=false", "core.autocrlf=false", "core.filemode=true",
            "commit.gpgsign=false", "tag.gpgSign=false", "protocol.allow=never", "protocol.https.allow=always", "protocol.file.allow=never",
            "http.followRedirects=false", "fetch.recurseSubmodules=false", "push.followTags=false", "push.recurseSubmodules=no",
            "credential.helper=", f"credential.helper=!{shlex.quote(self.gh)} auth git-credential",
        ]
        prefix = [self.git]
        for value in config:
            prefix.extend(["-c", value])
        return self._run(job, directory, [*prefix, *args], stdin=stdin, env=env)

    def _api(self, job, endpoint, *, method="GET", fields=None, allow_absent=False):
        args = [self.gh, "api", "--hostname", "github.com", "--method", method, endpoint]
        for key, value in (fields or {}).items():
            args.extend(["-F" if isinstance(value, bool) else "-f", f"{key}={str(value).lower() if isinstance(value, bool) else value}"])
        raw = self._run(job, self.root, args, allow_absent=allow_absent)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise DomainError("REMOTE_RESPONSE_INVALID") from None

    def _repository(self, snapshot, job):
        policy = snapshot["policy"]
        result = self._api(job, f"repos/{policy['repository_full_name']}")
        if (str(result.get("id")) != policy["repository_id"] or result.get("full_name") != policy["repository_full_name"]
                or result.get("html_url") != f"https://github.com/{policy['repository_full_name']}"
                or not result.get("permissions", {}).get("push")):
            raise DomainError("REMOTE_TARGET_CHANGED")
        return result

    def observe(self, snapshot, job):
        policy = snapshot["policy"]
        self._repository(snapshot, job)
        prefix = f"repos/{policy['repository_full_name']}"
        base = self._api(job, f"{prefix}/git/ref/heads/{quote(policy['base_branch'], safe='')}")
        branch = self._api(job, f"{prefix}/git/ref/heads/{quote(snapshot['branch'], safe='')}", allow_absent=True)
        query = f"state=all&head={quote(policy['repository_full_name'].split('/')[0] + ':' + snapshot['branch'], safe='')}&base={quote(policy['base_branch'], safe='')}&per_page=100"
        prs = self._api(job, f"{prefix}/pulls?{query}")
        self._repository(snapshot, job)
        if not isinstance(prs, list) or len(prs) > 1:
            raise DomainError("REMOTE_PR_CONFLICT")
        pr = prs[0] if prs else {}
        if pr and (pr.get("head", {}).get("ref") != snapshot["branch"] or pr.get("base", {}).get("ref") != policy["base_branch"]
                   or str(pr.get("head", {}).get("repo", {}).get("id")) != policy["repository_id"]
                   or str(pr.get("base", {}).get("repo", {}).get("id")) != policy["repository_id"]
                   or pr.get("html_url") != f"https://github.com/{policy['repository_full_name']}/pull/{pr.get('number')}"):
            raise DomainError("REMOTE_PR_CONFLICT")
        return RemoteObservation(
            repository_id=policy["repository_id"], repository_full_name=policy["repository_full_name"],
            base_sha=base["object"]["sha"], branch_sha=branch["object"]["sha"] if branch else "",
            pr_number=pr.get("number"), pr_url=pr.get("html_url", ""), pr_head_sha=pr.get("head", {}).get("sha", ""),
            pr_draft=bool(pr.get("draft") and pr.get("state") == "open"),
        )

    def _assemble(self, snapshot, bundle, job, directory):
        from apps.artifacts.services import materialize_baseline, read_bundle_file
        from apps.artifacts.trees import collect_tree, tree_digest

        source = Path(directory) / "source"
        materialize_baseline(bundle.id, source)
        baseline, contents = collect_tree(source)
        self._git(job, source, "init", "--quiet", "--template=")
        self._git(job, source, "fetch", "--no-tags", "--no-recurse-submodules", "--depth=1", snapshot["policy"]["remote_url"], f"refs/heads/{snapshot['policy']['base_branch']}")
        fetched = self._git(job, source, "rev-parse", "--verify", "FETCH_HEAD").strip()
        if fetched != snapshot["baseline_commit_sha"]:
            raise DomainError("BASE_CHANGED")
        raw = self._git(job, source, "ls-tree", "-r", "-z", "--full-tree", fetched)
        remote_tree = {}
        for item in raw.split("\x00"):
            if item:
                header, path = item.split("\t", 1)
                mode, kind, oid = header.split(" ")
                if kind != "blob":
                    raise DomainError("UNSUPPORTED_SNAPSHOT")
                remote_tree[path] = (int(mode, 8), oid)
        digest = hashlib.sha1 if len(fetched) == 40 else hashlib.sha256
        baseline_git = {row["path"]: (row["mode"], digest(f"blob {len(contents[row['path']])}\0".encode() + contents[row["path"]]).hexdigest()) for row in baseline}
        if remote_tree != baseline_git:
            raise DomainError("BASELINE_TREE_CHANGED")
        patch = read_bundle_file(bundle.id, "canonical.patch")
        if patch:
            self._git(job, source, "apply", "--binary", "--whitespace=nowarn", "-", stdin=patch.decode("utf-8"))
        if tree_digest(source) != snapshot["result_tree_sha256"]:
            raise DomainError("RESULT_TREE_CHANGED")
        entries, content = collect_tree(source)
        self._git(job, source, "read-tree", "--empty")
        for row in entries:
            if row["kind"] == "symlink":
                oid = self._git(job, source, "hash-object", "-w", "--no-filters", "--stdin", stdin=content[row["path"]].decode("utf-8")).strip()
            else:
                oid = self._git(job, source, "hash-object", "-w", "--no-filters", "--", row["path"]).strip()
            self._git(job, source, "update-index", "--add", "--cacheinfo", format(row["mode"], "o"), oid, row["path"])
        tree = self._git(job, source, "write-tree").strip()
        env = self._environment(source)
        for role in ("AUTHOR", "COMMITTER"):
            env[f"GIT_{role}_NAME"] = snapshot["policy"]["commit_author_name"]
            env[f"GIT_{role}_EMAIL"] = snapshot["policy"]["commit_author_email"]
            env[f"GIT_{role}_DATE"] = snapshot["commit_timestamp"]
        commit = self._git(job, source, "commit-tree", tree, "-p", fetched, "-m", snapshot["commit_message"], env=env).strip()
        return source, commit

    def prepare(self, snapshot, bundle, job):
        self._repository(snapshot, job)
        with tempfile.TemporaryDirectory(prefix="prepare-", dir=self.root) as directory:
            _, commit = self._assemble(snapshot, bundle, job, directory)
            return commit

    def push(self, snapshot, job):
        from apps.artifacts.services import verify_bundle
        from .services import _check_observation, authorize_transport_write

        bundle = verify_bundle(snapshot["bundle_id"], snapshot["manifest_sha256"])
        with tempfile.TemporaryDirectory(prefix="publish-", dir=self.root) as directory:
            source, commit = self._assemble(snapshot, bundle, job, directory)
            if commit != snapshot["commit_sha"]:
                raise DomainError("APPROVED_COMMIT_CHANGED")
            observation = self.observe(snapshot, job)
            _check_observation(snapshot, observation)
            if observation.branch_sha:
                raise DomainError("REMOTE_REF_CONFLICT")
            authorize_transport_write(job, snapshot)
            self._git(job, source, *new_ref_push_args(snapshot["policy"]["remote_url"], snapshot["branch"], commit))

    def create_pr(self, snapshot, job):
        from apps.artifacts.services import verify_bundle
        from .services import _check_observation, authorize_transport_write

        bundle = verify_bundle(snapshot["bundle_id"], snapshot["manifest_sha256"])
        body = public_summary(f"AI QA 실행 {snapshot['run_id']}\n\n{bundle.summary}\n\n필수 검증 PASS. 결과 {snapshot['manifest_sha256']}\n기준 {snapshot['baseline_commit_sha']}")
        observation = self.observe(snapshot, job)
        _check_observation(snapshot, observation)
        if observation.pr_number:
            return
        if observation.branch_sha != snapshot["commit_sha"]:
            raise DomainError("REMOTE_REF_CONFLICT")
        authorize_transport_write(job, snapshot)
        self._api(job, f"repos/{snapshot['policy']['repository_full_name']}/pulls", method="POST", fields={
            "title": f"AI QA: {snapshot['run_id']}", "body": body, "head": snapshot["branch"], "base": snapshot["policy"]["base_branch"], "draft": True,
        })
