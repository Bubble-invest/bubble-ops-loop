# External contract fixtures

- `bubble-ops-fixture/` is the minimal Step-5 skeleton from
  `vdk888/bubble-ops-fixture@72d045c598cbda98f9359b91cd1b2a5f5168e48a`.
  Its canonical `dept.yaml` carries the repository's later operator-name
  redaction so the byte-identity assertion remains current.
- `bubble-ops-fixture-codeowners/` pins the structural ownership rules needed
  by the CODEOWNERS documentation tests without requiring an ambient clone.
- `bubble-vps-platform/` is a deliberately minimal renderer contract double.
  It accepts the canonical CLI flags and emits the three artifacts consumed by
  `scripts/deploy-to-morty.sh`; it does not duplicate the platform renderer.

Keep these fixtures minimal. Live deployment and output assertions belong in
tests marked `live`, not in the default hermetic suite.
