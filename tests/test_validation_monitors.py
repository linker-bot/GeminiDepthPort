"""Counter semantics and negative evidence for external validation monitors."""
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

def module(name):
    spec=importlib.util.spec_from_file_location(name, ROOT/'scripts/validation'/f'{name}.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result

resource=module('resource_monitor')
status=module('service_watch')

class ValidationMonitors(unittest.TestCase):
    def test_cpu_units_and_oom_delta(self):
        a={'cpu_usec':10,'memory_bytes':1000,'memory_peak_bytes':2000,'oom_kill':0}
        b=dict(a,cpu_usec=2000010,memory_bytes=1500,oom_kill=1)
        report=resource.summarize([{'groups':[a]},{'groups':[b]}],2.,1)
        self.assertEqual(report['results'][0]['cpu_cores'],1.)
        self.assertEqual(report['results'][0]['oom_kills'],1)
        self.assertFalse(report['ok'])
    def test_counter_reset_cannot_be_reported_as_cpu_savings(self):
        a={'cpu_usec':10,'memory_bytes':1000,'memory_peak_bytes':2000,'oom_kill':0}
        with self.assertRaises(ValueError):resource.summarize([{'groups':[a]},{'groups':[dict(a,cpu_usec=0)]}],2.,1)
    def fixture(self):
        return {'version':1,'kind':'capture-service-status','state':'ready','ready':True,
                'session_id':'0'*32,'generation':'synthetic-generation','config_sha256':'f'*64,
                'updated_unix_ms':1000,'last_pair_age_ms':20,'restart_attempt':0,
                'pairs_published':10,'pairs_rejected':0,'publish_failures':0}
    def test_status_progress_without_private_identity_export(self):
        v=self.fixture();w=status.Watch(2000,3000);w.observe(v,1100)
        w.observe(dict(v,pairs_published=30,updated_unix_ms=2000),2100)
        report=w.summary();self.assertTrue(report['ok']);self.assertEqual(report['counter_deltas']['pairs_published'],20)
        self.assertNotIn('session_id',report)
    def test_stale_ready_snapshot_is_unhealthy(self):
        w=status.Watch(2000,3000);w.observe(self.fixture(),5000)
        self.assertFalse(w.summary()['ok']);self.assertIn('STALE_STATUS',w.summary()['errors'])
    def test_reopen_is_reported_even_if_new_worker_is_ready(self):
        v=self.fixture();w=status.Watch(2000,3000);w.observe(v,1100)
        w.observe(dict(v,session_id='1'*32,restart_attempt=1,pairs_published=1,updated_unix_ms=2000),2100)
        report=w.summary();self.assertFalse(report['ok']);self.assertEqual(report['session_changes'],1)
    def test_malformed_status_fails_closed(self):
        w=status.Watch(2000,3000);w.observe([],1100);self.assertFalse(w.summary()['ok'])

if __name__=='__main__':unittest.main()
