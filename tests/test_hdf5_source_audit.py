"""Independent raw-inventory and canonical source-audit contract tests."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

h5py = pytest.importorskip('h5py')
np = pytest.importorskip('numpy')

from evidence_system.hdf5_reader import RecordingReader
from evidence_system.registry import get_run, load_registry
from evidence_system.source_audit import REQUIRED_EDT_STEMS, build_source_audit, inspect_hdf5
from evidence_system.source_config import ResolvedRunSources


def _poses(times):
    times = np.asarray(times, dtype=float)
    angle = times * .4
    return np.column_stack([times * 2, times * -3, times * .5,
                            np.zeros(len(times)), np.zeros(len(times)),
                            np.sin(angle / 2), np.cos(angle / 2)])


def _chunk(path, times, *, schema='v3', camera_times=None, removal_time=None):
    times = np.asarray(times, dtype=float)
    prefix = 'vision_data/continuous_data/' if schema == 'v3' else 'data/'
    meta = 'metadata_v3/' if schema == 'v3' else 'metadata/'
    event = 'physics_data/intermittent_data/' if schema == 'v3' else ''
    with h5py.File(path, 'w') as handle:
        if len(times):
            handle[prefix + 'time'] = times
            handle[prefix + 'l_img'] = np.zeros((len(times), 4, 5, 3), dtype=np.uint8)
            handle[prefix + 'depth'] = np.ones((len(times), 4, 5), dtype=np.float32)
            for name in ('mastoidectomy_drill', 'mastoidectomy_volume'):
                handle[prefix + 'pose_' + name] = _poses(times)
            if schema == 'legacy':
                handle[prefix + 'pose_main_camera'] = _poses(times)
        handle[meta + 'voxel_volume'] = .008
        if schema == 'v3':
            handle.require_group('physics_data/intermittent_data')
            if camera_times is not None:
                camera = 'physics_data/continuous_data/high_frequency_poses/main_camera_pose/'
                handle[camera + 'time_stamp'] = np.asarray(camera_times)
                handle[camera + 'pose'] = _poses(camera_times)
        if removal_time is not None:
            handle[event + 'voxels_removed/voxel_time_stamp'] = [removal_time]
            handle[event + 'voxels_removed/voxel_removed'] = [[0., 4., 5., 6.]]
    return path


def _audit(raw, tmp_path):
    edt = tmp_path / 'edt'
    edt.mkdir(exist_ok=True)
    for name in REQUIRED_EDT_STEMS:
        (edt / (name + '.edt')).write_bytes(b'G3\n1 float\n2 2 2\n' + b'\0' * 32)
    bundle = tmp_path / 'bundle'
    bundle.mkdir(exist_ok=True)
    for name in ('model.pt', 'manifest.json', 'neural_preprocessing.json'):
        (bundle / name).write_text('{}')
    record = get_run('DEMO_CASE_01', load_registry(ROOT / 'config/examples/runs.example.csv'))
    sources = ResolvedRunSources(raw_run_dir=raw, edt_dir=edt, phase_annotation_file=None,
                                golden_reference_path=tmp_path / 'unused.json', phase_model_bundle=bundle)
    return build_source_audit(record, sources, hash_hdf5=False)


def test_v3_global_camera_slerp_crosses_chunk_boundary_and_keeps_local_voxel_indices(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    early = _chunk(raw / 'early.hdf5', [.02, .06], camera_times=[0., .04], removal_time=.04)
    late = _chunk(raw / 'late.hdf5', [.10, .14], camera_times=[.08, .12, .16], removal_time=.12)
    reader = RecordingReader(raw)
    for path, times, event_time in ((early, [.02, .06], .04), (late, [.10, .14], .12)):
        with reader.open_chunk(path) as view:
            np.testing.assert_allclose(view['data/pose_main_camera'], _poses(times), atol=1e-14)
            np.testing.assert_array_equal(view['voxels_removed/voxel_removed'][()][:, 0], [0])
            np.testing.assert_array_equal(view['voxels_removed/voxel_time_stamp'][()], [event_time])
    result = _audit(raw, tmp_path)
    assert result['ready_for_frame_extraction']
    assert result['hdf5']['frame_count_total'] == 4
    assert result['hdf5']['removed_voxel_row_count_total'] == 2
    camera = result['hdf5']['camera_alignment']
    assert camera['interpolated_frame_count'] == 4
    assert camera['missing_frame_count'] == 0
    first = result['hdf5']['chunks'][0]
    assert first['format'] == 'v3' and first['canonical_valid']
    physical_names = {item['path'] for item in first['datasets']}
    assert 'vision_data/continuous_data/time' in physical_names
    assert 'data/time' not in physical_names
    assert first['canonical_fields']['data/pose_main_camera']['shape'] == [2, 7]


def test_declared_camera_gap_retains_frame_and_records_incomplete_evidence(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    path = _chunk(raw / 'gap.hdf5', [.02, .5], camera_times=[0., .04, 1.])
    reader = RecordingReader(raw)
    with reader.open_chunk(path) as view:
        camera = view['data/pose_main_camera']
        assert np.isfinite(camera[0]).all()
        assert np.isnan(camera[1]).all()
        assert len(view['data/time']) == 2
    result = _audit(raw, tmp_path)
    assert result['ready_for_frame_extraction']
    assert result['camera_dependent_evidence_complete'] is False
    assert result['warnings']
    assert result['hdf5']['camera_alignment']['missing_frame_count'] == 1
    assert result['hdf5']['chunks'][0]['camera_missing_frame_count'] == 1


def test_physics_only_chunk_preserves_removal_count(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    _chunk(raw / 'frames.hdf5', [0., .04], camera_times=[0., .04])
    _chunk(raw / 'events.hdf5', [], removal_time=.02)
    result = _audit(raw, tmp_path)
    assert result['ready_for_frame_extraction']
    assert result['hdf5']['removed_voxel_row_count_total'] == 1
    events = next(item for item in result['hdf5']['chunks'] if item['file_name'] == 'events.hdf5')
    assert events['chunk_role'] == 'physics_only'
    assert events['frame_count'] == 0 and events['removed_voxel_row_count'] == 1


@pytest.mark.parametrize('mutation', ['unknown', 'mixed', 'missing_camera', 'raw_nan_camera', 'partial_removal'])
def test_unreadable_canonical_contract_never_becomes_metadata_or_zero(tmp_path, mutation):
    raw = tmp_path / 'raw'
    raw.mkdir()
    path = _chunk(raw / 'case.hdf5', [0., .04], camera_times=[0., .04], removal_time=.02)
    with h5py.File(path, 'a') as handle:
        if mutation == 'unknown':
            handle['other_recorder/frames'] = np.ones((2, 3))
        elif mutation == 'mixed':
            handle['data/time'] = [0., .04]
        elif mutation == 'missing_camera':
            del handle['physics_data/continuous_data/high_frequency_poses/main_camera_pose']
        elif mutation == 'raw_nan_camera':
            handle['physics_data/continuous_data/high_frequency_poses/main_camera_pose/pose'][0, 0] = np.nan
        else:
            del handle['physics_data/intermittent_data/voxels_removed/voxel_time_stamp']
    result = _audit(raw, tmp_path)
    assert not result['ready_for_frame_extraction']
    assert result['hdf5']['reader_error']
    assert result['hdf5']['frame_count_total'] is None
    assert result['hdf5']['removed_voxel_row_count_total'] is None
    chunk = result['hdf5']['chunks'][0]
    assert chunk['readable'] and chunk['datasets']
    assert chunk['chunk_role'] == 'unknown' and not chunk['canonical_valid']


def test_fractional_removed_timestamp_index_is_not_truncated(tmp_path):
    path = _chunk(tmp_path / 'case.hdf5', [0., .04], schema='legacy', removal_time=.02)
    with h5py.File(path, 'a') as handle:
        handle['voxels_removed/voxel_removed'][0, 0] = .5
    result = inspect_hdf5(path, hash_files=False)
    assert result['readable']
    assert not result['canonical_valid']
    assert result['removed_timestamp_indices_valid'] is False
    assert result['removed_timestamp_index_min'] == .5


@pytest.mark.parametrize('times', [[0., 0.], [0., float('nan')], [1., 0.]])
def test_invalid_frame_times_are_not_fixed_by_filtering_or_sorting(tmp_path, times):
    path = _chunk(tmp_path / 'case.hdf5', times, schema='legacy')
    result = inspect_hdf5(path, hash_files=False)
    assert result['readable'] and not result['canonical_valid']
    assert result['frame_count'] is None
    assert 'strictly increasing' in result['error']


def test_complete_legacy_and_known_metadata_keep_existing_contract(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    _chunk(raw / 'legacy.hdf5', [0., .04], schema='legacy', removal_time=.02)
    with h5py.File(raw / 'terminal.hdf5', 'w') as handle:
        handle.create_group('metadata')
    result = _audit(raw, tmp_path)
    assert result['ready_for_frame_extraction']
    assert result['camera_dependent_evidence_complete']
    assert result['hdf5']['frame_count_total'] == 2
    assert result['hdf5']['format'] == 'legacy'
    terminal = next(item for item in result['hdf5']['chunks'] if item['file_name'] == 'terminal.hdf5')
    assert terminal['chunk_role'] == 'metadata_only'
    assert terminal['canonical_valid'] and terminal['frame_count'] == 0


def test_missing_legacy_camera_is_not_counted_as_valid_empty_stream(tmp_path):
    path = _chunk(tmp_path / 'case.hdf5', [0., .04], schema='legacy')
    with h5py.File(path, 'a') as handle:
        del handle['data/pose_main_camera']
    result = inspect_hdf5(path, hash_files=False)
    assert result['readable'] and not result['canonical_valid']
    assert result['camera_pose_count'] is None
    assert 'Missing canonical field: data/pose_main_camera' in result['error']


@pytest.mark.parametrize('unknown_path', [
    'vision_data/new_camera_frames', 'physics_data/intermittent_data/removed_points/rows',
])
def test_unknown_nested_v3_stream_is_not_a_metadata_only_or_absent_event(tmp_path, unknown_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    _chunk(raw / 'valid.hdf5', [0., .04], camera_times=[0., .04])
    with h5py.File(raw / 'unknown.hdf5', 'w') as handle:
        handle['metadata_v3/voxel_volume'] = .008
        handle[unknown_path] = np.ones((2, 4))
    result = _audit(raw, tmp_path)
    assert not result['ready_for_frame_extraction']
    assert result['hdf5']['reader_error']
    assert result['hdf5']['removed_voxel_row_count_total'] is None


def test_legacy_and_v3_sibling_chunks_are_rejected_as_mixed(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    _chunk(raw / 'legacy.hdf5', [0., .04], schema='legacy')
    _chunk(raw / 'v3.hdf5', [.08, .12], camera_times=[.08, .12])
    result = _audit(raw, tmp_path)
    assert not result['ready_for_frame_extraction']
    assert 'Mixed legacy and v3' in result['hdf5']['reader_error']
    assert result['hdf5']['frame_count_total'] is None


def test_missing_camera_cannot_report_complete_camera_evidence(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    path = _chunk(raw / 'legacy.hdf5', [0., .04], schema='legacy')
    with h5py.File(path, 'a') as handle:
        del handle['data/pose_main_camera']
    result = _audit(raw, tmp_path)
    assert not result['ready_for_frame_extraction']
    assert not result['camera_dependent_evidence_complete']
