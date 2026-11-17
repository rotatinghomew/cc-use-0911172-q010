import json
import tempfile
import unittest
from pathlib import Path
from talent_observatory.contracts import canonical_json, fingerprint
from talent_observatory.dimensional_model.mart import TalentMart
from talent_observatory.ingestion.intent_repository import IntentRepository
from talent_observatory.dimensions.training_repository import TrainingRepository
from talent_observatory.metrics.job_repository import JobRepository
from talent_observatory.metrics.realization import RealizationConfig, RealizationError, SkillRealizationAnalyzer
from talent_observatory.reporting import Reporter
from talent_observatory.storage import Database

SKILL_VERSIONS = [
    {'code': 'python-ii', 'surrogate_key': 'sk-py2', 'canonical_code': 'python', 'valid_from': '2026-01-01', 'valid_to': '2026-06-30'},
    {'code': 'python-iii', 'surrogate_key': 'sk-py3', 'canonical_code': 'python', 'valid_from': '2026-07-01'},
    {'code': 'welding', 'surrogate_key': 'sk-weld', 'canonical_code': 'welding', 'valid_from': '2026-01-01'},
]
REGION_VERSIONS = [
    {'code': 'shenyang', 'surrogate_key': 'rg-sy', 'valid_from': '2026-01-01'},
    {'code': 'dalian', 'surrogate_key': 'rg-dl', 'valid_from': '2026-01-01'},
]
INDUSTRY_VERSIONS = [
    {'code': 'ai', 'surrogate_key': 'in-ai', 'valid_from': '2026-01-01'},
    {'code': 'equipment', 'surrogate_key': 'in-eq', 'valid_from': '2026-01-01'},
]
DIMENSIONS = {'skill': SKILL_VERSIONS, 'region': REGION_VERSIONS, 'industry': INDUSTRY_VERSIONS}

def record(kind, identifier, status, start, end=None, quantity=0, attributes=None, owner='owner-1', category='default', version=1):
    return {'kind': kind, f'{kind}_id': identifier, 'owner_id': owner, 'category': category, 'status': status,
            'effective_from': start, 'effective_to': end, 'quantity': quantity, 'amount': '0.00',
            'attributes': attributes or {}, 'version': version}

def training(identifier, region, skill, completed, persons=None, quantity=0, industry=None, status='completed'):
    attributes = {'skill_code': skill, 'region_code': region, 'completed_at': completed}
    if persons:
        attributes['graduates'] = list(persons)
    if industry:
        attributes['industry_code'] = industry
    return record('training', identifier, status, '2026-03-01', '2026-06-30', quantity or len(persons or []), attributes)

def job(identifier, region, skill, start, end, quantity, natural=None, observed='', industry=None, status='active'):
    attributes = {'skill_code': skill, 'region_code': region, 'natural_key': natural or identifier, 'observed_at': observed}
    if industry:
        attributes['industry_code'] = industry
    return record('job', identifier, status, start, end, quantity, attributes)

def intent(identifier, person, region, day='2026-06-25'):
    return record('intent', identifier, 'active', day, None, 0, {'person_id': person, 'region_code': region})

class Scenario:
    jobs = [
        job('j-py-1a', 'shenyang', 'python-iii', '2026-07-01', '2026-09-30', 5, natural='vac-py-01', observed='2026-07-01', industry='ai'),
        job('j-py-1b', 'shenyang', 'python-iii', '2026-07-01', '2026-09-30', 6, natural='vac-py-01', observed='2026-08-01', industry='ai'),
        job('j-weld-1', 'shenyang', 'welding', '2026-05-01', '2026-05-31', 4, industry='equipment'),
        job('j-cnc-1', 'shenyang', 'cnc-operator', '2026-07-01', '2026-08-31', 3),
        job('j-py-dl', 'dalian', 'python-iii', '2026-08-01', '2026-08-31', 8, industry='ai'),
    ]
    trainings = [
        training('t-py-1', 'shenyang', 'python-ii', '2026-06-15', persons=['p01', 'p02', 'p03', 'p04', 'p05', 'p06', 'p07', 'p08'], industry='ai'),
        training('t-weld', 'shenyang', 'welding', '2026-06-20', persons=['p11', 'p12', 'p13', 'p14', 'p15'], industry='equipment'),
        training('t-py-3', 'dalian', 'python-ii', '2026-06-18', persons=['p16', 'p17', 'p18', 'p19', 'p20'], industry='ai'),
        training('t-cnc', 'shenyang', 'legacy-cnc', '2026-06-21', quantity=4),
        training('t-py-2', 'shenyang', 'python-iii', '2026-09-10', persons=['p21', 'p22', 'p23', 'p24'], industry='ai'),
    ]
    intents = [
        intent('i-01', 'p01', 'dalian'),
        intent('i-02', 'p02', 'dalian'),
        intent('i-03', 'p03', 'shenyang'),
        intent('i-04', 'p03', 'shenyang', '2026-06-26'),
        intent('i-05', 'p03', 'shenyang', '2026-06-27'),
        intent('i-16', 'p16', 'dalian'),
        intent('i-17', 'p17', 'dalian'),
        intent('i-18', 'p18', 'dalian'),
        intent('i-19', 'p19', 'dalian'),
        intent('i-20', 'p20', 'dalian'),
    ]

