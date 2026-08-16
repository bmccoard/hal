import io
import json

import pytest

from hal.cancellation import CancellationToken
from hal.workflow_publication import (
    PublicationIsolationCapability, PullRequestRequest,
)
from hal.workflow_publication_adapters import (
    GitHubPullRequest, GitHubPullRequestAdapter, GitHubRepository,
    GitHubRESTAPI, PublicationAdapterRegistry,
)
from hal.workflow_runtime import WorkflowTransientError


COMMIT = "1" * 40
KEY = "2" * 64


class FakeGitHubAPI:
    def __init__(self) -> None:
        self.items = []
        self.created = []

    def list_pull_requests(self, repository, *, head, base, cancellation=None):
        return tuple(self.items)

    def create_pull_request(
        self, repository, *, title, body, head, base, cancellation=None,
    ):
        self.created.append({
            "repository": repository, "title": title, "body": body,
            "head": head, "base": base,
        })
        item = GitHubPullRequest(
            42, "https://github.com/acme/repo/pull/42", title, body,
            base, head, COMMIT,
        )
        self.items.append(item)
        return item


def _request() -> PullRequestRequest:
    return PullRequestRequest(
        __import__("pathlib").Path("."), "origin", "github:acme/repo",
        "Change", "Body", "main", "feature", (COMMIT,), {"passed": True},
        "Reviewed", {"body": "3" * 64, "checks": "4" * 64, "review": "5" * 64},
        KEY,
    )


def _adapter(api=None):
    return GitHubPullRequestAdapter(
        api or FakeGitHubAPI(), {"origin": GitHubRepository("acme", "repo")},
        credential_id="github-pr-token",
        isolation=PublicationIsolationCapability(True, "isolated-github-api"),
    )


def test_github_adapter_creates_then_discovers_same_pr() -> None:
    api = FakeGitHubAPI()
    adapter = _adapter(api)
    request = _request()

    assert adapter.find_pull_request(request, CancellationToken()) is None
    created = adapter.create_pull_request(request, CancellationToken())
    existing = adapter.find_pull_request(request, CancellationToken())

    assert created.outcome == "created"
    assert existing.outcome == "existing"
    assert created.provider_id == existing.provider_id == "42"
    assert created.url == "https://github.com/acme/repo/pull/42"
    assert api.created[0]["body"].endswith(f"<!-- hal-idempotency:{KEY} -->")
    assert adapter.remote_identities == {"origin": "github:acme/repo"}
    assert adapter.credential_scope.operations == frozenset({"pull_request"})


def test_github_adapter_rejects_ambiguous_or_mismatched_pr() -> None:
    api = FakeGitHubAPI()
    marker = f"<!-- hal-idempotency:{KEY} -->"
    api.items = [
        GitHubPullRequest(
            number, f"https://github.com/acme/repo/pull/{number}", "Change",
            marker, "main", "feature", COMMIT,
        )
        for number in (1, 2)
    ]
    adapter = _adapter(api)
    with pytest.raises(RuntimeError, match="multiple PRs"):
        adapter.find_pull_request(_request(), CancellationToken())

    api.items = [GitHubPullRequest(
        1, "https://github.com/acme/repo/pull/1", "Change", marker,
        "main", "feature", "9" * 40,
    )]
    with pytest.raises(RuntimeError, match="branch and commit"):
        adapter.find_pull_request(_request(), CancellationToken())


def test_publication_registry_routes_without_node_provider_conditionals() -> None:
    adapter = _adapter()
    registry = PublicationAdapterRegistry(pull_request={"github": adapter})

    assert registry.pull_request("github") is adapter
    assert registry.pull_request("gitlab") is None
    assert registry.push("github") is None
    with pytest.raises(ValueError, match="does not match provider"):
        PublicationAdapterRegistry(pull_request={"gitlab": adapter})


def test_github_rest_api_owns_bearer_token_and_bounds_response(monkeypatch) -> None:
    captured = {}
    payload = [{
        "number": 7, "html_url": "https://github.com/acme/repo/pull/7",
        "title": "Change", "body": "Body",
        "base": {"ref": "main"}, "head": {"ref": "feature", "sha": COMMIT},
    }]

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _limit): return json.dumps(payload).encode("utf-8")

    def urlopen(request, timeout):
        captured.update(request=request, timeout=timeout)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    api = GitHubRESTAPI("secret-token", timeout_seconds=12)
    result = api.list_pull_requests(
        GitHubRepository("acme", "repo"), head="feature", base="main",
    )

    request = captured["request"]
    assert request.full_url.startswith(
        "https://api.github.com/repos/acme/repo/pulls?",
    )
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert request.get_header("X-github-api-version") == "2022-11-28"
    assert captured["timeout"] == 12
    assert result[0].number == 7
    assert "secret-token" not in repr(api)


def test_github_http_error_redacts_credential(monkeypatch) -> None:
    def urlopen(*_args, **_kwargs):
        raise __import__("urllib.error").error.HTTPError(
            "https://api.github.com", 401, "bad", {},
            io.BytesIO(b'{"message":"token secret-token rejected"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    api = GitHubRESTAPI("secret-token")
    with pytest.raises(RuntimeError) as raised:
        api.list_pull_requests(
            GitHubRepository("acme", "repo"), head="feature", base="main",
        )
    assert "secret-token" not in str(raised.value)
    assert "[redacted]" in str(raised.value)


@pytest.mark.parametrize(
    ("status", "error_class"),
    [(429, "rate_limit"), (503, "service_unavailable")],
)
def test_github_transient_http_errors_are_typed(
    monkeypatch, status: int, error_class: str,
) -> None:
    def urlopen(*_args, **_kwargs):
        raise __import__("urllib.error").error.HTTPError(
            "https://api.github.com", status, "temporary", {},
            io.BytesIO(b"temporary"),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    api = GitHubRESTAPI("secret-token")

    with pytest.raises(WorkflowTransientError) as raised:
        api.list_pull_requests(
            GitHubRepository("acme", "repo"), head="feature", base="main",
        )

    assert raised.value.error_class == error_class
