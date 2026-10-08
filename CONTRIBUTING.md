# Contributing to Lore

Thanks for your interest in Lore! Bug reports, ideas and pull requests are all welcome.

## Contributor License Agreement

Before your first pull request can be merged, you need to sign the
[Contributor License Agreement](CLA.md). You only sign once, and it covers all your
future contributions.

When you open a pull request, a bot will comment asking you to sign. Reply with a
comment containing exactly:

```
I have read the CLA Document and I hereby sign the CLA
```

The `cla` check turns green once every author of the pull request's commits has
signed. If it doesn't update, comment `recheck`.

**Why a CLA?** Lore is licensed under the [AGPL-3.0](LICENSE), and its author also
offers commercial licenses and may relicense future versions. The CLA gives the author
the right to include your contribution under those terms too. You keep the copyright
in your work and can still use it however you like.

## Development

See the [README](README.md) for setup. Run the tests before opening a pull request:

```sh
cd app && uv run pytest
```

## Terminology

Lore uses generic tabletop RPG terms. Don't add trademarked game names, settings or
rules vocabulary from commercial game systems.
