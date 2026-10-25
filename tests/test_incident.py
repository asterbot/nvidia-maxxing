from maxxing.detector.docker_events import event_to_signal
from maxxing.detector.incident import EventType, IncidentEngine, Thresholds
from maxxing.detector.signals import Kind, Signal

T = 1_000_000.0


def feed(engine, signals):
    events = []
    for s in signals:
        events += engine.observe(s)
    return events


def healthy_baseline(engine, start=T, seconds=30):
    for i in range(seconds):
        engine.observe(Signal(Kind.HEALTH_OK, start + i))
        engine.observe(Signal(Kind.REQUEST, start + i, status=200))


def test_oom_opens_immediately():
    e = IncidentEngine()
    ev = feed(e, [Signal(Kind.CONTAINER_OOM, T, "vip-api-1")])
    assert [x.type for x in ev] == [EventType.OPENED]
    assert ev[0].rules[0].startswith("oom")


def test_single_crash_does_not_open_but_two_do():
    e = IncidentEngine()
    assert feed(e, [Signal(Kind.CONTAINER_DIE, T, "vip-api-1", exit_code=1)]) == []
    ev = feed(e, [Signal(Kind.CONTAINER_DIE, T + 5, "vip-api-2", exit_code=1)])
    assert ev and ev[0].type == EventType.OPENED and "crash_loop" in ev[0].rules[0]


def test_planned_stop_is_not_a_crash():
    e = IncidentEngine()
    sigs = []
    for i, name in enumerate(["vip-api-1", "vip-api-2", "vip-api-3"]):
        sigs += [Signal(Kind.CONTAINER_KILL, T + i, name), Signal(Kind.CONTAINER_DIE, T + i + 1, name, exit_code=143)]
    assert feed(e, sigs) == []


def test_crash_window_expires():
    e = IncidentEngine(Thresholds(crash_window_s=60))
    feed(e, [Signal(Kind.CONTAINER_DIE, T, "a", exit_code=1)])
    assert feed(e, [Signal(Kind.CONTAINER_DIE, T + 120, "b", exit_code=1)]) == []


def test_lb_down_needs_a_streak():
    e = IncidentEngine()
    assert feed(e, [Signal(Kind.HEALTH_FAIL, T), Signal(Kind.HEALTH_FAIL, T + 2), Signal(Kind.HEALTH_OK, T + 4),
                    Signal(Kind.HEALTH_FAIL, T + 6)]) == []
    ev = feed(e, [Signal(Kind.HEALTH_FAIL, T + 8), Signal(Kind.HEALTH_FAIL, T + 10)])
    assert ev and "lb_down" in ev[0].rules[0]


def test_error_rate_rule():
    e = IncidentEngine()
    sigs = [Signal(Kind.REQUEST, T + i * 0.1, status=500 if i % 2 else 200) for i in range(30)]
    ev = feed(e, sigs)
    assert len([x for x in ev if x.type == EventType.OPENED]) == 1
    assert any("error_rate" in r for r in ev[0].rules)


def test_healthy_surge_with_404s_does_not_page():
    e = IncidentEngine()
    healthy_baseline(e)
    surge = []
    for i in range(3000):  # 10x traffic, ~10% 404s, a couple of stray 502s, slow-but-ok health
        status = 404 if i % 10 == 0 else 502 if i % 1500 == 7 else 200
        surge.append(Signal(Kind.REQUEST, T + 30 + i * 0.01, status=status))
    surge += [Signal(Kind.LOG_ERROR, T + 40, "vip-api-1", level=50)]  # a single error line
    surge += [Signal(Kind.HEALTH_FAIL, T + 45), Signal(Kind.HEALTH_OK, T + 47)]  # one slow health check
    assert feed(e, surge) == []
    assert e.current is None


