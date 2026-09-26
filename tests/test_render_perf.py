"""Thread budget comes from the cgroup quota; progress follows the pipeline's own log order."""
from app import jobs, pipeline, worker


def test_cpu_budget_reads_cgroup_quota(tmp_path):
    f = tmp_path / 'cpu.max'
    f.write_text('800000 100000\n')
    assert pipeline.cpu_budget(f) == 8           # Railway: 8 vCPU while nproc says 48
    f.write_text('150000 100000\n')
    assert pipeline.cpu_budget(f) == 2           # partial CPU rounds up
    f.write_text('max 100000\n')
    unlimited = pipeline.cpu_budget(f)
    assert unlimited >= 1 and unlimited == pipeline.cpu_budget(tmp_path / 'missing')  # no quota: visible CPUs


def test_progress_rises_through_a_free_render(monkeypatch):
    seen = []
    monkeypatch.setattr(jobs, 'report', lambda *a, progress=None, **k: seen.append(progress))
    lines = ['Fetching listing 1', 'Found 21 photos, rating 5.0 from 23 reviews', 'Captured 7 reviews', 'Downloaded 21 photos',
             'Scoring 21 photos for sharpness, light and colour', 'Estimating depth for 18 photos', 'Scored 21 photos; using 11',
             'Estimating depth for 3 more frames', 'Audit: PASS (100/100)', 'QA guards passed (10 scenes, ~45s)', 'Depth maps ready',
             'Rendering cinematic 16:9 walkthrough (v2)', 'Rendered 57.7s reel', 'Uploading to your Google Drive']
    for line in lines:
        worker._progress({'id': 'j', 'lease_token': 't'}, line)
    # the late depth line maps lower than 'Scored'; jobs.report keeps GREATEST(progress), so the bar never moves back
    assert [p for p in seen if p is not None] == [8, 16, 24, 32, 40, 48, 40, 56, 80, 88, 96]
