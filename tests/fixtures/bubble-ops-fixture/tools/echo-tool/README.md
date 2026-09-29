# echo-tool

Deterministic stub tool for `bubble-ops-fixture`. Demonstrates the
**tool** side of the Notion v4 "skill vs tool" distinction:

> Tool = fonction déterministe ... récupère, calcule, normalise. **Ne raisonne pas.**

## Files

| File | Purpose |
|---|---|
| `tool.py` | The implementation — pure Python, no side effects |
| `schema.json` | JSON-schema for input + output (matches `tool.py`'s contract) |
| `README.md` | This file |

## Usage

```bash
echo '{"message":"hello fixture"}' | python3 tool.py
```

Output (with a different `ts` each run, but otherwise identical):

```json
{"echoed": "hello fixture", "ts": "2026-05-20T17:30:00.123456+00:00", "input_keys": ["message"]}
```

## Test

```bash
echo '{"message":"hi"}' | python3 tool.py | python3 -c \
  "import json,sys; d=json.load(sys.stdin); assert d['echoed']=='hi'; assert 'ts' in d; print('PASS')"
```

The fixture's `tests/run.sh` runs this check as the **tool-level** test
(one of the 4 required test levels per Notion v4 §"Testabilité par
isolation").

## Schema compliance

The tool's runtime output validates against the `output` sub-schema in
`schema.json`. To verify:

```bash
echo '{"message":"hi"}' | python3 tool.py | python3 -c "
import json, sys, jsonschema
out = json.load(sys.stdin)
schema = json.load(open('schema.json'))['properties']['output']
jsonschema.validate(out, schema)
print('SCHEMA_OK')
"
```
