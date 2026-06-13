# Contributing

Thanks for contributing to sshm. This guide is the concrete, step-by-step workflow
— setup, the checks to run, and the exact branch / commit / PR format the project
uses.

## 1. Setup

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:revsearch/sshm.git
cd sshm
uv sync            # create the venv and install deps (including the dev group)
```

Run the CLI straight from the checkout, no install needed:

```bash
uv run sshm --help
uv run sshm add test root@host
```

For debugging the daemon, run it in the foreground (it normally autostarts in the
background):

```bash
uv run sshmd        # logs to stderr and ~/.sshm/sshmd.log
```

## 2. Make your change

- Match the style of the surrounding code. The project is plain standard library
  + [click](https://click.palletsprojects.com/); don't add dependencies without a
  good reason.
- Add or update tests for any behavior you change (see `tests/`). Many tests fake
  the daemon/ProcessManager or use `cat` on a PTY so they need no real ssh host;
  PTY tests are marked POSIX-only and skip on Windows.

## 3. Run the checks (same as CI)

Both must pass before you open a PR:

```bash
uv run pytest                 # tests
uvx ruff check src tests      # lint — must be clean
```

Optional, to see coverage:

```bash
uv run --with pytest-cov pytest --cov=sshm --cov-report=term-missing
```

## 4. Branches

Branch off the latest `master`, and name the branch with a conventional type
prefix:

| Prefix      | For                                |
|-------------|------------------------------------|
| `feat/`     | new features                       |
| `fix/`      | bug fixes                          |
| `test/`     | tests only                         |
| `docs/`     | documentation only                 |
| `ci/`       | CI / workflow changes              |
| `refactor/` | refactors with no behavior change  |
| `perf/`     | performance                        |
| `chore/`    | tooling, deps, housekeeping        |

```bash
git checkout master && git pull
git checkout -b fix/reconnect-backoff
```

## 5. Commits

Write a conventional, imperative summary line — `type: short summary` (≤ ~72 chars)
— where `type` matches the branch prefixes (`feat`, `fix`, `test`, `docs`, `ci`,
`refactor`, `perf`, `chore`). Add a blank line and a body explaining the *why* and
any trade-offs when it isn't obvious:

```
fix: don't write client input to a reused fd after reconnect

_write_input dup()s the PTY master under the lock and writes the private dup
outside it, so a concurrent _kill/reconnect closing the fd can't misdirect
keystrokes onto whatever reused the fd number.
```

## 6. Pull request

1. Push your branch: `git push -u origin <branch>`.
2. Open a PR against `master`. Title uses the same `type: summary` form; the
   description says **what** changed and **why**.
3. CI must be green — tests run on Linux / macOS / Windows × Python 3.12 / 3.13,
   plus `ruff`. (POSIX-only tests skip on Windows.)
4. Push more commits to address review feedback.
5. Merge with **Squash and merge**: set the squash commit title to the PR title
   and paste a concise body. Delete the branch after merging.

## Project layout

See the module table in the [README](README.md#development). In short: `cli.py` is
the `sshm` entry point, `daemon.py` is `sshmd`, `process.py` manages the SSH
sessions and PTYs, `ipc.py` / `protocol.py` are the CLI↔daemon transport, and
`config.py` reads and writes `~/.ssh/config`.