def test_dedup_one_incident_per_outage():
    e = IncidentEngine()
    ev = feed(e, [Signal(Kind.CONTAINER_OOM, T, "a"), Signal(Kind.CONTAINER_OOM, T + 1, "b")]
              + [Signal(Kind.REQUEST, T + 2 + i * 0.1, status=502) for i in range(20)]
              + [Signal(Kind.HEALTH_FAIL, T + 3 + i) for i in range(5)])
    opened = [x for x in ev if x.type == EventType.OPENED]
    updated = [x for x in ev if x.type == EventType.UPDATED]
    assert len(opened) == 1
    assert {r.split(":")[0] for x in updated for r in x.rules} <= {"error_rate", "lb_down"}


def test_recovery_and_cooldown():
    th = Thresholds(recovery_s=20, cooldown_s=90)
    e = IncidentEngine(th)
    feed(e, [Signal(Kind.CONTAINER_OOM, T, "a")])
    assert e.current is not None
    # still bad: no resolve
    assert e.tick(T + 5) == []
    for i in range(30):
        e.observe(Signal(Kind.HEALTH_OK, T + 1 + i))
    res = e.tick(T + 30)
    assert res and res[0].type == EventType.RESOLVED
    assert e.current is None
    # a new OOM inside the cooldown is suppressed, after it opens a new incident
    assert feed(e, [Signal(Kind.CONTAINER_OOM, T + 60, "a")]) == []
    ev = feed(e, [Signal(Kind.CONTAINER_OOM, T + 200, "a")])
    assert ev and ev[0].type == EventType.OPENED and ev[0].incident.seq == 2


def test_no_recovery_while_health_failing():
    e = IncidentEngine()
    feed(e, [Signal(Kind.CONTAINER_OOM, T, "a")])
    for i in range(40):
        e.observe(Signal(Kind.HEALTH_FAIL, T + i))
    assert e.tick(T + 45) == []


def test_deploy_grace_mutes_lb_rules_but_not_crashes():
    e = IncidentEngine(Thresholds(deploy_grace_s=20))
    e.observe(Signal(Kind.DEPLOY, T, "vip-api-1", detail="create"))
    assert feed(e, [Signal(Kind.HEALTH_FAIL, T + 1 + i) for i in range(5)]) == []
    ev = feed(e, [Signal(Kind.CONTAINER_DIE, T + 6, "x", exit_code=1), Signal(Kind.CONTAINER_DIE, T + 7, "y", exit_code=1)])
    assert ev and "crash_loop" in ev[0].rules[0]


def test_unhealthy_replicas_rule():
    e = IncidentEngine()
    assert feed(e, [Signal(Kind.CONTAINER_UNHEALTHY, T, "vip-api-1")]) == []
    ev = feed(e, [Signal(Kind.CONTAINER_UNHEALTHY, T + 3, "vip-api-2")])
    assert ev and "unhealthy" in ev[0].rules[0]


def test_docker_event_mapping():
    base = {"Type": "container", "timeNano": int(T * 1e9)}
    attrs = {"name": "vip-api-1", "com.docker.compose.service": "api"}
    die = event_to_signal({**base, "Action": "die", "Actor": {"Attributes": {**attrs, "exitCode": "137"}}}, "api")
    assert die.kind == Kind.CONTAINER_DIE and die.exit_code == 137 and die.ts == T
    assert event_to_signal({**base, "Action": "health_status: unhealthy", "Actor": {"Attributes": attrs}}, "api").kind == Kind.CONTAINER_UNHEALTHY
    assert event_to_signal({**base, "Action": "oom", "Actor": {"Attributes": attrs}}, "api").kind == Kind.CONTAINER_OOM
    assert event_to_signal({**base, "Action": "create", "Actor": {"Attributes": attrs}}, "api").kind == Kind.DEPLOY
    assert event_to_signal({**base, "Action": "exec_start: wget", "Actor": {"Attributes": attrs}}, "api") is None
    traffic = {"name": "vip-traffic-1", "com.docker.compose.service": "traffic"}
    assert event_to_signal({**base, "Action": "die", "Actor": {"Attributes": traffic}}, "api") is None
