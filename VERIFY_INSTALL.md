# Verify the active FORGE installation

The logs supplied for the latest failed run reference the old `coder_agent.py` line layout. To prevent patching one checkout while running another, verify the module path in the exact virtualenv used to launch `forge`:

```bash
cd ~/Desktop/forge_harness
./venv/bin/python -c "import forge_harness; print(forge_harness.__file__); print(getattr(forge_harness, '__patch_revision__', 'PATCH MISSING'))"
./venv/bin/python - <<'PY'
from forge_harness.agents.coder_agent import CoderAgent
from forge_harness.utils.server import ExpoServer
print('stale-anchor repair:', hasattr(CoderAgent, '_normalize_stale_repair_anchors'))
print('screen mount repair:', hasattr(CoderAgent, '_ensure_screen_mount_operation'))
print('server identity check:', hasattr(ExpoServer, '_server_belongs_to_project'))
PY
```

Expected revision: `2026-10-28-screen-mount-server-identity`; all three booleans should be `True`. If any check fails, this environment is not using the repaired code. Do not rerun an expensive generation until all four checks match.
