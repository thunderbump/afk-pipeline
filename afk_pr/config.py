"""Resolve host locations and committed repository policy for explicit PR passes."""

import base64
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import tomllib

DEFAULT_CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "afk/config.toml"
)
DEFAULTS = tomllib.loads(Path(__file__).with_name("defaults.toml").read_text())


def keys(value, allowed, name):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"unknown or malformed {name} settings")


def positive(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def location(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be an absolute path or start with ~/")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path or start with ~/")
    return path.resolve()


def repository(value):
    if not isinstance(value, str):
        raise TypeError("repository must be a GitHub Git URL")
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?",
        value,
    )
    if not match:
        raise ValueError("repository must be a GitHub Git URL, not a local checkout")
    return match[1].lower()


def load_config(path=DEFAULT_CONFIG, *, historical=False):
    path = Path(path)
    if path.suffix == ".json":
        if not historical:
            raise ValueError(
                "Legacy JSON is not accepted for new PR jobs; migrate to host config.toml. JSON remains available for historical status/publication retry."
            )
        value = json.loads(path.read_text())
        return {"run_root": location(value["run_root"], "run_root")}
    try:
        value = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Cannot read host TOML {path}: {error}") from error
    state = location(
        value.get(
            "state_root",
            str(
                Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
                / "afk"
            ),
        ),
        "state_root",
    )
    if historical:
        return {"run_root": state}
    keys(
        value,
        {
            "schema_version",
            "beads_workspace",
            "beads",
            "state_root",
            "workspace_root",
            "agent_timeout_seconds",
            "acquisition_timeout_seconds",
            "projects",
            "fixture_resources",
        },
        "host",
    )
    if value.get("schema_version") != 1:
        raise ValueError("host schema_version must be 1")
    result = {
        **value,
        "run_root": state,
        "workspace_root": location(
            value.get("workspace_root", str(state / "workspaces")), "workspace_root"
        ),
        "agent_timeout_seconds": positive(
            value.get("agent_timeout_seconds", DEFAULTS["agent_timeout_seconds"]),
            "agent_timeout_seconds",
        ),
        "acquisition_timeout_seconds": positive(
            value.get(
                "acquisition_timeout_seconds", DEFAULTS["acquisition_timeout_seconds"]
            ),
            "acquisition_timeout_seconds",
        ),
        "config_path": str(path.resolve()),
    }
    workspace = result["workspace_root"]
    evidence = state / "pr-reviews"
    if (
        workspace == evidence
        or workspace.is_relative_to(evidence)
        or evidence.is_relative_to(workspace)
    ):
        raise ValueError("workspace_root and durable job directory must not overlap")
    keys(value.get("beads", {}), {"password_file"}, "beads")
    projects = value.get("projects", {})
    resources = value.get("fixture_resources", {})
    if not isinstance(projects, dict) or not isinstance(resources, dict):
        raise TypeError("projects and fixture_resources must be tables")
    seen = set()
    for slug, project in projects.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", slug):
            raise ValueError("invalid project slug")
        keys(project, {"repository", "fixture_resource", "fixtures"}, "project")
        identity = repository(project.get("repository"))
        if identity in seen:
            raise ValueError("duplicate registered GitHub repository")
        seen.add(identity)
    stacks = set()
    for name, resource in resources.items():
        keys(
            resource,
            {"worker_home", "stack_path", "workspace_cleanup", "cleanup_adapter"},
            "fixture resource",
        )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ValueError("invalid fixture resource name")
        stack = location(resource.get("stack_path"), "stack_path")
        home = location(resource.get("worker_home"), "worker_home")
        if stack in stacks:
            raise ValueError("one stack_path must have one fixture resource identity")
        stacks.add(stack)
        for shared in (home, stack):
            if shared.is_relative_to(workspace) or workspace.is_relative_to(shared):
                raise ValueError(
                    "shared fixture resources must not overlap disposable workspaces"
                )
        if "cleanup_adapter" in resource:
            resource["cleanup_adapter"] = str(
                location(resource["cleanup_adapter"], "cleanup_adapter")
            )
        if resource.get("workspace_cleanup", False) is not False:
            raise ValueError(
                "external fixture workspace cleanup is unsupported until resource release is proven"
            )
    return result


def settings(path, url):
    from afk_pr.github import identity

    config = load_config(path)
    repo, _ = identity(url)
    matches = [
        (slug, p)
        for slug, p in config.get("projects", {}).items()
        if repository(p["repository"]) == repo.lower()
    ]
    if len(matches) != 1:
        raise ValueError(
            f"repository_not_registered: add a project Git URL for {repo} to {path}"
        )
    return config, *matches[0]


