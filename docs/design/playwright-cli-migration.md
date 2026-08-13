# Migration: Playwright MCP + Kiro Crew proxy to Playwright CLI

Status: proposed. Replaces the browser stack wholesale rather than adding a
second path, because two browser backends would double the surface that already
produces the defects this migration retires.

## Why

Today an agent drives a browser through `@playwright/mcp` behind a Kiro Crew
proxy that exists to keep accessibility trees out of the model context. The
proxy earns its keep, but the surrounding machinery does not:

- The launcher is resolved at runtime through PATH and an `npx` fallback, so a
  gateway start depends on npm registry auth. When a token expires every
  `browser_*` tool disappears.
- Twenty-two MCP tool schemas are re-sent every request. Measured against the
  server this host actually runs: 22 tools, 14,080 bytes of schema, roughly
  3.5K to 5K tokens per turn that no compression can remove.
- The compressed outline still lands in the context on every snapshot.
- Browser Mode is a persistent on/off flag, and every write path has to be
  taught not to destroy operator config as it flips.

`@playwright/cli` (verified: v0.1.18) answers all four, and does so with
capabilities we currently hand-roll.

## Verified facts this plan rests on

Established by running the CLI on a developer host, not from documentation:

| Fact | Evidence |
|---|---|
| Snapshot goes to disk, stdout gets a path | `### Snapshot` / `- [Snapshot](.playwright-cli/page-<ts>.yml)`; 315 bytes on disk for example.com, ~250 chars of stdout per command |
| Dashboard can be served over HTTP | `show --port <n>` documented in `--help` as "start as a blocking http server on this port"; logs `Listening on http://localhost:45613` |
| Dashboard is loopback-only by default | `--host` "defaults to localhost"; a request from the host's LAN address fails to connect |
| **Default bind is IPv6 only** | `lsof` reports `IPv6 ... TCP localhost:45613 (LISTEN)`; `http://127.0.0.1:<port>/` fails outright. `--host 127.0.0.1` produces an IPv4 listener |
| **Root path answers 302, not 200** | `curl -o /dev/null -w %{http_code}` returns `302` on `/` |
| Dashboard includes remote control | Docs: "Live viewport with tab bar, navigation controls, and full remote mouse/keyboard input. Press Escape to release." |
| No capability gating exists | Docs, verbatim: "In the CLI all capabilities are always available -- there's no gating." |
| Skills install is agent-neutral and targetable | `install --skills` accepts `claude` (default) or `agents`; `--global` installs into the home directory instead of the workspace |
| Operator launch flags already have a home | Config schema carries `browser.launchOptions.args`, `launchOptions.proxy`, `contextOptions.viewport/locale/userAgent/storageState/permissions` |
| Install does not require a global install | `npm install -g @playwright/cli@latest` or `npx playwright-cli`; Node.js 20 or newer |

## What is deleted

| Component | Lines | Replaced by |
|---|---:|---|
| `mcp_playwright_proxy.py` | 1,284 | snapshot-to-disk, `screenshot`, `pdf` |
| `browser/setup.py` | 2,077 | `install --skills`, the CLI's own config file |
| `browser/command_bus.py` | 397 | dashboard remote input |
| `browser/cli.py` | 257 | `state-save`/`state-load`, `cookie-*` |
| `browser/auth.py` | 212 | same |
| `browser/screencast.py` | 91 | `show --port` (see the frontend section) |
| `test_browser_setup.py` | 2,288 | far smaller suite; most cases test machinery that stops existing |
| `test_browser_screencast.py` | 661 | same |
| `test_browser_native_routing.py` | 448 | same |
| `test_mcp_playwright_proxy.py` | 432 | same |

Roughly 9,700 lines of code and tests. 115 files across `src/`, `website/src/`,
`docs/` and `test/` mention playwright and need a sweep.

Retired concepts, each of which currently has code and tests of its own:
`KIROCREW_PLAYWRIGHT_CMD`, the `npx` fallback, `playwright-config.json`
generation, `playwright-storage-state.json` assembly, the extension token file,
`playwright-extension-mode`, `browser-mode-enabled`, the four-value registration
status, the agent-shadow scan, and the entry-carryover sidecar.

## Consent model

The current capability gate is tool presence: with Browser Mode off the
`browser_*` tools are absent, so the capability does not exist for the model.
That mechanism does not survive the migration, and no CLI feature replaces it:
capabilities cannot be gated, and a binary on PATH is reachable from any shell
turn.

**Installation therefore becomes the gate.** Not present means the capability
does not exist; installing it is the operator's act of granting it. This is the
only coherent model available, not a preference.

**Decided:** an install Kiro Crew performs is consent by construction, the
operator's existing working setup is treated as consent so a migration never
silently disarms them, and **presence of `playwright-cli` on PATH is consent**
whether or not Kiro Crew installed it.

