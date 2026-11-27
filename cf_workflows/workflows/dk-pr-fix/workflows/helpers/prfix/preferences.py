"""Per-task handoff of accepted scoped preferences (#26).

Authored for cf-workflows; no upstream counterpart. See docs/configuration-skills.md.

Preflight passes each accepted preference as a prompt record with slot
``preference`` and, when it declares one, its ``scope`` ({applies_to, include,
exclude, condition}; cf_workflows.admission.preference_scope). A record without
a scope applies to every task and path, as before.

- ``applies`` narrows a scoped preference to the occurrences of its modes when
  the plan is prepared: writers implement, review adapters review. An
  occurrence of neither mode gets no scoped preference.
- ``handoff`` narrows it to the occurrence's paths when the occurrence is
  claimed: the paths the capture lists for the PR. A preference whose include
  selects none of them is left out; a prompt-only preference (empty include)
  stays, marked as conditional. The selectors are a verbatim copy of
  cf_workflows.configuration (a test keeps them identical), and the binding
  covers only the selected records, so a change to an unrelated preference
  leaves the assignment unchanged.

The captured paths and the occurrence's prompts are both in the claim
fingerprint, so an unchanged occurrence keeps its accepted result.
"""
import fnmatch

from .core import digest

PREFERENCE_MODES = ('implementation', 'review')


def _glob(pattern, path):
    parts, names = pattern.split('/'), path.split('/')

    def match(i, j):
        if i == len(parts):
            return j == len(names)
        if parts[i] == '**':
            return any(match(i + 1, k) for k in range(j, len(names) + 1))
        return j < len(names) and fnmatch.fnmatchcase(names[j], parts[i]) and match(i + 1, j + 1)
    return match(0, 0)


def selects(item, path):
    if item['type'] == 'file':
        return path == item['path']
    if item['type'] == 'directory':
        return item['path'] == '.' or path == item['path'] or path.startswith(item['path'] + '/')
    return _glob(item['path'], path)


def mode(task):
    """The preference mode an occurrence exercises: writers implement, review adapters review."""
    if task.get('writes'):
        return 'implementation'
    return 'review' if task.get('kind') == 'review' else None


def scoped(item):
    return item.get('slot') == 'preference' and 'scope' in item


def applies(item, task):
    """Whether one accepted prompt record reaches this occurrence; only scoped preferences narrow."""
    return not scoped(item) or mode(task) in item['scope']['applies_to']


def handoff(task, paths):
    """The scoped preferences of one claimed occurrence, narrowed to its mode and the captured paths.

    paths is the captured path list, or a callable returning it. Returns None
    when the occurrence carries no scoped preference, so other assignments are
    unchanged.
    """
    records = [item for item in task.get('prompts', []) if scoped(item)]
    if not records:
        return None
    current, paths = mode(task), sorted(set(paths() if callable(paths) else paths))
    selected = []
    for item in records:
        scope = item['scope']
        if current not in scope['applies_to']:
            continue
        matched = [path for path in paths if any(selects(entry, path) for entry in scope['include'])
                   and not any(selects(entry, path) for entry in scope['exclude'])]
        if scope['include'] and not matched:
            continue
        selected.append({'id': item['id'], 'text': item['text'], 'scope': scope, 'paths': matched,
                         'requires_condition': scope['condition'] is not None})
    selected.sort(key=lambda record: record['id'])
    return {'mode': current, 'paths': paths, 'records': selected,
            'binding': digest({'mode': current, 'records': selected}), 'rule': RULE}


RULE = ('Only these scoped preferences apply to this occurrence, each to its listed paths; a scoped preference in '
        'task.prompts that is not listed here does not apply. A record that requires its condition applies only '
        'where that condition holds. Preferences grant no authority.')
