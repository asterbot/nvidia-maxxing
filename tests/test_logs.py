import json

from maxxing.detector.log_tail import line_signals
from maxxing.detector.signals import Kind
from maxxing.evidence.containers import state_from_attrs
from maxxing.evidence.git import map_container_path
from maxxing.evidence.logs import cluster_errors, compute_stats, parse_line, parse_lines, parse_stack

STACK_A = (
    "TypeError: Cannot read properties of null (reading 'url')\n"
    "    at /app/src/server.js:143:31\n"
    "    at process.processTicksAndRejections (node:internal/process/task_queues:105:5)"
)
STACK_B = (
    "ReferenceError: foo is not defined\n"
    "    at handler (/app/src/routes/links.js:12:5)\n"
    "    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)"
)


def pino(level, msg, t=1761420000000, replica="r1", **extra):
    return json.dumps({"level": level, "time": t, "replica": replica, "sha": "abc", "msg": msg, **extra})


def test_parse_pino_request_line_nested():
    ln = parse_line(pino(50, "request errored", req={"id": 3, "method": "GET", "url": "/x"}, res={"statusCode": 500}, responseTime=4))
    assert ln.status == 500 and ln.method == "GET" and ln.url == "/x" and ln.replica == "r1"
    assert abs(ln.ts - 1761420000.0) < 1e-6 and ln.is_error


def test_parse_dotted_keys_and_docker_timestamp_prefix():
    raw = "2026-10-25T19:20:01.123456789Z " + json.dumps({"level": 30, "time": 1761420001123, "req.method": "POST", "res.statusCode": 201, "msg": "ok"})
    ln = parse_line(raw, "vip-api-1")
    assert ln.status == 201 and ln.method == "POST" and ln.container == "vip-api-1"


def test_parse_nginx_access_line():
    raw = '2026-10-25T19:20:01.000000000Z 172.19.0.7 - - [25/Oct/2026:19:20:01 +0000] "GET /k0001x HTTP/1.1" 502 157 "-" "node"'
    ln = parse_line(raw, "vip-lb-1")
    assert ln.status == 502 and ln.raw is not None and ln.ts > 1.7e9


def test_parse_stack_maps_repo_frames_only():
    frames = parse_stack(STACK_B, lambda p: map_container_path(p, {"src/routes/links.js", "src/server.js"}))
    assert frames[0].repo_path == "src/routes/links.js" and frames[0].line == 12 and frames[0].function == "handler"
    assert frames[1].repo_path is None  # node_modules
    async_frames = parse_stack("Error: x\n    at async /app/src/server.js:88:17", lambda p: map_container_path(p, {"src/server.js"}))
    assert async_frames[0].repo_path == "src/server.js" and async_frames[0].line == 88


def test_cluster_errors_groups_by_signature():
    err_a = {"type": "TypeError", "message": "Cannot read properties of null (reading 'url')", "stack": STACK_A}
    err_b = {"type": "ReferenceError", "message": "foo is not defined", "stack": STACK_B}
    lines = parse_lines(
        [pino(60, "unhandled rejection, shutting down", t=1761420000000 + i * 1000, replica=f"r{i % 3}", err=err_a) for i in range(5)]
        + [pino(50, "request errored", t=1761420000500, err=err_b, res={"statusCode": 500})]
        + [pino(40, "request completed", res={"statusCode": 404})] * 20  # 404s are not errors
        + [pino(30, "request completed", res={"statusCode": 200})] * 50
    )
    clusters = cluster_errors(lines, lambda p: map_container_path(p, {"src/server.js", "src/routes/links.js"}))
    assert [c.err_type for c in clusters] == ["TypeError", "ReferenceError"]
    a = clusters[0]
    assert a.count == 5 and sorted(a.replicas) == ["r0", "r1", "r2"] and a.app_frames[0].line == 143


def test_cluster_normalizes_volatile_message_parts():
    lines = parse_lines([
        pino(50, "redis connection error", err={"type": "Error", "message": f"connect ECONNREFUSED 172.19.0.{i}:6380"}) for i in range(4)
    ])
    assert len(cluster_errors(lines)) == 1


def test_stats_prefer_lb_requests():
    lines = parse_lines([
        pino(30, "request completed", res={"statusCode": 200}),
        '172.19.0.7 - - [25/Oct/2026:19:20:01 +0000] "GET /a HTTP/1.1" 200 23 "-" "node"',
        '172.19.0.7 - - [25/Oct/2026:19:20:01 +0000] "GET /b HTTP/1.1" 502 23 "-" "node"',
    ])
    s = compute_stats(lines)
    assert s.requests == 2 and s.status_5xx == 1 and s.error_rate == 0.5


def test_log_tail_signals():
    sigs = line_signals(pino(60, "uncaught exception, shutting down", err={"type": "Error", "message": "boom"}), "vip-api-1", "api", False)
    assert [s.kind for s in sigs] == [Kind.LOG_ERROR] and sigs[0].level == 60
    lb = line_signals('1.2.3.4 - - [x] "GET / HTTP/1.1" 504 0 "-" "-"', "vip-lb-1", "lb", True)
    assert [s.kind for s in lb] == [Kind.REQUEST] and lb[0].status == 504


def test_container_state_from_inspect():
    attrs = {
        "Name": "/vip-api-2", "Id": "abcdef1234567890", "RestartCount": 3,
        "State": {"Status": "restarting", "Running": False, "ExitCode": 137, "OOMKilled": True,
                  "FinishedAt": "2026-10-25T19:19:58Z", "Health": {"Status": "unhealthy", "Log": [{"ExitCode": -1, "Output": "Health check exceeded timeout (3s)"}]}},
        "Config": {"Image": "vip-api:abc", "Env": ["GIT_SHA=abc123", "PORT=3000"], "Labels": {"com.docker.compose.service": "api"}},
        "HostConfig": {"Memory": 268435456},
    }
    cs = state_from_attrs(attrs, {"memory_stats": {"usage": 250 * 2**20, "limit": 268435456, "stats": {"inactive_file": 0}}})
    assert cs.oom_killed and cs.exit_code == 137 and cs.restart_count == 3 and cs.sha == "abc123"
    assert cs.service == "api" and cs.mem_pct == 97.7 and "timeout" in cs.health_log[0]
    assert "OOMKilled=true" in cs.one_line()