### Accepted risk

Presence-as-consent has one hole, accepted deliberately rather than overlooked.
An operator who installed `playwright-cli` for their own work has granted nothing,
yet the capability is armed. It cannot be narrowed after the fact:

- The CLI has no capability gating (verbatim: "In the CLI all capabilities are
  always available -- there's no gating"), so there is no subset to grant.
- `attach --extension` connects to the operator's own running Chrome, which
  carries their live logged-in sessions.
- A binary on PATH is reachable from any shell turn, so nothing in the tool
  surface can express the restriction.

The exposure is therefore: on such a host, an agent turn can drive a browser
holding the operator's authenticated sessions without the operator having said
yes to that. This is the price of removing the toggle, and the toggle is what
produced the defect class this migration retires.

Two mitigations are cheap and do NOT reintroduce a gate, and should ship with
Phase 2:

- State it in the docs and in the Settings surface, so presence-as-consent is
  discoverable rather than a surprise found by reading code.
- Make browser use visible after the fact. The dashboard panel showing a live
  session is itself the disclosure, and it is already in Phase 1.

## Approval: which commands run without a prompt

A browsing step is a shell command now, so without an allow path every `open`,
`click` and `snapshot` raises an approval prompt and browsing is unusable. The
allow path is deliberately NOT the bundled `auto_approve_tools` list, because
that list is matched against the tool *title*, which the model writes: an
injected agent could title `rm -rf /` as "playwright-cli snapshot" and be
auto-approved. Instead the check sits on the same command-keyed path the user's
own "always allow" grants use, and reads the real command out of `tool_input`.

The boundary is the page. A verb whose whole effect lands inside the browser
page or session is auto-approved; a verb that reaches the local machine is not:

| Kept interactive | Why |
|---|---|
| `eval`, `run-code` | run attacker-authored code in an authenticated page; with `fetch()` that is a complete exfiltration path |
| `upload` | sends an arbitrary LOCAL file to the current page |
| `state-load` | reads an arbitrary local path and injects its cookies into the live session |
| `state-save <path>`, `video-start <path>` | bare, these write inside the service's output dir; with an argument they are an arbitrary-path write |
| `install`, `install-browser` | mutate the machine; installing is the dashboard's job, not the agent's |

Three properties make this hold up rather than merely look careful:

- **Allowlists, not denylists**, for both verbs and flags. A verb added by a
  future CLI release is denied until someone reviews and lists it.
- **Flags are checked too.** The official docs use `screenshot --filename=<path>`,
  so a local path can arrive as a flag rather than a positional argument.
  Skipping unrecognized flags on the way to the verb would auto-approve an
  arbitrary local write; an unknown flag therefore denies the whole command.
- **One splitter.** The segment split (which rejects command substitution and
  requires every segment of a chained command to pass) is shared with the
  existing trusted-pattern path rather than written a second time, because a
  second shell splitter is how a bypass gets introduced.
- **Redirections are refused.** A redirection is the SHELL's work, so
  `playwright-cli snapshot > file` creates or truncates that file before the
  approved command runs, and the verb allowlist cannot see it: `>` and the path
  arrive as ordinary tokens. The check is quote-aware, because `click "div >
  span"` is a legitimate selector. This was found by review, not by design --
  the first version approved it.
- **Only a shell tool reaches this path.** `_extract_bash_command` reads a
  `command` field out of ANY tool input, so without an `is_shell` gate a
  non-shell tool that carries one (`cron_add`, which can schedule a shell
  command) would be auto-approved -- turning "browsing is allowed" into
  "creating a durable scheduled job is allowed".

The install and token endpoints refuse **app tokens** (403 + SEL), because route
scoping is not capability scoping: an app listing `/api/browser` in its manifest
would otherwise be able to install the binary that arms auto-approval, or replace
the attach token that silences the browser's own per-attach prompt.

Every auto-approval is recorded in the security event log with
`reason: "browser_cli"`, so the decision is auditable after the fact and is
distinguishable from a grant the user made themselves.

## Accepted limitation: npm is the only distribution channel

The capability rests on a package whose only official distribution is the npm
registry, and that is a real cost this migration accepts rather than solves:

- `@playwright/cli` is a Node program (`#!/usr/bin/env node`, Node 18+), so there
  is no way to drop a self-contained binary on a host. Bundling it would not
  remove that requirement, only the 19 MB download.
- The upstream GitHub release carries **no build assets**, so "download the
  standalone binary" is not an option that exists today.
- `pip install playwright` and the .NET tool install a DIFFERENT product (the
  `playwright` browser-installer / codegen CLI). They are not substitutes.
- yarn / pnpm / bun avoid the npm *client*, not the npm *registry*.

Who this hurts: an operator whose `.npmrc` points at a corporate registry that
does not mirror the package, and anyone with no Node toolchain at all. For the
first, the workaround is a user-prefix install against the public registry with
the binary symlinked onto `PATH` (documented in the `kirocrew-commands` skill,
including the two caveats: the bin dir must be on `PATH`, and overriding the
employer's registry config is the operator's decision). For the second, the panel
now names the remedy and links `nodejs.org` instead of only stating a version
requirement.

What would actually fix it is upstream: portable release archives with a bundled
Node runtime, checksums or Sigstore signatures so enterprises can mirror and
audit them, and OS package-manager entries. That is a reasonable feature request
to file against `microsoft/playwright-cli`, and it is deliberately out of scope
here.

## Snapshot files

The CLI writes a timestamped YAML per command into `.playwright-cli/` and
documents no pruning, so the directory grows without bound.

**Decided:** the gateway service prunes them on a schedule. Pruning belongs to a
long-lived component rather than to the agent, because the agent has no reason
to know the retention policy and a per-command prune would race the daemon.
The directory must therefore live at a path the service knows, not wherever an
agent's cwd happened to be. Snapshots are throwaway state, so retention is by
age and count, and the service must never delete a file the current session
still refers to.

**Decided:** the agent reads snapshot YAML directly with its own file tools. No
read-and-summarize layer. This is the whole point of the migration: the tree is
on disk, the stdout line carries the path, and the agent decides whether it
needs the file at all. A wrapper that read and summarized it would put the tree
back in the context and rebuild the proxy we are deleting.

## Install flow

**Decided: global install only.** `npx` re-resolves through the registry on
every invocation, which is precisely the fragility this migration removes: an
expired registry token would take browsing down again, exactly as it does today.
A global binary is resolved once at install time.

1. Detect: is `playwright-cli` on PATH, and is Node 20 or newer present?
2. If absent, offer the install: `npm install -g @playwright/cli@latest`.
3. `install-browser` for the browser binary (`--with-deps` on Linux). The CLI
   downloads one on first use, but an explicit step gives a progress surface and
   a failure the operator can see.
4. `install --skills agents --global` so the command reference is discoverable
   without occupying the system prompt.
5. Record that the install happened.

Registry auth still applies at install time, which is unavoidable for an npm
package. The improvement is that it applies once at install rather than on every
gateway start.

## Frontend: display and control

`show --port <n> --host 127.0.0.1` serves the dashboard over loopback HTTP, and
that dashboard already provides the session grid with live screencast, a session
detail view with tab bar and navigation, and full remote mouse and keyboard
input. It replaces both halves of what we maintain today: `useBrowserFrame` plus
`screencast.py` for display, and `command_bus.py` for control.

The panel embeds it in an iframe. Three findings must be honoured or this fails
in ways that look like a broken feature:

1. Bind with `--host 127.0.0.1` explicitly. The default listener is IPv6-only
   and an iframe pointed at `127.0.0.1` gets a connection failure.
2. Health-check for any response, not for 200. `/` answers 302.
3. `show --port` blocks, so it is a supervised child process with its own
   lifecycle, not a fire-and-forget call. `show --kill` stops the daemon.

Never pass `--host 0.0.0.0`: it would expose a fully interactive remote-input
browser view to the network.

## Existing installs

An operator on the current design has a `playwright-mcp` entry in
`~/.kiro/settings/mcp.json`, possibly a `KIROCREW_PLAYWRIGHT_CMD` pin, a
`playwright-config.json`, a storage-state file, and an extension token. The
migration must:

- Remove the canonical `playwright-mcp` entry it owns, and leave a user's own
  entry of that name alone.
- Carry `contextOptions`/`launchOptions` from the generated
  `playwright-config.json` into the CLI's config file, since the schema is the
  same shape, then stop owning that file.
- Point the CLI at the existing storage state rather than discarding it.
- Treat a working current setup as granted consent: an operator who has Browser
  Mode on today must not be silently disarmed.

## Phases

Each phase is its own PR and leaves the tree working.

1. **Adapter behind the existing surface.** Add the CLI driver and the
   supervised `show` process. No deletions. The dashboard panel switches to the
   iframe. Proves display and control before anything is removed.
2. **Install and consent.** Detection, the install action, the consent record,
   and the migration of an existing install.
3. **Cut over and delete.** Remove the proxy, `browser/`, the MCP registration,
   and their tests. Rewrite `docs/system-specs/modules/browser.md`, the browser
   sections of the agent system prompt, and the `web-browse` / `web-verify` /
   `browser-auth` skills.
4. **Sweep.** The remaining files among the 115 that mention playwright:
   install guides, mcp architecture doc, e2e gate.

## Open decisions

None. All four are settled above: global install only, presence as consent (with
the accepted risk recorded), the service prunes snapshots, and the agent reads
snapshot YAML with its own file tools.