def analyze(**overrides):
    options = {'jobs': Scenario.jobs, 'trainings': Scenario.trainings, 'intents': Scenario.intents, 'dimensions': DIMENSIONS, 'as_of': None}
    options.update(overrides)
    return SkillRealizationAnalyzer().analyze(options['jobs'], options['trainings'], options['intents'], options['dimensions'], as_of=options['as_of'])

def cell(report, region, skill, cohort):
    for row in report['cohorts']:
        if (row['region'], row['canonical_skill'], row['cohort']) == (region, skill, cohort):
            return row
    raise AssertionError(f'cell not found: {region} {skill} {cohort}')

def demand(report, region, skill):
    for row in report['demand']:
        if (row['region'], row['canonical_skill']) == (region, skill):
            return row
    raise AssertionError(f'demand not found: {region} {skill}')

def region(report, name):
    for row in report['regions']:
        if row['region'] == name:
            return row
    raise AssertionError(f'region not found: {name}')

class RealizationTests(unittest.TestCase):
    def test_renamed_skill_realizes_across_versions(self):
        row = cell(analyze(), 'shenyang', 'python', '2026-06')
        self.assertEqual(row['supply_labels'], ['python-ii'])
        self.assertEqual(row['demand_labels'], ['python-iii'])
        self.assertTrue(row['renamed'])
        self.assertEqual(row['window'], {'start': '2026-06-01', 'end': '2026-09-01'})
        self.assertEqual(row['graduates'], 8)
        self.assertEqual(row['local_supply'], 6)
        self.assertEqual(row['openings_in_window'], 6)
        self.assertEqual(row['realized'], 6)
        self.assertEqual(row['unrealized'], 0)

    def test_timing_misalignment_classified(self):
        row = cell(analyze(), 'shenyang', 'welding', '2026-06')
        self.assertEqual(row['openings_in_window'], 0)
        self.assertEqual(row['realized'], 0)
        self.assertEqual(row['unrealized'], 5)
        self.assertEqual(row['causes']['timing_misaligned']['openings'], 4)
        self.assertEqual(row['causes']['timing_misaligned']['job_ids'], ['j-weld-1'])

    def test_dimension_incomparable_buckets(self):
        report = analyze()
        supply = report['incomparable']['supply']
        self.assertEqual([row['training_id'] for row in supply], ['t-cnc'])
        self.assertEqual(supply[0]['reason'], 'skill_unresolved_at_completion')
        self.assertEqual(supply[0]['graduates'], 4)
        demand_rows = report['incomparable']['demand']
        self.assertEqual([row['job_id'] for row in demand_rows], ['j-cnc-1'])
        self.assertEqual(demand_rows[0]['reason'], 'skill_unresolved_at_job_start')
        causes = region(report, 'shenyang')['causes']['dimension_incomparable']
        self.assertEqual(causes['supply_graduates'], 4)
        self.assertEqual(causes['demand_openings'], 3)

    def test_duplicate_job_observations_do_not_inflate(self):
        report = analyze()
        self.assertEqual(demand(report, 'shenyang', 'python')['openings'], 6)
        self.assertEqual(report['counts']['duplicate_job_observations'], 1)
        dropped = report['duplicates']['jobs'][0]
        self.assertEqual(dropped['job_id'], 'j-py-1a')
        self.assertEqual(dropped['kept'], 'j-py-1b')
        self.assertEqual(dropped['natural_key'], 'vac-py-01')

    def test_multiple_intents_same_person_do_not_inflate(self):
        row = cell(analyze(), 'shenyang', 'python', '2026-06')
        self.assertEqual(row['local_supply'], 6)
        self.assertEqual(len(row['trace']['person_ids']), 8)
        self.assertEqual(len(row['trace']['intent_ids']), 5)
        self.assertEqual(row['realized'], 6)

    def test_cross_region_flow_not_counted_as_local_supply(self):
        report = analyze()
        shenyang = region(report, 'shenyang')
        self.assertEqual(shenyang['outflow'], 2)
        self.assertEqual(shenyang['local_supply'], 15)
        dalian = region(report, 'dalian')
        self.assertEqual(dalian['inflow_excluded'], 2)
        self.assertEqual(dalian['local_supply'], 5)
        self.assertEqual(report['flows'], [{'origin': 'shenyang', 'target': 'dalian', 'persons': 2, 'person_ids': ['p01', 'p02']}])
        self.assertEqual(demand(report, 'shenyang', 'python')['causes']['cross_region_outflow']['persons'], 2)

    def test_realized_allocation_earlier_cohort_first(self):
        report = analyze()
        later = cell(report, 'shenyang', 'python', '2026-09')
        self.assertEqual(later['openings_in_window'], 6)
        self.assertEqual(later['realized'], 0)
        self.assertEqual(later['unrealized'], 4)
        self.assertEqual(later['causes']['openings_below_supply']['available_openings'], 0)
        total = demand(report, 'shenyang', 'python')
        self.assertEqual(total['realized'], 6)
        self.assertLessEqual(total['realized'], total['openings'])
        self.assertEqual(total['unfilled'], 0)

    def test_supply_shortage_cause(self):
        row = demand(analyze(), 'dalian', 'python')
        self.assertEqual(row['openings'], 8)
        self.assertEqual(row['realized'], 5)
        self.assertEqual(row['unfilled'], 3)
        self.assertEqual(row['causes']['supply_shortage']['openings'], 3)

    def test_demand_timing_misaligned_when_no_cohort_window(self):
        row = demand(analyze(), 'shenyang', 'welding')
        self.assertEqual(row['covered_openings'], 0)
        self.assertEqual(row['unfilled'], 4)
        self.assertEqual(row['causes']['timing_misaligned']['openings'], 4)

    def test_region_summary_traces_to_records(self):
        report = analyze()
        shenyang = region(report, 'shenyang')
        self.assertEqual(shenyang['graduates'], 17)
        self.assertEqual(shenyang['realized'], 6)
        self.assertEqual(shenyang['unrealized'], 9)
        self.assertEqual(shenyang['openings'], 10)
        self.assertEqual(shenyang['unfilled'], 4)
        self.assertEqual(set(shenyang['trace']['training_ids']), {'t-py-1', 't-weld', 't-py-2'})
        self.assertEqual(set(shenyang['trace']['job_ids']), {'j-py-1b', 'j-weld-1'})
        self.assertEqual(shenyang['industries']['supply'], {'ai': 12, 'equipment': 5})
        self.assertEqual(shenyang['industries']['demand'], {'ai': 6, 'equipment': 4})
        dalian = region(report, 'dalian')
        self.assertEqual(set(dalian['trace']['training_ids']), {'t-py-3'})
        self.assertEqual(set(dalian['trace']['job_ids']), {'j-py-dl'})
        june = cell(report, 'shenyang', 'python', '2026-06')
        self.assertEqual(june['trace']['training_ids'], ['t-py-1'])
        self.assertEqual(june['trace']['job_ids'], ['j-py-1b'])
        self.assertEqual(june['trace']['allocations'], [{'job_id': 'j-py-1b', 'openings_used': 6}])

    def test_as_of_point_in_time(self):
        report = analyze(as_of='2026-06-30')
        self.assertEqual(report['as_of'], '2026-06-30')
        cohorts = {(row['region'], row['canonical_skill'], row['cohort']) for row in report['cohorts']}
        self.assertNotIn(('shenyang', 'python', '2026-09'), cohorts)
        row = cell(report, 'shenyang', 'python', '2026-06')
        self.assertEqual(row['openings_in_window'], 0)
        self.assertIn('no_matching_demand', row['causes'])
        self.assertEqual(demand(report, 'shenyang', 'welding')['openings'], 4)
        self.assertEqual(len(report['demand']), 1)

    def test_window_months_configurable(self):
        analyzer = SkillRealizationAnalyzer(RealizationConfig(window_months=1))
        report = analyzer.analyze(Scenario.jobs, Scenario.trainings, Scenario.intents, DIMENSIONS)
        row = cell(report, 'shenyang', 'python', '2026-06')
        self.assertEqual(row['window'], {'start': '2026-06-01', 'end': '2026-07-01'})
        self.assertEqual(row['openings_in_window'], 0)
        self.assertIn('timing_misaligned', row['causes'])

    def test_invalid_window_rejected(self):
        with self.assertRaises(RealizationError):
            RealizationConfig(window_months=0)

class HistoricalSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'db.sqlite3')
        self.facts = [
            {'source': 'jobs', 'source_key': 'j1', 'event_date': '2026-05-10', 'industry_code': 'ai', 'region_code': 'shenyang', 'openings': 6, 'seekers': 2},
            {'source': 'jobs', 'source_key': 'j2', 'event_date': '2026-05-12', 'industry_code': 'ai', 'region_code': 'shenyang', 'openings': 4, 'seekers': 5},
        ]
        self.dimensions = {'industry': [{'code': 'ai', 'surrogate_key': 'in-ai', 'valid_from': '2026-01-01', 'valid_to': '2026-10-31'},
                                        {'code': 'ai-plus', 'surrogate_key': 'in-ai2', 'valid_from': '2026-11-01'}],
                           'region': REGION_VERSIONS}
        mart = TalentMart()
        self.published = mart.aggregate(mart.ingest(self.facts, self.dimensions)['accepted'], ['industry_key', 'region_key'])

    def tearDown(self):
        self.temp.cleanup()

    def test_historical_gaps_not_rewritten(self):
        content = {'facts': self.facts, 'aggregates': self.published}
        version = self.db.save_snapshot('mart_gap', 'annual-2026', content, fingerprint(content))
        self.assertEqual(version, 1)
        report = analyze()
        self.assertTrue(report['regions'])
        stored = self.db.load_snapshot('mart_gap', 'annual-2026', 1)['content']
        self.assertEqual(stored, json.loads(canonical_json(content)))
        analyzer = SkillRealizationAnalyzer()
        check = analyzer.verify_historical_gaps(self.facts, stored['aggregates'], self.dimensions)
        self.assertTrue(check['unchanged'])
        self.assertEqual(check['mismatches'], [])
        gap = self.published[0]['gap']
        self.assertEqual(gap, 3)
        tampered = [dict(row, gap=gap + 1) for row in self.published]
        self.assertFalse(analyzer.verify_historical_gaps(self.facts, tampered, self.dimensions)['unchanged'])

    def test_new_analysis_uses_own_snapshot_namespace(self):
        content = {'facts': self.facts, 'aggregates': self.published}
        self.db.save_snapshot('mart_gap', 'annual-2026', content, fingerprint(content))
        report = analyze()
        version = self.db.save_snapshot('skill_realization', 'annual-2026', report, fingerprint(report))
        self.assertEqual(version, 1)
        original = self.db.load_snapshot('mart_gap', 'annual-2026')
        self.assertEqual(original['version'], 1)
        self.assertEqual(original['content'], json.loads(canonical_json(content)))

class ReporterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'db.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_reporter_skill_realization(self):
        jobs = JobRepository(self.db)
        trainings = TrainingRepository(self.db)
        intents = IntentRepository(self.db)
        jobs.create({'job_id': 'j1', 'owner_id': 'ent-1', 'status': 'active', 'effective_from': '2026-07-01',
                     'effective_to': '2026-09-30', 'quantity': 2,
                     'attributes': {'skill_code': 'python-iii', 'region_code': 'shenyang', 'natural_key': 'vac-1', 'industry_code': 'ai'}})
        trainings.create({'training_id': 't1', 'owner_id': 'school-1', 'status': 'completed', 'effective_from': '2026-03-01',
                          'effective_to': '2026-06-15', 'quantity': 3,
                          'attributes': {'skill_code': 'python-ii', 'region_code': 'shenyang', 'graduates': ['p01', 'p02', 'p03'], 'industry_code': 'ai'}})
        intents.create({'intent_id': 'i1', 'owner_id': 'p01', 'status': 'active', 'effective_from': '2026-06-25',
                        'attributes': {'person_id': 'p01', 'region_code': 'dalian'}})
        report = Reporter(self.db).skill_realization(DIMENSIONS)
        shenyang = region(report, 'shenyang')
        self.assertEqual(shenyang['graduates'], 3)
        self.assertEqual(shenyang['outflow'], 1)
        self.assertEqual(shenyang['local_supply'], 2)
        self.assertEqual(shenyang['realized'], 2)
        self.assertEqual(shenyang['unfilled'], 0)
        self.assertEqual(report['flows'][0]['origin'], 'shenyang')
        self.assertEqual(report['flows'][0]['target'], 'dalian')
        row = cell(report, 'shenyang', 'python', '2026-06')
        self.assertTrue(row['renamed'])
        self.assertEqual(row['trace']['training_ids'], ['t1'])
        self.assertEqual(row['trace']['job_ids'], ['j1'])
        version = self.db.save_snapshot('skill_realization', 'review-2026', report, fingerprint(report))
        self.assertEqual(version, 1)

if __name__ == '__main__':
    unittest.main()
