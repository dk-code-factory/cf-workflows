---
name: dk-config
description: Explain saved cf-workflows settings, or change settings, permissions, prompts, prompt resources and saved entry options from an exact request through the shared admission.
---

This plugin version is not runnable. It needs a released engine of the exact
pinned version with engine-managed native sessions (agent-workflow#129),
durable input gates and verified setup (agent-workflow#130). Do not copy the
workflow into the target repository, and do not run it with any other engine.

An explanation is one transient session that cites existing configuration
paths. It has no request ID, history or gate.

A change binds the exact request to a durable request ID. One session with the
admitted leaf role and no skills returns exact typed operations, concrete
questions (one durable gate; an interrupted wait opens no new session, and the
answer resumes the request in a new session with a new claim) or a cancellation.
The host applies only this entry's operations, admits the exact revision
through the shared structural and semantic admission, and writes exactly that
revision once through the suite's single configuration writer, then reads it
back. Earlier uncertain writes are read back first. A concurrent change
settles the stale proposal as not applied. The installed workflow itself
writes nothing.

A saved permission never authorizes an action: each request still needs its
own authorization. Nothing is imported from an earlier installation; restate an
old setting as a new request.

Never log in, refresh or copy credentials, use an engine found on PATH or grant
write authority.
