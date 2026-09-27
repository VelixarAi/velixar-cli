import json
import unittest
from unittest.mock import patch
from click.testing import CliRunner
import velixar_vou as v

class CountTests(unittest.TestCase):
    def invoke(self, payload, args=None):
        with patch.object(v, '_get', return_value=payload) as get:
            result=CliRunner().invoke(v.vou, ['count']+(args or []))
            get.assert_called_once_with('summary', since=None, until=None)
            return result
    def test_explicit_zero_and_pending(self):
        r=self.invoke(dict(workspace_id='ws',total_vou=0,unnormalized_events=2,meter_gaps=0))
        self.assertEqual(r.exit_code,0);self.assertIn('VOU 0 |',r.output)
        self.assertIn('pending 2',r.output);self.assertIn('NOT FOR BILLING',r.output)
    def test_absent_is_unknown(self):
        r=self.invoke(dict(workspace_id='ws'))
        self.assertIn('VOU Unknown',r.output);self.assertIn('gaps Unknown',r.output)
    def test_invalid_amount_is_never_zero(self):
        for value in [True,-1,float('nan'),float('inf'),'3']:
            self.assertEqual(v._measured_number(value),'Unknown')
    def test_json_retains_server_contract(self):
        data=dict(workspace_id='ws',total_vou=1.25,unnormalized_events=3,schedule_ids=['beta'])
        r=self.invoke(data,['--format','json'])
        self.assertEqual(json.loads(r.output),data)
    def test_missing_workspace_refuses(self):
        self.assertNotEqual(self.invoke(dict(total_vou=5)).exit_code,0)
    def test_failure_after_refresh_does_not_repeat_stale_total(self):
        with patch.object(v,'_get',side_effect=[dict(workspace_id='ws',total_vou=8),RuntimeError('secret')]),patch.object(v.time,'sleep'):
            r=CliRunner().invoke(v.vou,['count','--watch'])
        self.assertNotEqual(r.exit_code,0);self.assertEqual(r.output.count('VOU 8 |'),1)
        self.assertIn('VOU unavailable',r.output);self.assertNotIn('secret',r.output)
    def test_server_weight_basis_and_partial_coverage(self):
        r=self.invoke(dict(workspace_id='ws', total_vou=3, weight_basis='MEASURED', coverage=dict(complete=True,truncated=False)))
        self.assertIn('MEASURED weights',r.output)
        r=self.invoke(dict(workspace_id='ws',total_vou=3,coverage=dict(complete=False,truncated=True)))
        self.assertNotEqual(r.exit_code,0);self.assertNotIn('VOU 3 |',r.output)

    def test_summary_renders_server_task_amount_without_recalculation(self):
        from rich.console import Console
        data=dict(workspace_id='ws',total_vou=9,weight_basis='MIXED',vou_by_task_model_provider=[
            dict(task_type='coding',model_id='model-x',provider='foundry',vou_amount=1.234567,weight_basis='ASSUMPTION')])
        with patch.object(v,'_get',return_value=data),patch.object(v,'console',Console(width=140,force_terminal=False)):
            r=CliRunner().invoke(v.vou,['summary'])
        self.assertEqual(r.exit_code,0);self.assertIn('1.234567',r.output)
        self.assertIn('coding',r.output);self.assertIn('MIXED',r.output)

    def test_no_provider_endpoint(self):
        with patch.object(v,'_get',return_value=dict(workspace_id='ws',total_vou=2)) as get:
            r=CliRunner().invoke(v.vou,['count','--since','2026-09-27T00:00:00Z'])
        self.assertEqual(r.exit_code,0)
        get.assert_called_once_with('summary',since='2026-09-27T00:00:00Z',until=None)

if __name__=='__main__':unittest.main()
