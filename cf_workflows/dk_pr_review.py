"""Installed-resource preflight for cf-workflows:dk-pr-review (incoming review).

Incoming review is a preset over the shared installed workflow: its entry
workflows/review.yml calls workflows/main.yml in review mode. Reviewers and
the simplification reviewer run as engine-managed sessions with the admitted
reviewer role, a separate verifier session checks every finding, and nothing
is fixed. This module only selects that preset and admits it through the same
preflight as dk-pr-fix; it starts no session and runs nothing.
"""
from . import admission, dk_pr_fix

WORKFLOW = 'cf-workflows:dk-pr-review'
ENTRY = 'workflows/review.yml'
# Bundled defaults for this entry point. Configured scopes still apply above them.
DEFAULTS = {'tasks': ['read-feedback', {'stage': 'reviews'}], 'finalizers': [],
            'reviewers': [['review', admission.SIMPLIFIER]]}


def logical_run(pr):
    """The default logical run of incoming review, distinct from the fix workflow's run of the same PR."""
    return 'review_' + admission.digest(admission.pr_identity(pr, 'request.pr'))[:32]


def preflight(*, request, configuration, invocation, deployment, repository, transport,
              roots=None, home=None, install=dk_pr_fix.INSTALL, custom=True):
    """Admit one incoming review of the installed workflow; writes nothing.

    custom=False rejects custom inputs for a preset entry over review.yml that does not carry them.
    """
    if isinstance(request, dict) and 'logical_run' not in request:
        try:
            request = {**request, 'logical_run': logical_run(request.get('pr'))}
        except admission.AdmissionError:
            pass  # Admission reports the malformed PR identity.
    return dk_pr_fix.preflight(request=request, configuration=configuration, invocation=invocation,
                               deployment=deployment, repository=repository, transport=transport, roots=roots,
                               home=home, install=install, mode='review', defaults=DEFAULTS,
                               workflow=WORKFLOW, entry=ENTRY, custom=custom)
