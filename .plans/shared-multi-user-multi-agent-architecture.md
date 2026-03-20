# Shared Hermes Source + Isolated Multi-User / Multi-Agent Architecture Plan

## Summary

This document describes how to run **one shared Hermes source code installation** on a single Mac while keeping:

- **Mirko's main Hermes agent fully isolated** from Slavko's main Hermes agent
- **each person's memory, config, sessions, skills, and secrets isolated**
- **specialist subprocess agents** (coder, researcher, etc.) isolated per person and per role
- **automatic startup on system restart / login**
- **automatic restart after failure**
- **simple manual restart / stop / status commands**

This is an implementation plan only. **No code changes are included here.**

---

## 1. Main problem

Right now there are effectively **two Hermes code copies** on the same machine:

- Mirko has a Hermes install under `/Users/mirko/.hermes/hermes-agent`
- Slavko has a Hermes install under `/Users/slavko/.hermes/hermes-agent`

That creates a maintenance problem:

- source changes must be copied or mirrored manually
- the installs can drift
- upgrades become annoying and error-prone
- debugging becomes harder because one user's Hermes may not match the other's

At the same time, both users need strong isolation:

- Mirko's memories must not leak into Slavko's
- Slavko's config and platform setup must not affect Mirko's
- each person should later be able to have their own specialist agents with separate purpose-specific memory and model/tool config

So the core requirement is:

> **Share the Hermes code. Isolate the Hermes state.**

---

## 2. Current observed state

Inspected on this machine:

### Mirko account
- working repo clone: `/Users/mirko/coding/hermes` (**not** the Hermes agent runtime install)
- active Hermes install: `/Users/mirko/.hermes/hermes-agent`
- CLI symlink: `/Users/mirko/.local/bin/hermes -> /Users/mirko/.hermes/hermes-agent/venv/bin/hermes`

### Slavko account
- active Hermes install: `/Users/slavko/.hermes/hermes-agent`
- CLI symlink: `/Users/slavko/.local/bin/hermes -> /Users/slavko/.hermes/hermes-agent/venv/bin/hermes`

### Relevant Hermes behavior confirmed from source
Hermes already supports isolating state via:

- `HERMES_HOME`

If `HERMES_HOME` is not set, Hermes defaults to:

- `~/.hermes`

So Hermes is already structurally compatible with the desired design.

---

## 3. Target architecture

## 3.1 Core design

Use:

- **one shared Hermes source tree + one shared venv**
- **separate `HERMES_HOME` directories for every independent agent identity**

That gives:

- one codebase to maintain
- one set of upgrades
- one set of code changes
- many isolated agent states

---

## 3.2 Human-level agents

There will be **one main manager agent per human user**.

### Mirko
- main manager agent
- owns Mirko's conversations
- delegates work to Mirko's specialist agents

### Slavko
- main manager agent
- owns Slavko's conversations
- delegates work to Slavko's specialist agents

These two main agents must remain fully isolated.

---

## 3.3 Specialist subprocess agents per user

Each human user can have dedicated specialist agents.

Example layout:

### Mirko
- `manager`
- `coder`
- `researcher`
- optional later: `ops`, `writer`, `planner`

### Slavko
- `manager`
- `coder`
- `researcher`
- optional later: `ops`, `writer`, `planner`

Each specialist agent should have its own:

- `HERMES_HOME`
- config
- model/provider
- enabled tools / toolsets
- memory
- skills
- logs
- sessions

So even **Mirko's coder** and **Mirko's researcher** should not share the same memory store unless explicitly intended.

---

## 4. Recommended filesystem layout

## 4.1 Shared code

Canonical shared runtime install:

```text
/Users/Shared/hermes-agent/
```

Suggested contents:

```text
/Users/Shared/hermes-agent/
├── .git/
├── venv/
├── hermes
├── hermes_cli/
├── gateway/
├── tools/
├── ...
```

This becomes the **single runtime source of truth**.

### Ownership recommendation
Preferred:
- owner: Mirko
- group: a shared local group containing both Mirko and Slavko
- permissions: readable/executable by both users

Practical alternative:
- Mirko owns the directory
- Slavko gets read + execute access
- only Mirko performs upgrades/edits

This is simpler and safer than allowing both users to edit the shared runtime tree.

---

## 4.2 Per-user, per-agent state directories

Use separate homes like this:

### Mirko
```text
/Users/mirko/.hermes-manager
/Users/mirko/.hermes-coder
/Users/mirko/.hermes-researcher
```

