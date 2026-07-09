from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "ops-crm" / "index.html").read_text()


def test_dashboard_has_operator_workspace_controls():
    required = [
        'id="search"',
        'id="ownerFilter"',
        'id="statusFilter"',
        'id="exportButton"',
        'id="resetButton"',
        'id="operatorNotes"',
        'id="saveNotesButton"',
        'id="copyNotesButton"',
        'copyActionButton',
        'Mark done',
        'Mark blocked',
        'localStorage',
        'mmtvu-operator-crm-notes-v2',
        'id="revenueOsPanel"',
        'id="dailyBriefLink"',
        'ops-crm/crm.sqlite',
        'id="prospectStatusSelect"',
    ]
    for marker in required:
        assert marker in HTML


def test_dashboard_status_writes_go_to_sqlite_not_localstorage():
    # One action-status store: the dashboard posts status to serve.py, which writes
    # SQLite and re-exports; localStorage keeps only operator notes.
    assert '/api/action-status' in HTML
    assert '/api/prospect-status' in HTML
    assert 'mmtvu-operator-crm-state-v1' not in HTML
    assert "status:'open'" not in HTML
    assert 'uv run python ops-crm/serve.py' in HTML
    assert 'python3 -m http.server' not in HTML
    assert 'contacted_total' in HTML


def test_dashboard_escapes_dynamic_fields_before_inner_html_insertion():
    # The static app uses innerHTML for templating, so every dynamic dataset field
    # inserted into the generated markup must pass through esc().
    dynamic_markers = [
        '${esc(a.action)}',
        '${esc(a.reason)}',
        '${esc(a.owner)}',
        '${esc(a.evidence_link)}',
        '${esc(a.source_entity_id)}',
        '${esc(a.expected_revenue_path)}',
    ]
    for marker in dynamic_markers:
        assert marker in HTML
    assert 'function esc(v)' in HTML


def test_dashboard_exposes_keyboard_shortcuts():
    for marker in ["ev.key === '/'", "ev.key === 'j'", "ev.key === 'k'", "ev.key === 'd'", "ev.key === 'b'", "ev.key === 'c'"]:
        assert marker in HTML
