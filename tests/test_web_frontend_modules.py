import base64
import re
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.provider import reset_store_for_tests


ROOT = Path(__file__).resolve().parents[1]


def run_node(source: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_router_and_http_modules_preserve_auth_and_role_interfaces() -> None:
    router = _data_module(ROOT / "app/web/core/router.js")
    http = _data_module(ROOT / "app/web/core/http.js")
    run_node(
        f"""
        import assert from 'node:assert/strict';
        import {{ allowedViews, parseRoute }} from {router!r};
        import {{ createHttpClient }} from {http!r};
        const values = new Map();
        const storage = {{ getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }};
        const route = parseRoute('#candidate/iv_1?token=candidate-secret', storage);
        assert.equal(route.candidateToken, 'candidate-secret');
        assert.equal(storage.getItem('candidate-session:iv_1'), 'candidate-secret');
        assert.deepEqual([...allowedViews(['reviewer'])], ['interviews']);
        globalThis.window = {{ setTimeout, clearTimeout, sessionStorage: storage }};
        let captured;
        const client = createHttpClient({{
          tokenStorage: storage,
          fetchImpl: async (path, options) => {{ captured = {{ path, options }}; return {{ ok: true, status: 200, text: async () => '{{"items":[]}}' }}; }},
        }});
        client.setAccessToken('bearer-value');
        await client.request('/api/v1/interviews');
        assert.equal(captured.options.headers.Authorization, 'Bearer bearer-value');
        await client.request('/api/v1/public/interviews/iv_1', {{ headers: {{ 'X-Candidate-Session-Token': 'candidate-secret' }} }});
        assert.equal(captured.options.headers.Authorization, undefined);
        const blockingFetch = async (_path, options) => new Promise((_resolve, reject) => {{
          options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), {{ once: true }});
        }});
        const cancellable = createHttpClient({{ tokenStorage: storage, fetchImpl: blockingFetch }});
        const caller = new AbortController();
        const cancelled = cancellable.request('/api/v1/interviews', {{ signal: caller.signal, timeoutMs: 100000 }});
        caller.abort();
        await assert.rejects(cancelled, error => error.code === 'REQUEST_CANCELLED');
        await assert.rejects(
          cancellable.request('/api/v1/interviews', {{ timeoutMs: 1 }}),
          error => error.code === 'REQUEST_TIMEOUT',
        );
        """
    )


def test_candidate_runtime_replays_complete_local_audio_after_disconnect() -> None:
    runtime = _data_module(ROOT / "app/web/candidate/runtime.js")
    run_node(
        f"""
        import assert from 'node:assert/strict';
        import {{ createCandidateInterviewRuntime }} from {runtime!r};
        globalThis.window = {{ setTimeout, clearTimeout }};
        globalThis.WebSocket = {{ OPEN: 1 }};
        globalThis.MediaStream = class {{ constructor(tracks) {{ this.tracks = tracks; }} }};
        class Recorder {{
          static last;
          static isTypeSupported() {{ return true; }}
          constructor() {{ this.mimeType = 'audio/webm;codecs=opus'; this.state = 'inactive'; this.listeners = {{}}; Recorder.last = this; }}
          addEventListener(type, fn) {{ this.listeners[type] = fn; }}
          start() {{ this.state = 'recording'; }}
          stop() {{ this.state = 'inactive'; this.listeners.stop(); }}
          emit(blob) {{ this.listeners.dataavailable({{ data: blob }}); }}
        }}
        globalThis.MediaRecorder = Recorder;
        const phases = [];
        const firstSocket = {{ readyState: 1, sent: [], send(value) {{ this.sent.push(value); }} }};
        const candidate = createCandidateInterviewRuntime({{ onStateChange: state => phases.push(state.phase), stopAckTimeoutMs: 100000 }});
        candidate.start({{ mediaStream: {{ getAudioTracks: () => [{{}}] }}, candidateSocket: firstSocket, activeTurnId: 'turn_1' }});
        Recorder.last.emit(new Blob(['complete-audio']));
        firstSocket.readyState = 3;
        candidate.stop();
        assert.equal(candidate.getSnapshot().phase, 'recoverable');
        const recoveredSocket = {{ readyState: 1, sent: [], send(value) {{ this.sent.push(value); }} }};
        await candidate.recover(recoveredSocket);
        assert.equal(candidate.getSnapshot().phase, 'awaiting_server');
        assert.equal(typeof recoveredSocket.sent[0], 'string');
        assert.ok(recoveredSocket.sent[1] instanceof ArrayBuffer);
        assert.equal(typeof recoveredSocket.sent[2], 'string');
        candidate.acknowledgeMediaStored();
        assert.equal(candidate.getSnapshot().phase, 'stored');
        """
    )


def test_workspace_query_uses_fixed_route_level_requests_without_catalog_n_plus_one() -> None:
    workspace = _data_module(ROOT / "app/web/core/workspace.js")
    run_node(
        f"""
        import assert from 'node:assert/strict';
        import {{ createWorkspaceQuery }} from {workspace!r};
        const calls = [];
        const state = {{}};
        const api = async path => {{
          calls.push(path);
          if (path.endsWith('/job-positions')) return {{ items: [{{id:'p1'}}] }};
          if (path.endsWith('/knowledge-bases')) return {{ items: [{{id:'kb1'}}] }};
          if (path.endsWith('/workspace/question-overview')) return {{ total: 9, ready: 7, recent: [{{id:'q9'}}] }};
          return {{ items: [] }};
        }};
        const query = createWorkspaceQuery({{ api, state, newestFirst: items => items || [] }});
        await query.load('questions', ['interviewer']);
        assert.deepEqual(calls.sort(), ['/api/v1/job-positions', '/api/v1/knowledge-bases'].sort());
        assert.equal(state.questions.length, 0);
        assert.equal(state.knowledgeBases.length, 1);
        calls.length = 0;
        await query.load('overview', ['interviewer']);
        assert.ok(calls.includes('/api/v1/workspace/question-overview'));
        assert.ok(!calls.includes('/api/v1/workspace/question-catalog'));
        assert.equal(state.questionOverview.total, 9);
        """
    )


def test_appointment_invite_failure_preserves_created_appointment_for_retry() -> None:
    appointment = _data_module(ROOT / "app/web/interviews/appointment.js")
    run_node(
        f"""
        import assert from 'node:assert/strict';
        import {{ createAppointmentAndInvite }} from {appointment!r};
        let creates = 0;
        const result = await createAppointmentAndInvite({{
          createAppointment: async () => {{ creates += 1; return {{ id: 'appointment_1', status: 'scheduled' }}; }},
          issueInvitation: async () => {{ throw new Error('readiness failed'); }},
        }});
        assert.equal(creates, 1);
        assert.equal(result.appointment.id, 'appointment_1');
        assert.equal(result.invitation, null);
        assert.equal(result.inviteError.message, 'readiness failed');
        """
    )


def test_react_entrypoint_and_bundles_are_served_by_fastapi() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())
    index = api.get("/")
    assert index.status_code == 200
    assert '<div id="root"></div>' in index.text
    bundle_paths = re.findall(r'(?:src|href)="(/web/bundles/[^"]+)"', index.text)
    assert bundle_paths
    for path in bundle_paths:
        response = api.get(path)
        assert response.status_code == 200, path
    assert api.get("/web/vendor/lucide.min.js").status_code == 200
    assert api.get("/web/assets/digital-interviewer.png").status_code == 200


def _data_module(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:text/javascript;base64,{encoded}"
