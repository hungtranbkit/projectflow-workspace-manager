"""Deterministic contract boundary for tool-less planner test doubles."""


def install_planner_test_double(invoker, runner) -> None:
    """Install a runner without requiring a provider binary or credentials."""
    invoker.runner = runner
    invoker.which = lambda name: f"/__projectflow_test_double__/{name}"