def fixture_policy(value):
    keys(
        value, {"command", "description", "timeout_seconds", "github_auth"}, "fixtures"
    )
    command = value.get("command", ["./scripts/validate"])
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in command)
    ):
        raise ValueError("fixtures.command must be a nonempty argv array")
    description = value.get("description", "Repository fixtures: " + " ".join(command))
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 2000
    ):
        raise ValueError(
            "fixtures.description must be nonempty text under 2000 characters"
        )
    auth = value.get("github_auth", False)
    if type(auth) is not bool:
        raise ValueError("fixtures.github_auth must be boolean")
    return {
        "command": command,
        "evidence": description,
        "timeout_seconds": positive(
            value.get("timeout_seconds", DEFAULTS["fixture_timeout_seconds"]),
            "fixture timeout",
        ),
        "github_auth": auth,
    }


def policy(github, repo, sha, project):
    """Read policy from an exact trusted commit, never the candidate worktree."""
    tree = github.api(f"repos/{repo}/git/trees/{sha}")["tree"]
    entry = next((e for e in tree if e["path"] == "afk.toml"), None)
    value = {}
    if entry is not None:
        if entry["type"] != "blob" or entry.get("mode") == "120000":
            raise ValueError("afk.toml must be a committed regular file")
        blob = github.api(f"repos/{repo}/git/blobs/{entry['sha']}")
        if blob.get("size", 0) > 65536 or blob.get("encoding") != "base64":
            raise ValueError("repository policy exceeds supported size/encoding")
        value = tomllib.loads(base64.b64decode(blob["content"]).decode())
        keys(
            value,
            {"schema_version", "base_branch", "fixtures", "validation"},
            "repository",
        )
        if value.get("schema_version") != 1:
            raise ValueError("repository schema_version must be 1")
    if "base_branch" in value and (
        not isinstance(value["base_branch"], str) or not value["base_branch"].strip()
    ):
        raise ValueError("base_branch must be a nonempty string")
    override = project.get("fixtures")
    fixtures = override if override is not None else value.get("fixtures", {})
    keys(
        fixtures,
        {"command", "description", "timeout_seconds", "github_auth"},
        "fixtures",
    )
    if "command" not in fixtures:
        scripts = next(
            (e for e in tree if e["path"] == "scripts" and e["type"] == "tree"), None
        )
        entries = (
            github.api(f"repos/{repo}/git/trees/{scripts['sha']}")["tree"]
            if scripts
            else []
        )
        if not any(
            e["path"] == "validate" and e.get("mode") == "100755" for e in entries
        ):
            raise ValueError(
                "fixture_policy_missing: commit afk.toml or executable scripts/validate, or configure an explicit host fixture override"
            )
    return (
        fixture_policy(fixtures),
        {
            "commit": sha,
            "source": "host_override"
            if override is not None
            else "afk.toml"
            if entry
            else "conventional_entrypoint",
        },
        value.get("base_branch"),
    )


def branch_sha(github, repo, branch):
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise ValueError("invalid base_branch")
    from afk_pr.jobs import git

    git(Path.cwd(), "check-ref-format", "refs/heads/" + branch)
    return github.api(f"repos/{repo}/git/ref/heads/{quote(branch, safe='/')}")[
        "object"
    ]["sha"]


def job_settings(config, slug, project, github, base):
    repo = repository(project["repository"])
    validation, provenance, _ = policy(github, repo, base, project)
    resource_name = project.get("fixture_resource")
    resource = None
    if resource_name is not None:
        raw = config.get("fixture_resources", {}).get(resource_name)
        if raw is None:
            raise ValueError(f"unknown fixture_resource: {resource_name}")
        resource = {
            "name": resource_name,
            "worker_home": str(location(raw["worker_home"], "worker_home")),
            "stack_path": str(location(raw["stack_path"], "stack_path")),
        }
    return {
        "layout": "independent-clones-v1",
        "repository": f"https://github.com/{repo}.git",
        "github_repository": repo,
        "remote": f"https://github.com/{repo}.git",
        "project": slug,
        "validation": validation,
        "policy": provenance,
        "workspace_root": str(config["workspace_root"]),
        "review_timeout": config["agent_timeout_seconds"],
        "acquisition_timeout": config["acquisition_timeout_seconds"],
        "fixture_resource": resource,
        "fixture_slot": "resource:" + resource_name
        if resource
        else "repository:" + repo,
        "cleanup_allowed": resource is None,
        "config_source": config["config_path"],
    }
