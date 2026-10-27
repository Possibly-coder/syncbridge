import httpx
import pytest

from app.connectors.base import RetryableError
from app.connectors.github import GitHubConnector


def issue(number, updated, pr=False, labels=()):
    data = {"number": number, "title": "Issue {}".format(number), "state": "open",
            "labels": [{"name": name} for name in labels], "assignee": {"login": "octocat"},
            "html_url": "https://github.com/o/r/issues/{}".format(number), "updated_at": updated}
    if pr:
        data["pull_request"] = {"url": "..."}
    return data


def test_follows_link_header_and_advances_since():
    seen = []

    def handler(request):
        seen.append(request.url)
        if "page=2" in str(request.url):
            return httpx.Response(200, json=[issue(3, "2026-10-03T00:00:00Z", pr=True)])
        return httpx.Response(
            200,
            json=[issue(1, "2026-10-01T00:00:00Z", labels=["bug", "priority: high"]), issue(2, "2026-10-02T00:00:00Z")],
            headers={"Link": '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    gh = GitHubConnector("o", "r", since="2026-09-30T00:00:00Z", http=http)

    first = gh.fetch_page({})
    assert not first.done
    assert first.items[0].priority == "priority: high"
    assert seen[0].params["since"] == "2026-09-30T00:00:00Z"
    assert seen[0].params["direction"] == "asc"

    second = gh.fetch_page(first.cursor)
    assert second.done
    assert second.items[0].kind == "pull_request"
    assert second.cursor["since"] == "2026-10-03T00:00:00Z"
    assert second.cursor["next_url"] is None


def test_exhausted_quota_403_is_retryable():
    def handler(request):
        return httpx.Response(403, json={"message": "API rate limit exceeded"},
                              headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    with pytest.raises(RetryableError) as exc:
        GitHubConnector("o", "r", http=http).fetch_page({})
    assert exc.value.retry_after == 1.0
