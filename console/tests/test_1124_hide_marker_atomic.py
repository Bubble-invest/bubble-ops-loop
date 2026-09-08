from pathlib import Path

import yaml
from console.services import github_reader as reader


def test_hide_marker_keeps_prior_complete_record_until_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, 'repo_path', lambda slug: tmp_path)
    dest = tmp_path / 'inbox/decisions/synthetic.yaml'
    dest.parent.mkdir(parents=True)
    dest.write_text('decision: prior\n')
    calls = []
    real_replace = reader.os.replace

    def observe(source, target):
        assert Path(target) == dest
        assert dest.read_text() == 'decision: prior\n'
        assert yaml.safe_load(Path(source).read_text()) == {'decision': 'approved'}
        calls.append(True)
        real_replace(source, target)

    monkeypatch.setattr(reader.os, 'replace', observe)
    reader._write_local_hide_marker('content', 'synthetic', {'decision': 'approved'})
    assert calls == [True]
    assert yaml.safe_load(dest.read_text()) == {'decision': 'approved'}
    assert list(dest.parent.glob('.*.tmp')) == []


def test_failed_hide_marker_replace_preserves_old_record_and_cleans_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, 'repo_path', lambda slug: tmp_path)
    dest = tmp_path / 'inbox/decisions/synthetic.yaml'
    dest.parent.mkdir(parents=True)
    dest.write_text('decision: prior\n')

    def fail(source, target):
        raise OSError('synthetic interrupted publication')

    monkeypatch.setattr(reader.os, 'replace', fail)
    reader._write_local_hide_marker('content', 'synthetic', {'decision': 'approved'})
    assert dest.read_text() == 'decision: prior\n'
    assert list(dest.parent.glob('.*.tmp')) == []
