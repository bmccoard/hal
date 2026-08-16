"""Concrete publication providers behind provider-neutral workflow contracts."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from types import MappingProxyType
from typing import Any, Mapping
import urllib.error
import urllib.parse
import urllib.request

from .cancellation import CancellationToken, cancellation_or_default
from .workflow_publication import (
    PublicationCredentialScope, PublicationIsolationCapability,
    PullRequestAdapter, PullRequestRequest, PullRequestResult, PushAdapter,
)
from .workflow_runtime import WorkflowTransientError


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    owner: str
    name: str

    def __post_init__(self) -> None:
        pattern = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?"
        if not re.fullmatch(pattern, self.owner) or not re.fullmatch(pattern, self.name):
            raise ValueError("GitHub owner and repository names are invalid")

    @property
    def identity(self) -> str:
        return f"github:{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class GitHubPullRequest:
    number: int
    url: str
    title: str
    body: str
    base: str
    head: str
    head_sha: str


class GitHubRESTAPI:
    """Small credential-owning GitHub REST surface used only by the adapter."""

    def __init__(
        self, token: str, *, api_base: str = "https://api.github.com",
        timeout_seconds: float = 60,
    ) -> None:
        if not token.strip():
            raise ValueError("GitHub publication token must not be empty")
        parsed = urllib.parse.urlparse(api_base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("GitHub API base must be an HTTPS origin or path")
        if timeout_seconds <= 0:
            raise ValueError("GitHub API timeout must be positive")
        self.__token = token.strip()
        self.api_base = api_base.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def list_pull_requests(
        self, repository: GitHubRepository, *, head: str, base: str,
        cancellation: CancellationToken | None = None,
    ) -> tuple[GitHubPullRequest, ...]:
        query = urllib.parse.urlencode({
            "state": "open", "head": f"{repository.owner}:{head}", "base": base,
            "per_page": 100,
        })
        data = self._request(
            "GET", f"/repos/{repository.owner}/{repository.name}/pulls?{query}",
            None, cancellation,
        )
        if not isinstance(data, list):
            raise RuntimeError("GitHub pull-request list response must be an array")
        return tuple(self._pull_request(item) for item in data)

    def create_pull_request(
        self, repository: GitHubRepository, *, title: str, body: str,
        head: str, base: str, cancellation: CancellationToken | None = None,
    ) -> GitHubPullRequest:
        data = self._request(
            "POST", f"/repos/{repository.owner}/{repository.name}/pulls",
            {"title": title, "body": body, "head": head, "base": base}, cancellation,
        )
        if not isinstance(data, Mapping):
            raise RuntimeError("GitHub pull-request create response must be an object")
        return self._pull_request(data)

    def _request(
        self, method: str, path: str, payload: Mapping[str, Any] | None,
        cancellation: CancellationToken | None,
    ) -> Any:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.api_base + path, data=data, method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.__token}",
                "Content-Type": "application/json",
                "User-Agent": "hal-workflow-publication",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            # Provider messages are bounded and the credential is never included.
            detail = exc.read(16_384).decode("utf-8", "replace").replace(
                self.__token, "[redacted]",
            )
            if exc.code == 429:
                raise WorkflowTransientError(
                    "rate_limit", f"GitHub API {exc.code}: {detail}",
                ) from exc
            if exc.code in {408, 409} or exc.code >= 500:
                raise WorkflowTransientError(
                    "service_unavailable", f"GitHub API {exc.code}: {detail}",
                ) from exc
            raise RuntimeError(f"GitHub API {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise WorkflowTransientError(
                "network", f"GitHub API connection failed: {exc.reason}",
            ) from exc
        except TimeoutError as exc:
            raise WorkflowTransientError("timeout", "GitHub API timed out") from exc
        cancellation.raise_if_cancelled()
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("GitHub API response exceeded 2 MiB")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("GitHub API returned invalid JSON") from exc

    @staticmethod
    def _pull_request(value: Mapping[str, Any]) -> GitHubPullRequest:
        try:
            number = value["number"]
            url = str(value["html_url"])
            title = str(value["title"])
            body = str(value.get("body") or "")
            base = str(value["base"]["ref"])
            head = str(value["head"]["ref"])
            head_sha = str(value["head"]["sha"]).lower()
        except (KeyError, TypeError) as exc:
            raise RuntimeError("GitHub pull-request response is malformed") from exc
        parsed = urllib.parse.urlparse(url)
        if (
            isinstance(number, bool) or not isinstance(number, int) or number <= 0
            or parsed.scheme != "https" or not parsed.netloc
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head_sha)
        ):
            raise RuntimeError("GitHub pull-request identity is malformed")
        return GitHubPullRequest(number, url, title, body, base, head, head_sha)


class GitHubPullRequestAdapter:
    """Idempotent GitHub PR adapter with repository-scoped authority."""

    provider = "github"

    def __init__(
        self,
        api: GitHubRESTAPI,
        repositories: Mapping[str, GitHubRepository],
        *,
        credential_id: str,
        isolation: PublicationIsolationCapability,
    ) -> None:
        if not isolation.enforceable:
            raise ValueError("GitHub adapter requires an enforceable isolation boundary")
        if not credential_id.strip():
            raise ValueError("GitHub adapter credential scope needs a non-secret ID")
        if not repositories:
            raise ValueError("GitHub adapter requires at least one allowed repository")
        aliases = dict(repositories)
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", key) for key in aliases):
            raise ValueError("GitHub repository aliases are invalid")
        identities = {alias: repository.identity for alias, repository in aliases.items()}
        if len(set(identities.values())) != len(identities):
            raise ValueError("GitHub repository identities must be unique")
        self.api = api
        self.repositories = MappingProxyType(aliases)
        self.remote_identities = MappingProxyType(identities)
        self.isolation = isolation
        self.credential_scope = PublicationCredentialScope(
            credential_id.strip(), frozenset(identities.values()),
            frozenset({"pull_request"}),
        )

    def find_pull_request(
        self, request: PullRequestRequest, cancellation: CancellationToken,
    ) -> PullRequestResult | None:
        repository = self._repository(request)
        marker = _idempotency_marker(request.idempotency_key)
        candidates = [
            item for item in self.api.list_pull_requests(
                repository, head=request.head, base=request.base,
                cancellation=cancellation,
            )
            if marker in item.body
        ]
        if len(candidates) > 1:
            raise RuntimeError("GitHub returned multiple PRs for one HAL idempotency key")
        if not candidates:
            return None
        return self._result(request, candidates[0], "existing")

    def create_pull_request(
        self, request: PullRequestRequest, cancellation: CancellationToken,
    ) -> PullRequestResult:
        repository = self._repository(request)
        marker = _idempotency_marker(request.idempotency_key)
        body = request.body.rstrip() + "\n\n" + marker
        item = self.api.create_pull_request(
            repository, title=request.title, body=body,
            head=request.head, base=request.base, cancellation=cancellation,
        )
        return self._result(request, item, "created")

    def _repository(self, request: PullRequestRequest) -> GitHubRepository:
        try:
            repository = self.repositories[request.remote]
        except KeyError as exc:
            raise PermissionError(f"GitHub remote {request.remote!r} is not configured") from exc
        if repository.identity != request.remote_identity:
            raise PermissionError("GitHub request remote identity is outside adapter scope")
        return repository

    @staticmethod
    def _result(
        request: PullRequestRequest, item: GitHubPullRequest, outcome: str,
    ) -> PullRequestResult:
        if (
            item.base != request.base or item.head != request.head
            or item.head_sha != request.commits[-1]
        ):
            raise RuntimeError("GitHub PR does not match the requested branch and commit")
        return PullRequestResult(
            str(item.number), item.url, outcome, request.remote_identity,
            request.base, request.head, request.commits,
        )


class PublicationAdapterRegistry:
    """Routes provider-neutral nodes to explicitly installed trusted adapters."""

    def __init__(
        self,
        *,
        push: Mapping[str, PushAdapter] | None = None,
        pull_request: Mapping[str, PullRequestAdapter] | None = None,
    ) -> None:
        self._push = MappingProxyType(dict(push or {}))
        self._pull_request = MappingProxyType(dict(pull_request or {}))
        for provider, adapter in (*self._push.items(), *self._pull_request.items()):
            if provider != adapter.provider:
                raise ValueError(f"publication adapter key {provider!r} does not match provider")

    def push(self, provider: str) -> PushAdapter | None:
        return self._push.get(provider)

    def pull_request(self, provider: str) -> PullRequestAdapter | None:
        return self._pull_request.get(provider)


def _idempotency_marker(key: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("publication idempotency key is invalid")
    return f"<!-- hal-idempotency:{key} -->"