### Slavko
```text
/Users/slavko/.hermes-manager
/Users/slavko/.hermes-coder
/Users/slavko/.hermes-researcher
```

Each of those directories will contain its own:

```text
config.yaml
.env
SOUL.md
logs/
sessions/
memories/
cron/
skills/
```

Important: the default human-facing main agent should use a clearly named home such as:

- `/Users/mirko/.hermes-manager`
- `/Users/slavko/.hermes-manager`

rather than overloading plain `~/.hermes`, because that makes the multi-agent design explicit and easier to reason about.

---

## 4.3 Optional convenience wrappers per user

To avoid typing long commands, each user can have wrapper scripts in `~/bin/`.

Example for Mirko:

```text
~/bin/hermes-manager
~/bin/hermes-coder
~/bin/hermes-researcher
```

Example for Slavko:

```text
~/bin/hermes-manager
~/bin/hermes-coder
~/bin/hermes-researcher
```

Each wrapper would do nothing fancy: just set `HERMES_HOME` and exec the shared Hermes binary.

This keeps operations clean and avoids mistakes.

---

## 5. Why this design is correct

## 5.1 Code is shared
Benefits:

- one upgrade path
- one patch location
- one git history
- no manual mirroring between users
- lower chance of drift

## 5.2 State is isolated
Benefits:

- separate memory per person
- separate memory per specialist role
- separate platform configs and home channels
- different models per agent
- different tool settings per agent
- easier debugging because each agent state is scoped to one purpose

## 5.3 Subagent collaboration stays clean
Recommended collaboration model:

- manager delegates to specialist agents
- specialist agents do not directly maintain long free-form chats with each other
- manager passes context between specialists when needed
- shared files can be used as artifacts when necessary

This avoids chaotic peer-to-peer multi-agent behavior.

---

## 6. Important macOS service-management constraint

Hermes already has built-in gateway service support via:

```bash
hermes gateway install
hermes gateway start
hermes gateway stop
hermes gateway restart
hermes gateway status
```

However, in the current source, the **macOS launchd label/path is fixed to one service per user**:

- label: `ai.hermes.gateway`
- plist path: `~/Library/LaunchAgents/ai.hermes.gateway.plist`

That means:

- the built-in `hermes gateway install` flow is fine for **one default gateway service per user**
- it is **not sufficient by itself** for multiple named agent services under the same macOS user account

Also, the generated launchd plist does **not currently inject a custom `HERMES_HOME`**. So for multi-agent-per-user services, custom launchd plists are the right approach.

### Conclusion
For this architecture on macOS:

- use the shared Hermes codebase
- use **custom launchd plists** per agent identity
- set `HERMES_HOME` explicitly in each plist
- use launchd `RunAtLoad` + `KeepAlive` for auto-start and crash recovery

This avoids needing code changes up front.

---

## 7. Recommended rollout model

## Phase 1 — shared code + two main agents
First stabilize the basics:

- one shared Hermes runtime at `/Users/Shared/hermes-agent`
- Mirko main manager agent service
- Slavko main manager agent service

This alone solves the code duplication problem.

## Phase 2 — add on-demand specialist subprocesses
After the main agents are stable:

- create Mirko coder/researcher homes
- create Slavko coder/researcher homes
- run them on demand first using wrappers / subprocess commands

This is the safest and simplest way to validate the architecture.

## Phase 3 — optional always-on specialist services
Only if needed later:

- install dedicated launchd services for always-on coder/researcher agents
- keep their identities and home directories separate

This is optional. Most specialist agents should probably be **spawned on demand** by the manager rather than running 24/7.

---

## 8. Proposed runtime identities

## 8.1 Main manager services
These should auto-run.

### Mirko
- service label: `ai.hermes.mirko.manager`
- `HERMES_HOME=/Users/mirko/.hermes-manager`

### Slavko
- service label: `ai.hermes.slavko.manager`
- `HERMES_HOME=/Users/slavko/.hermes-manager`

These are the primary always-on messaging agents.

---

## 8.2 Specialist agent identities
These should start as on-demand subprocess agents.

### Mirko
- `ai.hermes.mirko.coder`
- `ai.hermes.mirko.researcher`

### Slavko
- `ai.hermes.slavko.coder`
- `ai.hermes.slavko.researcher`

Each maps to its own `HERMES_HOME`.

---

## 9. Shared source implementation plan

## Step 1: Pick the source of truth
Use **Mirko's current Hermes runtime source** as the canonical shared source.

