---
name: dk-preferences
description: Add, update or remove personal and repository preferences for implementation and review, with concrete scopes and contradiction checks before saving.
---

This plugin version is not runnable. It needs a released engine of the exact
pinned version with engine-managed native sessions (agent-workflow#129),
durable input gates and verified setup (agent-workflow#130). Do not copy the
workflow into the target repository, and do not run it with any other engine.

One session with the admitted leaf role and no skills proposes exact typed
preference operations in personal scopes or the target repository's scope. New
records get explicit `implementation`/`review` modes (the `default_applies_to`
option) and stable IDs; an update keeps its saved modes. A preference that may
overlap another applicable one needs a compatible reason or the user's recorded
answer: otherwise one durable gate asks, and nothing is saved. No precedence is
invented.

The host admits the exact revision through the shared structural and semantic
admission and writes it once through the suite's single configuration writer,
then reads it back. The installed workflow itself writes nothing. Preferences
grant no execution or publication authority.

Never log in, refresh or copy credentials, use an engine found on PATH or grant
write authority.
