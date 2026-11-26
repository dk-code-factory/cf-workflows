"""Installed side of cf-workflows:dk-pr-iter (#24): re-check the route preflight chose.

workflows/iter.yml runs `iter_route` (a retained preparation operation) before
either entry. Host-side routing (cf_workflows/routing.py) already compared
immutable numeric account IDs and staged the routed request; preflight passes
the chosen route and the two IDs it compared. This operation reads the PR and
the authenticated account once each, and continues only when both IDs are
unchanged and the staged request still fits the route:

- `fix` needs the actor's own PR (by ID, or a configured personal account on
  the PR's host) or the explicit `maintain_other` request routing staged;
- `review` never carries `maintain_other`, and on the actor's own PR it never
  carries an approval grant or the `approve_pr` permission.

A token for another account, a changed PR, a failed read or a mismatched
request rejects before any session or effect. Nothing is written remotely; the
selected entry's own preflight checks still run again inside main.yml.
"""
from .core import identity, text

ROUTES = ("fix", "review")


class GitHubIdentity:
    """The two read-only identity reads over the installed gh transport (capture.GitHub)."""

    def __init__(self, transport):
        self.transport = transport

    def pull_request(self, url):
        pr = identity(url)
        self.transport.host = pr["host"]
        return self.transport.request("GET", "repos/%s/%s/pulls/%d" % (pr["owner"], pr["name"], pr["number"]))

    def viewer(self, host):
        self.transport.host = host
        return self.transport.request("GET", "user")


def identity_provider(root, provider):
    """The injected identity reads, or the installed gh transport."""
    if provider is not None:
        return provider
    from .capture import GitHub
    return GitHubIdentity(GitHub(root))


def _account(value, label):
    if (not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] <= 0):
        raise ValueError(label + " has no immutable numeric ID")
    return value["id"]


def _recorded(value):
    if not isinstance(value, dict) or set(value) != {"author", "actor"}:
        raise ValueError("routed identity needs exactly the author and actor IDs preflight compared")
    for key in ("author", "actor"):
        if type(value[key]) is not int or value[key] <= 0:
            raise ValueError("routed " + key + " ID must be a positive integer")
    return value


def _accounts(value):
    if not isinstance(value, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("host"), str)
            or type(item.get("id")) is not int or item["id"] <= 0 for item in value):
        raise ValueError("accounts must be {host, id} records with numeric IDs")
    return value


def route_operation(provider, request, inputs):
    """Confirm the staged route against current identities; read-only."""
    route = text(inputs["route"], "route")
    if route not in ROUTES:
        raise ValueError("dk-pr-iter routes to fix or review only")
    recorded = _recorded(inputs["identity"])
    accounts = _accounts(inputs["accounts"])
    permissions = inputs["permissions"]
    if not isinstance(permissions, dict):
        raise ValueError("permissions must be an object")
    pr = identity(request["pr"])
    try:
        pull = provider.pull_request(pr["url"])
        viewer = provider.viewer(pr["host"])
    except Exception as error:  # Any read failure rejects; the route is never assumed.
        raise ValueError("PR author or authenticated account identity is unavailable: "
                         + type(error).__name__) from None
    try:
        same = (isinstance(pull, dict) and identity(pull.get("html_url")) == pr
                and pull.get("number") == pr["number"])
    except ValueError:
        same = False
    if not same:
        raise ValueError("GitHub returned another PR")
    author = _account(pull.get("user"), "PR author")
    actor = _account(viewer, "authenticated account")
    if (author, actor) != (recorded["author"], recorded["actor"]):
        raise ValueError("the PR author or authenticated account differs from the identities routing "
                         "compared; route the request again")
    owned = author == actor or any(item["host"].lower() == pr["host"] and item["id"] == author
                                   for item in accounts)
    other = request.get("maintain_other")
    if route == "fix" and not owned and not other:
        raise ValueError("fixing another author's PR needs the explicit maintain_other request routing staged")
    if route == "review":
        if other:
            raise ValueError("incoming review never maintains another author's PR")
        if owned and ("approve_pr" in request.get("authorizations", {}) or permissions.get("approve_pr")):
            raise ValueError("the authenticated account never approves its own PR")
    return {"route": route, "ownership": "mine" if owned else "others",
            "identity": {"author": author, "actor": actor}}