Source to migrate from:

```text
/Users/mirko/.hermes/hermes-agent
```

Destination:

```text
/Users/Shared/hermes-agent
```

Rationale:

- it is already the actively used install
- it is the cleanest candidate for becoming the shared runtime
- it avoids using Slavko's install as the canonical base

---

## Step 2: Move or clone the shared runtime
Preferred clean approach:

1. copy or move Mirko's active Hermes install to `/Users/Shared/hermes-agent`
2. confirm the shared venv works from there
3. update each user's PATH/symlink/wrapper to point at the shared binary
4. leave old per-user installs untouched until validation is complete
5. remove old duplicate runtime trees only after successful validation

Safer migration rule:

> Do not delete the old installs until both users can successfully run the shared runtime with isolated `HERMES_HOME` values.

---

## Step 3: Repoint both users to the shared binary
Both users should execute the same binary, e.g.:

```bash
/Users/Shared/hermes-agent/venv/bin/hermes
```

Possible methods:

### Option A — per-user symlink
Each user's `~/.local/bin/hermes` points to the shared binary.

### Option B — per-user wrapper script
Each user keeps a wrapper script that execs the shared binary.

### Recommendation
Use:
- a plain shared `hermes` symlink for convenience
- separate wrapper scripts for named agent identities (`hermes-manager`, `hermes-coder`, etc.)

---

## 10. Per-user manager-agent setup

## 10.1 Mirko manager
Primary home:

```bash
HERMES_HOME=/Users/mirko/.hermes-manager
```

Example manual run:

```bash
HERMES_HOME=/Users/mirko/.hermes-manager /Users/Shared/hermes-agent/venv/bin/hermes
```

Example gateway foreground run:

```bash
HERMES_HOME=/Users/mirko/.hermes-manager /Users/Shared/hermes-agent/venv/bin/hermes gateway run
```

## 10.2 Slavko manager
Primary home:

```bash
HERMES_HOME=/Users/slavko/.hermes-manager
```

Example manual run:

```bash
HERMES_HOME=/Users/slavko/.hermes-manager /Users/Shared/hermes-agent/venv/bin/hermes
```

Example gateway foreground run:

```bash
HERMES_HOME=/Users/slavko/.hermes-manager /Users/Shared/hermes-agent/venv/bin/hermes gateway run
```

These are the two main human-facing agents.

---

## 11. Specialist subprocess setup

## 11.1 On-demand subprocess principle
The manager agent should spawn specialist agents using the same shared Hermes binary but a different `HERMES_HOME`.

Example for Mirko coder:

```bash
HERMES_HOME=/Users/mirko/.hermes-coder /Users/Shared/hermes-agent/venv/bin/hermes chat -q "Inspect this repo and write a code review summary to /tmp/review.md"
```

Example for Mirko researcher:

```bash
HERMES_HOME=/Users/mirko/.hermes-researcher /Users/Shared/hermes-agent/venv/bin/hermes chat -q "Research the best approach and summarize findings"
```

Same pattern for Slavko with `/Users/slavko/...` homes.

---

## 11.2 Specialist identity configuration
Each specialist should have a purpose-specific setup.

### Coder agent
Should typically have:
- coding-focused model
- coding-related skills
- strong terminal/file tool access
- narrower soul/system prompt focused on coding execution

### Researcher agent
Should typically have:
- research/synthesis-focused model
- web/research skills
- web access emphasized
- output format oriented around findings + sources + recommendations

The important thing is not the persona fluff. The important thing is:

- separate home
- separate config
- separate model
- separate tools
- separate memory

---

## 12. Auto-start and auto-restart strategy

## 12.1 Main recommendation
Use **launchd user agents** on macOS for the always-on manager agents.

Each service should have:

- `RunAtLoad = true`
- `KeepAlive = true` or equivalent restart policy
- explicit `HERMES_HOME`
- explicit working directory pointing to `/Users/Shared/hermes-agent`
- stdout/stderr logs written under the agent's own `HERMES_HOME/logs/`

This guarantees:

- starts automatically on login / restart
- restarts if it crashes
- logs are isolated per agent

---

## 12.2 Recommended always-on scope
Always-on by default:

- Mirko manager
- Slavko manager

Optional later:

- Mirko coder
- Mirko researcher
- Slavko coder
- Slavko researcher

Recommendation: keep specialists **on-demand first**. Only promote a specialist to always-on if there is a concrete reason.

---

## 13. Launchd service plan

