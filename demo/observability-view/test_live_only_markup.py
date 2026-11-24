"""Public ProjectFlow page must contain exclusively live Terminal MCP data."""
from pathlib import Path

HTML = Path(__file__).with_name("index.html")

def test_public_page_only_loads_live_sse_and_no_simulator():
    content = HTML.read_text(encoding="utf-8")
    assert 'src="live-sse.js"' in content
    assert 'id="live-panel"' in content
    assert 'id="live-sessions"' in content
    assert 'id="live-running-count"' in content
    assert 'src="app.js"' not in content
    for marker in (
        'demo-divider', 'DỮ LIỆU MẪU', 'Mô phỏng các bước làm việc',
        'data-scenario=', 'id="play"', 'id="workflow"',
        'Trạng thái thực thi', 'Task đang chờ',
    ):
        assert marker not in content
    assert content.count("<main>") == 1
    assert content.count("</main>") == 1

def test_live_view_remains_read_only():
    content = HTML.read_text(encoding="utf-8")
    assert "Content-Security-Policy" in content
    assert "connect-src https://terminal-bootstrap.mesflow.net https://raw.githubusercontent.com" in content
    assert "Không thực thi lệnh hay sửa task" in content
