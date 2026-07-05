from .trace import evaluate_task_trace


def run_m2_scenarios(*args, **kwargs):
    from .m2_scenarios import run_m2_scenarios as _run_m2_scenarios

    return _run_m2_scenarios(*args, **kwargs)


__all__ = ["evaluate_task_trace", "run_m2_scenarios"]
