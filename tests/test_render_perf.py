"""Thread budget comes from the cgroup quota."""
from app import pipeline


def test_cpu_budget_reads_cgroup_quota(tmp_path):
    f = tmp_path / 'cpu.max'
    f.write_text('800000 100000\n')
    assert pipeline.cpu_budget(f) == 8           # Railway: 8 vCPU while nproc says 48
    f.write_text('150000 100000\n')
    assert pipeline.cpu_budget(f) == 2           # partial CPU rounds up
    f.write_text('max 100000\n')
    unlimited = pipeline.cpu_budget(f)
    assert unlimited >= 1 and unlimited == pipeline.cpu_budget(tmp_path / 'missing')  # no quota: visible CPUs