## 13.1 Service labels
Use distinct labels like:

```text
ai.hermes.mirko.manager
ai.hermes.slavko.manager
ai.hermes.mirko.coder
ai.hermes.mirko.researcher
ai.hermes.slavko.coder
ai.hermes.slavko.researcher
```

## 13.2 Plist locations
Per user:

### Mirko
```text
/Users/mirko/Library/LaunchAgents/ai.hermes.mirko.manager.plist
/Users/mirko/Library/LaunchAgents/ai.hermes.mirko.coder.plist
/Users/mirko/Library/LaunchAgents/ai.hermes.mirko.researcher.plist
```

### Slavko
```text
/Users/slavko/Library/LaunchAgents/ai.hermes.slavko.manager.plist
/Users/slavko/Library/LaunchAgents/ai.hermes.slavko.coder.plist
/Users/slavko/Library/LaunchAgents/ai.hermes.slavko.researcher.plist
```

## 13.3 Service command shape
Each plist should launch the shared Hermes binary, for example:

```bash
/Users/Shared/hermes-agent/venv/bin/python -m hermes_cli.main gateway run --replace
```

with environment variables including:

- `HERMES_HOME=/Users/<user>/.hermes-<role>`

and with:

- `WorkingDirectory=/Users/Shared/hermes-agent`

---

## 14. Manual run and management commands

The final implementation should document and standardize these commands.

## 14.1 Manual foreground runs

### Mirko manager
```bash
HERMES_HOME=/Users/mirko/.hermes-manager /Users/Shared/hermes-agent/venv/bin/hermes gateway run
```

### Slavko manager
```bash
HERMES_HOME=/Users/slavko/.hermes-manager /Users/Shared/hermes-agent/venv/bin/hermes gateway run
```

### Mirko coder
```bash
HERMES_HOME=/Users/mirko/.hermes-coder /Users/Shared/hermes-agent/venv/bin/hermes
```

### Mirko researcher
```bash
HERMES_HOME=/Users/mirko/.hermes-researcher /Users/Shared/hermes-agent/venv/bin/hermes
```

### Slavko coder
```bash
HERMES_HOME=/Users/slavko/.hermes-coder /Users/Shared/hermes-agent/venv/bin/hermes
```

### Slavko researcher
```bash
HERMES_HOME=/Users/slavko/.hermes-researcher /Users/Shared/hermes-agent/venv/bin/hermes
```

---

## 14.2 Recommended service control commands
Because the macOS built-in Hermes launchd service name is currently fixed, the final implementation should manage custom multi-agent plists via `launchctl` directly.

### Load service
```bash
launchctl load ~/Library/LaunchAgents/ai.hermes.mirko.manager.plist
```

### Unload service
```bash
launchctl unload ~/Library/LaunchAgents/ai.hermes.mirko.manager.plist
```

### Start service
```bash
launchctl start ai.hermes.mirko.manager
```

### Stop service
```bash
launchctl stop ai.hermes.mirko.manager
```

### Restart service
Preferred restart sequence:
```bash
launchctl stop ai.hermes.mirko.manager
launchctl start ai.hermes.mirko.manager
```

If plist changed:
```bash
launchctl unload ~/Library/LaunchAgents/ai.hermes.mirko.manager.plist
launchctl load ~/Library/LaunchAgents/ai.hermes.mirko.manager.plist
launchctl start ai.hermes.mirko.manager
```

### Check status
```bash
launchctl list | grep ai.hermes.mirko.manager
```

### Tail logs
```bash
tail -f /Users/mirko/.hermes-manager/logs/gateway.log
```

Equivalent commands should exist for Slavko and any specialist service labels.

---

## 15. Failure recovery expectations

The service design should guarantee:

- if the machine restarts, the manager agents come back automatically
- if a Hermes manager process exits unexpectedly, launchd restarts it
- if a plist changes, unload/load/start restores the service cleanly
- logs remain attached to the correct `HERMES_HOME`

For specialist agents:

- on-demand subprocesses do not need launchd recovery because the manager can respawn them
- always-on specialists, if later enabled, should get the same launchd recovery policy

---

## 16. Configuration isolation details

Every agent identity should maintain its own:

- `config.yaml`
- `.env`
- `SOUL.md`
- `skills/`
- `memories/`
- `sessions/`
- `logs/`
- `cron/`

This is what makes the architecture truly isolated.

### Example consequence
Mirko's coder can use:
- one model
- coding-specific skills
- terminal-heavy permissions

