FORGE repair-coverage fix source archive

This is the full FORGE source tree bundled with the repair-coverage fix.

After extracting, run from the extracted repository root:
  python -m pytest -q
  python -m compileall -q forge_harness tests

For applying only the incremental changes to an existing checkout, use the separate .patch file and run `patch --dry-run -p1` before applying.
