from app.services.planner_service import PlannerAgentError, PlannerAgentInvoker
calls=[]
def runner(*args, **kwargs):
    calls.append((args, kwargs))
    raise AssertionError("runner must not execute when provider is unavailable")
invoker=PlannerAgentInvoker(runner=runner, which=lambda name: None)
try:
    invoker.invoke("claude", "synthetic local probe", {"type":"object"}, "/tmp")
except PlannerAgentError as exc:
    assert "claude CLI not found in PATH" in str(exc), str(exc)
else:
    raise AssertionError("expected fail-closed provider-unavailable error")
assert calls == [], calls
print("PASS: unavailable provider returns PlannerAgentError before runner invocation; no provider process/network call")
