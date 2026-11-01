# Test fixture: frozen repository snapshot

A copy of `config/`, `results/`, `public/` and `.gitlab/CODEOWNERS`, taken from `main` at results update 3.

The unit tests use this snapshot instead of the live files, so `verify` tests the checker's code and never depends on the content of the MR being checked. Checking the live content is the `integrity` job's job.

Refresh the snapshot only when the data format or the site's structure changes on purpose:

```bash
rm -rf checker/tests/fixtures/repo/{config,results,public,.gitlab}
git archive main config results public .gitlab/CODEOWNERS | tar -x -C checker/tests/fixtures/repo
```
