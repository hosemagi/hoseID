# Environments

Three venvs. The split is not tidiness — it is required.

`speciesnet` installs protobuf 7.35.1. `megadetector` depends on `ultralytics-yolov5`, which pins
`protobuf<=3.20.1`. In a combined venv pip reports the conflict as an error and leaves the
environment in a state it does not support. Both packages happened to keep working (verified:
MegaDetector inference ran correctly after the protobuf upgrade), but that is luck, not a
contract, and it would break on any future upgrade of either.

| venv | Installs | protobuf | Runs |
|---|---|---|---|
| `.venv` | `hoseid`, pytest, pillow | n/a | CLI, tests, review/tag code |
| `.venv-detector` | `hoseid`, megadetector 10.0.24 | 3.20.1 | `stages/detect.py` (CPU) |
| `.venv-classifier` | `hoseid`, speciesnet 5.0.5 | 7.35.1 | `stages/classify.py` (MPS) |

The `hoseid` package is installed into all three and is deliberately dependency-light (pydantic,
click) so it cannot drag a conflicting pin into either ML environment. The stages import it for
paths, schema and DB access; they never import each other.

Recreate:

```bash
for v in .venv .venv-detector .venv-classifier; do python3.12 -m venv $v; done
.venv/bin/pip install -e . pytest pillow
.venv-detector/bin/pip install -e . megadetector
.venv-classifier/bin/pip install -e . speciesnet --use-pep517
```

Verify the split held:

```bash
.venv-detector/bin/pip list | grep -E '^protobuf'      # expect 3.20.1
.venv-classifier/bin/pip list | grep -E '^protobuf'    # expect 7.35.1
```