while Mirko's researcher can use:
- another model
- research skills
- more web-focused configuration

without either contaminating the other's memory.

---

## 17. Security and permissions guidance

## 17.1 Shared source permissions
The shared source tree must be:

- readable/executable by both users
- writable only by the user responsible for upgrades, unless both intentionally co-maintain it

Recommendation:
- keep the shared source runtime effectively administrator-controlled
- keep per-user state private to that user

## 17.2 State permissions
Each `HERMES_HOME` should remain owned by its respective user and not be shared.

That means:
- Mirko should not rely on reading `/Users/slavko/.hermes-*`
- Slavko should not rely on reading `/Users/mirko/.hermes-*`

This preserves privacy and clean ownership.

---

## 18. Migration procedure plan

## Phase A — prepare shared runtime
1. verify Mirko's current Hermes install is the desired source of truth
2. stage it into `/Users/Shared/hermes-agent`
3. validate the shared binary runs
4. do **not** remove old installs yet

## Phase B — create isolated manager homes
1. create `/Users/mirko/.hermes-manager`
2. create `/Users/slavko/.hermes-manager`
3. initialize each with its own config/env/secrets/platform setup
4. run each manager manually with explicit `HERMES_HOME`
5. confirm isolation

## Phase C — repoint CLI entrypoints
1. repoint Mirko's Hermes entrypoint to the shared runtime
2. repoint Slavko's Hermes entrypoint to the shared runtime
3. verify both users execute the same codebase

## Phase D — install manager launchd services
1. add custom launchd plist for Mirko manager
2. add custom launchd plist for Slavko manager
3. load and start both
4. reboot/login-test and crash-restart-test

## Phase E — add specialist homes
1. create coder/researcher homes for Mirko
2. create coder/researcher homes for Slavko
3. configure models/tools/skills per role
4. validate manual subprocess invocation

## Phase F — optional specialist services
1. decide whether any specialists need to be always-on
2. if yes, create dedicated launchd plists per specialist
3. otherwise keep them on-demand

## Phase G — remove old duplicate installs
Only after everything is stable:
1. archive or remove `/Users/mirko/.hermes/hermes-agent`
2. archive or remove `/Users/slavko/.hermes/hermes-agent`
3. keep only the shared runtime

---

## 19. Validation checklist

The implementation is complete only when all of the following are true:

### Shared code
- [ ] both users run `/Users/Shared/hermes-agent/venv/bin/hermes`
- [ ] there is only one maintained runtime source tree
- [ ] upgrades only need to happen once

### User isolation
- [ ] Mirko main manager uses `/Users/mirko/.hermes-manager`
- [ ] Slavko main manager uses `/Users/slavko/.hermes-manager`
- [ ] memories are isolated
- [ ] sessions are isolated
- [ ] skills are isolated
- [ ] platform config is isolated

### Specialist isolation
- [ ] Mirko coder and researcher have separate homes
- [ ] Slavko coder and researcher have separate homes
- [ ] specialist models can differ by role
- [ ] specialist tools can differ by role
- [ ] specialist memories do not bleed together

### Operations
- [ ] manager services auto-start on login/restart
- [ ] manager services auto-restart on failure
- [ ] restart commands are documented and tested
- [ ] logs are easy to inspect

---

## 20. Recommended final operating model

### Always on
- Mirko manager
- Slavko manager

### On demand
- Mirko coder
- Mirko researcher
- Slavko coder
- Slavko researcher

### Optional later
If a specialist needs to monitor something continuously, promote that one specialist to an always-on launchd service.

This keeps the system simpler and avoids running unnecessary background processes.

---

## 21. Final recommendation

This is the right architecture:

- **one shared Hermes codebase** at `/Users/Shared/hermes-agent`
- **one isolated manager agent per human user**
- **multiple isolated specialist agents per user**
- **all isolation implemented through distinct `HERMES_HOME` directories**
- **launchd services for always-on manager agents**
- **on-demand subprocesses for specialist agents by default**

Most importantly, this removes the bad part of the current setup — duplicated source trees — without giving up the privacy and identity isolation you want.

---

## 22. Next implementation document / execution step

When moving from plan to implementation, the next task should produce:

1. exact shared-directory creation commands
2. exact symlink/wrapper creation commands
3. exact per-user `HERMES_HOME` directory creation commands
4. exact launchd plist templates for Mirko manager and Slavko manager
5. exact verification commands
6. exact rollback steps

That should be done as a separate implementation pass so the migration can be executed safely and incrementally.
