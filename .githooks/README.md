# Safety hooks

These hooks stop private material from ever reaching this public repo. They are
version-controlled but **not active until you point git at them** (git never runs
tracked hooks automatically):

```bash
git config core.hooksPath .githooks
```

Run that once per clone. After that:

- **`pre-commit`** blocks a commit if you stage a denylisted file
  (`reviews.db`, `products.json`, `exports/`, `docs/opportunity-*.md`, `*.log`,
  `.env`) **or** if the staged diff contains private strings (real target-app
  names, a `/Users/...` home path, or a personal email). `sample.db` is allowed.
- **`pre-push`** re-checks the commits you're about to push for those same
  denylisted files — a backstop for anything added with `git add -f` or committed
  with `--no-verify`.

Genuine override (rare): `git commit --no-verify` / `git push --no-verify`.

Edit the `DENY_PATHS` / `DENY_TEXT` patterns in the hook scripts to fit your own
list of things that must stay local.
