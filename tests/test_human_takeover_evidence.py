"""Receipt validation only; these synthetic cases are not hardware acceptance."""
import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('human_takeover', Path(__file__).with_name('live_human_takeover.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def evidence():
    return {
        'operation': {'execution': 'cancelled', 'submitted': 12},
        'lease': {'control_epoch': 2},
        'heartbeat_after': {'control_epoch': 3},
        'human_held_observed': {'human_held': True, 'control_epoch': 3},
        'human_released': {'human_held': False},
        'stream_started_ns': 100,
        'fixture_after': {'keys': [['press', 97, 0, 101], ['release', 97, 0, 102]]},
    }


def test_complete_evidence_is_accepted():
    module.verify_takeover(evidence())


@pytest.mark.parametrize('fault', [
    'missing_release', 'still_held', 'no_backend_hold', 'no_app_release',
    'old_app_events', 'only_agent_keys', 'no_input_submitted', 'completed_stream',
    'unchanged_epoch',
])
def test_incomplete_or_unrelated_evidence_never_passes(fault):
    report = copy.deepcopy(evidence())
    if fault == 'missing_release': del report['human_released']
    elif fault == 'still_held': report['human_released']['human_held'] = True
    elif fault == 'no_backend_hold': report['human_held_observed']['human_held'] = False
    elif fault == 'no_app_release': report['fixture_after']['keys'].pop()
    elif fault == 'old_app_events': report['stream_started_ns'] = 200
    elif fault == 'only_agent_keys':
        for event in report['fixture_after']['keys']: event[1] = ord('0')
    elif fault == 'no_input_submitted': report['operation']['submitted'] = 0
    elif fault == 'completed_stream': report['operation']['execution'] = 'finished'
    elif fault == 'unchanged_epoch': report['heartbeat_after']['control_epoch'] = 2
    with pytest.raises(AssertionError): module.verify_takeover(report)
