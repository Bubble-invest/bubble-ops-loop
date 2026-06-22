"""
test_vocabulary_health.py — UX-refresh.

/health is the 'Carnet de bord' page. Calm rows, human last-active phrasing.
The pending/post-mvp wiring stays (existing text-drift tests require it)
but the headline + subtitle are in human voice.
"""


def test_health_title_is_carnet_de_bord(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.text
    assert "Carnet de bord" in body, "health title must be 'Carnet de bord'"


def test_health_subtitle_uses_human_voice(client):
    r = client.get("/health")
    body = r.text.lower()
    assert "actif pour la dernière fois" in body, \
        "subtitle must use human 'actif pour la dernière fois' voice"


def test_health_uses_pas_encore_commence_for_never(client):
    """Empty cells render 'pas encore commencé' instead of 'never'."""
    r = client.get("/health")
    body = r.text
    # v1 stub returns 'never' for every cell, which we translate.
    assert "pas encore commencé" in body, \
        "stub 'never' cells must read 'pas encore commencé'"
    # And the bare 'never' label must be gone from VISIBLE copy. Strip the
    # non-visible parts first: HTML comments, plus <script>/<style> blocks and
    # Jinja comments — the word "never" legitimately appears in JS/Jinja CODE
    # COMMENTS inside inline <script> (e.g. "...so it never goes blank"), which
    # the operator never reads. (Without stripping these, the test false-fails on
    # a code comment even though the rendered FR copy is correct.)
    import re
    visible = body.lower()
    visible = re.sub(r"<!--.*?-->", "", visible, flags=re.DOTALL)
    visible = re.sub(r"<script\b.*?</script>", "", visible, flags=re.DOTALL)
    visible = re.sub(r"<style\b.*?</style>", "", visible, flags=re.DOTALL)
    visible = re.sub(r"\{#.*?#\}", "", visible, flags=re.DOTALL)
    assert "never" not in visible, \
        "no 'never' label in visible copy (rendered text must be French)"


def test_health_footer_explains_live_source(client):
    """Footer tells the operator the data is read live from the real loop
    traces (updated 2026-06-01: was the stub-era 'dans la coulisse' note;
    the source is now actually wired — {{OPERATOR}} msg 1180)."""
    r = client.get("/health")
    body = r.text.lower()
    assert "en direct" in body or "source vivante" in body, \
        "footer must say the data is read live from the real loop traces"
