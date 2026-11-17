from __future__ import annotations
import datetime as dt
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from ..dimensional_model.mart import TalentMart

class RealizationError(ValueError):
    pass

def _parse_day(value: Any) -> dt.date | None:
    if value is None or value == '':
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])

def _month_start(day: dt.date) -> dt.date:
    return dt.date(day.year, day.month, 1)

def _shift_months(month_start: dt.date, months: int) -> dt.date:
    if month_start.day != 1:
        raise RealizationError('month anchor must be the first of a month')
    index = month_start.year * 12 + (month_start.month - 1) + months
    return dt.date(index // 12, index % 12 + 1, 1)

def _overlaps(start: dt.date, end: dt.date | None, window_start: dt.date, window_end: dt.date) -> bool:
    right = end if end is not None else dt.date.max
    return start < window_end and right >= window_start

@dataclass(frozen=True)
class RealizationConfig:
    window_months: int = 3
    as_of: Any = None
    job_statuses: tuple[str, ...] = ('active', 'received', 'validated')
    training_statuses: tuple[str, ...] = ('active', 'completed', 'received', 'validated')
    intent_statuses: tuple[str, ...] = ('active', 'received', 'validated')
    def __post_init__(self) -> None:
        if int(self.window_months) < 1:
            raise RealizationError('window_months must be positive')

class SkillRealizationAnalyzer:
    '''Skill supply-demand realization analysis from completion cohort to job window.

    Instead of placing graduation counts next to same-month vacancies, each training
    completion cohort (completion month) is matched against the job window of
    ``window_months`` months that follows it. Skill, region and industry codes are
    resolved through the slowly-changing dimension versions effective on each
    record's own date, so a skill renamed by a version adjustment still matches its
    canonical identity while historical records keep the caliber of their time.

    Counting rules:
    - repeated observations of the same vacancy (same natural key) are deduplicated,
      keeping the latest observation;
    - the same person's multiple intents count once; realization never exceeds
      distinct local persons nor deduplicated openings;
    - cross-region inflow/outflow persons are excluded from local supply and are
      reported separately in the flows block;
    - openings are allocated to cohorts in chronological order, so the same opening
      cannot be realized twice across cohort windows;
    - unresolvable codes are not silently dropped: they are reported in the
      ``incomparable`` buckets (dimension not comparable) with their record ids.
    '''

    def __init__(self, config: RealizationConfig | None = None):
        self.config = config or RealizationConfig()
        self.mart = TalentMart()

    # ---------- field extraction ----------
    @staticmethod
    def _attributes(record: Mapping[str, Any]) -> Mapping[str, Any]:
        return record.get('attributes') or {}

    def skills_of(self, record: Mapping[str, Any]) -> tuple[str, ...]:
        attributes = self._attributes(record)
        for key in ('skill_codes', 'skills', 'skill_code', 'skill'):
            value = attributes.get(key)
            if value in (None, '', []):
                value = record.get(key)
            if value not in (None, '', []):
                if isinstance(value, str):
                    return (value,)
                return tuple(str(item) for item in value)
        category = str(record.get('category') or '').strip()
        return () if category in ('', 'default') else (category,)

    def region_of(self, record: Mapping[str, Any]) -> Any:
        attributes = self._attributes(record)
        return attributes.get('region_code') or attributes.get('region') or attributes.get('target_region') or record.get('region_code') or record.get('region')

    def industry_of(self, record: Mapping[str, Any]) -> Any:
        attributes = self._attributes(record)
        return attributes.get('industry_code') or attributes.get('industry') or record.get('industry_code')

    def persons_of(self, record: Mapping[str, Any]) -> tuple[str, ...]:
        attributes = self._attributes(record)
        for key in ('graduates', 'person_ids', 'graduate_ids'):
            value = attributes.get(key)
            if value:
                return tuple(str(item) for item in value)
        return ()

    def person_of(self, record: Mapping[str, Any]) -> Any:
        attributes = self._attributes(record)
        return attributes.get('person_id') or attributes.get('seeker_id') or record.get('owner_id')

    def natural_key_of(self, record: Mapping[str, Any], kind: str) -> str:
        attributes = self._attributes(record)
        return str(attributes.get('natural_key') or attributes.get('source_key') or record.get(f'{kind}_id') or record.get('id'))

    def observed_at_of(self, record: Mapping[str, Any]) -> str:
        attributes = self._attributes(record)
        return str(attributes.get('observed_at') or attributes.get('received_at') or record.get('updated_at') or '')

    def completion_of(self, record: Mapping[str, Any]) -> dt.date | None:
        attributes = self._attributes(record)
        for key in ('completed_at', 'completion_date', 'graduated_at'):
            day = _parse_day(attributes.get(key))
            if day is not None:
                return day
        return _parse_day(record.get('effective_to')) or _parse_day(record.get('effective_from'))

    # ---------- dimension resolution ----------
    def _version_row(self, versions: Sequence[Mapping[str, Any]], code: Any, day: dt.date) -> tuple[Mapping[str, Any] | None, str | None]:
        matches = []
        for row in versions or []:
            if str(row.get('code')) != str(code):
                continue
            start = _parse_day(row.get('valid_from'))
            end = _parse_day(row.get('valid_to'))
            if start is not None and start <= day and (end is None or day <= end):
                matches.append(row)
        if len(matches) != 1:
            return None, 'dimension_resolution_' + ('missing' if not matches else 'ambiguous')
        return matches[0], None

    def _resolve(self, dimensions: Mapping[str, Any], kind: str, code: Any, day: dt.date) -> tuple[str | None, str | None, str | None]:
        '''Resolve a business code under the caliber effective on ``day``.

        Returns (canonical, label, error). Without a version table the code is its
        own caliber; with one, the canonical identity follows the version row in
        effect on that date (renames share a canonical_code).'''
        if code in (None, ''):
            return None, None, f'missing_{kind}'
        versions = (dimensions or {}).get(kind) or []
        if not versions:
            return str(code), str(code), None
        row, error = self._version_row(versions, code, day)
        if error is not None:
            return None, str(code), error
        canonical = row.get('canonical_code') or row.get('canonical') or row.get('canonical_key') or row.get('code')
        return str(canonical), str(row.get('code')), None

    # ---------- normalization ----------
    def _normalize_intents(self, intents: Sequence[Mapping[str, Any]], dimensions: Mapping[str, Any], as_of: dt.date | None):
        person_regions: dict[str, set[str]] = {}
        person_intents: dict[str, list[str]] = {}
        issues = []
        used = 0
        for record in intents:
            if str(record.get('status') or '') not in self.config.intent_statuses:
                continue
            intent_id = str(record.get('intent_id') or record.get('id') or '')
            day = _parse_day(record.get('effective_from'))
            if day is None:
                issues.append({'intent_id': intent_id, 'reason': 'missing_effective_from'})
                continue
            if as_of is not None and day > as_of:
                continue
            person = self.person_of(record)
            region, _, error = self._resolve(dimensions, 'region', self.region_of(record), day)
            if error is not None:
                issues.append({'intent_id': intent_id, 'reason': error})
                continue
            if person in (None, ''):
                issues.append({'intent_id': intent_id, 'reason': 'missing_person'})
                continue
            used += 1
            person = str(person)
            person_regions.setdefault(person, set()).add(region)
            person_intents.setdefault(person, []).append(intent_id)
        return person_regions, person_intents, issues, used

    def _normalize_trainings(self, trainings: Sequence[Mapping[str, Any]], dimensions: Mapping[str, Any], as_of: dt.date | None):
        supply = []
        incomparable = []
        industry_supply: dict[str, dict[str, int]] = {}
        for record in trainings:
            if str(record.get('status') or '') not in self.config.training_statuses:
                continue
            training_id = str(record.get('training_id') or record.get('id') or '')
            completed = self.completion_of(record)
            if completed is None:
                incomparable.append({'training_id': training_id, 'reason': 'missing_completion_date', 'region': 'unknown', 'graduates': int(record.get('quantity') or 0)})
                continue
            if as_of is not None and completed > as_of:
                continue
            region, region_label, region_error = self._resolve(dimensions, 'region', self.region_of(record), completed)
            skills = self.skills_of(record)
            persons = self.persons_of(record)
            graduates = len(persons) if persons else int(record.get('quantity') or 0)
            base = {'training_id': training_id, 'completed': completed.isoformat(), 'cohort': f'{completed:%Y-%m}',
                    'graduates': graduates, 'region': region or 'unknown'}
            if region_error is not None:
                incomparable.append(base | {'reason': region_error, 'skill_codes': list(skills)})
                continue
            if not skills:
                incomparable.append(base | {'reason': 'missing_skill'})
                continue
            resolved = []
            failure = None
            for code in skills:
                canonical, label, error = self._resolve(dimensions, 'skill', code, completed)
                if error is not None:
                    failure = {'code': str(code), 'reason': error}
                    break
                resolved.append((canonical, label))
            if failure is not None:
                incomparable.append(base | {'reason': 'skill_unresolved_at_completion', 'detail': failure})
                continue
            industry = None
            industry_code = self.industry_of(record)
            if industry_code not in (None, ''):
                industry, _, _ = self._resolve(dimensions, 'industry', industry_code, completed)
            supply.append({'training_id': training_id, 'region': region, 'region_label': region_label,
                           'completed': completed, 'cohort': base['cohort'], 'skills': tuple(resolved),
                           'persons': persons, 'untracked': 0 if persons else graduates, 'industry': industry})
            if industry is not None:
                industry_supply.setdefault(region, {}).setdefault(industry, 0)
                industry_supply[region][industry] += graduates
        return supply, incomparable, industry_supply

    def _normalize_jobs(self, jobs: Sequence[Mapping[str, Any]], dimensions: Mapping[str, Any], as_of: dt.date | None):
        candidates = []
        for record in jobs:
            if str(record.get('status') or '') not in self.config.job_statuses:
                continue
            start = _parse_day(record.get('effective_from'))
            if start is None:
                continue
            if as_of is not None and start > as_of:
                continue
            candidates.append(record)
        observations: dict[str, int] = {}
        for record in candidates:
            key = self.natural_key_of(record, 'job')
            observations[key] = observations.get(key, 0) + 1
        ordered = sorted(candidates, key=lambda record: (self.observed_at_of(record), int(record.get('version') or 0), str(record.get('job_id') or record.get('id') or '')))
        chosen: dict[str, Mapping[str, Any]] = {}
        duplicates = []
        for record in ordered:
            key = self.natural_key_of(record, 'job')
            if key in chosen:
                duplicates.append({'job_id': str(chosen[key].get('job_id') or chosen[key].get('id')), 'natural_key': key,
                                   'kept': str(record.get('job_id') or record.get('id'))})
            chosen[key] = record
        demand = []
        incomparable = []
        industry_demand: dict[str, dict[str, int]] = {}
        for key, record in sorted(chosen.items()):
            start = _parse_day(record.get('effective_from'))
            end = _parse_day(record.get('effective_to'))
            job_id = str(record.get('job_id') or record.get('id') or key)
            openings = int(record.get('quantity') or 0)
            region, region_label, region_error = self._resolve(dimensions, 'region', self.region_of(record), start)
            base = {'job_id': job_id, 'natural_key': key, 'openings': openings, 'region': region or 'unknown', 'start': start.isoformat()}
            if region_error is not None:
                incomparable.append(base | {'reason': region_error})
                continue
            skills = self.skills_of(record)
            if not skills:
                incomparable.append(base | {'reason': 'missing_skill'})
                continue
            resolved = []
            failure = None
            for code in skills:
                canonical, label, error = self._resolve(dimensions, 'skill', code, start)
                if error is not None:
                    failure = {'code': str(code), 'reason': error}
                    break
                resolved.append((canonical, label))
            if failure is not None:
                incomparable.append(base | {'reason': 'skill_unresolved_at_job_start', 'detail': failure})
                continue
            industry = None
            industry_code = self.industry_of(record)
            if industry_code not in (None, ''):
                industry, _, _ = self._resolve(dimensions, 'industry', industry_code, start)
            demand.append({'job_id': job_id, 'natural_key': key, 'region': region, 'skills': tuple(resolved),
                           'start': start, 'end': end, 'openings': openings, 'observations': observations.get(key, 1),
                           'industry': industry})
            if industry is not None:
                industry_demand.setdefault(region, {}).setdefault(industry, 0)
                industry_demand[region][industry] += openings
        return demand, incomparable, duplicates, industry_demand, len(chosen)

    # ---------- analysis ----------
    def analyze(self, jobs: Sequence[Mapping[str, Any]], trainings: Sequence[Mapping[str, Any]], intents: Sequence[Mapping[str, Any]],
                dimensions: Mapping[str, Any] | None = None, as_of: Any = None) -> dict[str, Any]:
        jobs, trainings, intents = list(jobs), list(trainings), list(intents)
        dimensions = dimensions or {}
        as_of_day = _parse_day(as_of) if as_of is not None else _parse_day(self.config.as_of)
        person_regions, person_intents, intent_issues, intents_used = self._normalize_intents(intents, dimensions, as_of_day)
        supply, incomparable_supply, industry_supply = self._normalize_trainings(trainings, dimensions, as_of_day)
        demand, incomparable_demand, duplicates, industry_demand, jobs_deduped = self._normalize_jobs(jobs, dimensions, as_of_day)

        cells: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in supply:
            for canonical, label in item['skills']:
                key = (item['region'], canonical, item['cohort'])
                cell = cells.setdefault(key, {'region': item['region'], 'canonical_skill': canonical, 'cohort': item['cohort'],
                                              'training_ids': [], 'supply_labels': set(), 'persons': set(), 'untracked': 0})
                cell['training_ids'].append(item['training_id'])
                cell['supply_labels'].add(label)
                cell['persons'].update(item['persons'])
                cell['untracked'] += item['untracked']

        flows: dict[tuple[str, str], set[str]] = {}
        for cell in cells.values():
            outflow = {}
            for person in cell['persons']:
                targets = person_regions.get(person)
                if targets and cell['region'] not in targets:
                    outflow[person] = sorted(targets)
            cell['outflow_persons'] = outflow
            cell['local_persons'] = sorted(cell['persons'] - set(outflow))
            cell['graduates'] = len(cell['persons']) + cell['untracked']
            cell['outflow'] = len(outflow)
            cell['local_supply'] = len(cell['local_persons']) + cell['untracked']
            for person, targets in outflow.items():
                for target in targets:
                    flows.setdefault((cell['region'], target), set()).add(person)

        windows: dict[str, tuple[dt.date, dt.date]] = {}
        def window_of(cohort: str) -> tuple[dt.date, dt.date]:
            if cohort not in windows:
                start = _month_start(dt.date.fromisoformat(cohort + '-01'))
                windows[cohort] = (start, _shift_months(start, self.config.window_months))
            return windows[cohort]

        demand_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in demand:
            for canonical, label in item['skills']:
                entry = dict(item)
                entry['label'] = label
                entry['remaining'] = item['openings']
                demand_index.setdefault((item['region'], canonical), []).append(entry)
        for rows in demand_index.values():
            rows.sort(key=lambda row: (row['start'], row['job_id']))

        cohort_rows = []
        for (region, canonical, cohort), cell in sorted(cells.items()):
            window_start, window_end = window_of(cohort)
            pool = demand_index.get((region, canonical), [])
            in_window = [job for job in pool if _overlaps(job['start'], job['end'], window_start, window_end)]
            out_window = [job for job in pool if not _overlaps(job['start'], job['end'], window_start, window_end)]
            eligible = [job for job in in_window if job['remaining'] > 0]
            available = sum(job['remaining'] for job in eligible)
            realized = min(cell['local_supply'], available)
            left = realized
            allocations = []
            for job in eligible:
                if left <= 0:
                    break
                take = min(job['remaining'], left)
                job['remaining'] -= take
                left -= take
                allocations.append({'job_id': job['job_id'], 'openings_used': take})
            unrealized = cell['local_supply'] - realized
            openings_in_window = sum(job['openings'] for job in in_window)
            causes: dict[str, Any] = {}
            if unrealized > 0:
                if openings_in_window == 0:
                    if out_window:
                        causes['timing_misaligned'] = {'openings': sum(job['openings'] for job in out_window),
                                                       'job_ids': sorted(job['job_id'] for job in out_window)}
                    incomparable_here = [row for row in incomparable_demand if row['region'] == region]
                    if incomparable_here:
                        causes['dimension_incomparable'] = {'openings': sum(row['openings'] for row in incomparable_here),
                                                            'job_ids': sorted(row['job_id'] for row in incomparable_here)}
                    if not causes:
                        causes['no_matching_demand'] = {'graduates_unrealized': unrealized}
                else:
                    causes['openings_below_supply'] = {'openings_in_window': openings_in_window, 'available_openings': available,
                                                       'graduates_unrealized': unrealized}
                    if out_window:
                        causes['timing_misaligned'] = {'openings': sum(job['openings'] for job in out_window),
                                                       'job_ids': sorted(job['job_id'] for job in out_window)}
            demand_labels = sorted({job['label'] for job in in_window})
            supply_labels = sorted(cell['supply_labels'])
            cohort_rows.append({'region': region, 'canonical_skill': canonical, 'cohort': cohort,
                                'window': {'start': window_start.isoformat(), 'end': window_end.isoformat()},
                                'graduates': cell['graduates'], 'outflow': cell['outflow'], 'local_supply': cell['local_supply'],
                                'openings_in_window': openings_in_window, 'realized': realized, 'unrealized': unrealized,
                                'supply_labels': supply_labels, 'demand_labels': demand_labels,
                                'renamed': bool(supply_labels and demand_labels and set(supply_labels).isdisjoint(demand_labels)),
                                'causes': causes,
                                'trace': {'training_ids': sorted(cell['training_ids']),
                                          'job_ids': sorted(job['job_id'] for job in in_window),
                                          'allocations': allocations,
                                          'person_ids': sorted(cell['persons']),
                                          'intent_ids': sorted({intent for person in cell['persons'] for intent in person_intents.get(person, ())})}})

        cohorts_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in cohort_rows:
            cohorts_by_key.setdefault((row['region'], row['canonical_skill']), []).append(row)
        cells_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for (region, canonical, cohort), cell in cells.items():
            cells_by_key.setdefault((region, canonical), []).append(cell)

        demand_rows = []
        for (region, canonical), pool in sorted(demand_index.items()):
            key_cells = cohorts_by_key.get((region, canonical), [])
            raw_cells = cells_by_key.get((region, canonical), [])
            key_windows = [window_of(cell['cohort']) for cell in key_cells]
            total_openings = sum(job['openings'] for job in pool)
            realized_total = sum(cell['realized'] for cell in key_cells)
            covered = sum(job['openings'] for job in pool
                          if any(_overlaps(job['start'], job['end'], start, end) for start, end in key_windows))
            misaligned_jobs = [job for job in pool
                               if not any(_overlaps(job['start'], job['end'], start, end) for start, end in key_windows)]
            causes: dict[str, Any] = {}
            if not key_cells:
                causes['no_training_cohort'] = {'openings': total_openings}
            else:
                misaligned = sum(job['openings'] for job in misaligned_jobs)
                if misaligned:
                    causes['timing_misaligned'] = {'openings': misaligned, 'job_ids': sorted(job['job_id'] for job in misaligned_jobs)}
                shortage = covered - realized_total
                if shortage > 0:
                    causes['supply_shortage'] = {'openings': shortage}
            outflow_persons = set().union(*(set(cell['outflow_persons']) for cell in raw_cells)) if raw_cells else set()
            if outflow_persons:
                causes['cross_region_outflow'] = {'persons': len(outflow_persons)}
            supply_labels = sorted(set().union(*(cell['supply_labels'] for cell in raw_cells))) if raw_cells else []
            demand_labels = sorted({job['label'] for job in pool})
            demand_rows.append({'region': region, 'canonical_skill': canonical,
                                'openings': total_openings, 'realized': realized_total, 'unfilled': total_openings - realized_total,
                                'covered_openings': covered, 'cohorts': [cell['cohort'] for cell in key_cells],
                                'supply_labels': supply_labels, 'demand_labels': demand_labels,
                                'renamed': bool(supply_labels and demand_labels and set(supply_labels).isdisjoint(demand_labels)),
                                'causes': causes,
                                'trace': {'job_ids': sorted(job['job_id'] for job in pool),
                                          'training_ids': sorted({tid for cell in key_cells for tid in cell['trace']['training_ids']})}})

        regions: dict[str, dict[str, Any]] = {}
        def region_bucket(region: str) -> dict[str, Any]:
            return regions.setdefault(region, {'region': region, 'graduates': 0, 'local_supply': 0, 'realized': 0, 'unrealized': 0,
                                               'openings': 0, 'unfilled': 0, 'training_ids': set(), 'job_ids': set(), 'person_ids': set(),
                                               'outflow_persons': set(), 'inflow_persons': set(), 'causes': {}})
        def merge_cause(causes: dict[str, Any], name: str, detail: Mapping[str, Any]) -> None:
            bucket = causes.setdefault(name, {})
            for field, value in detail.items():
                if isinstance(value, int):
                    bucket[field] = bucket.get(field, 0) + value
        for row in cohort_rows:
            bucket = region_bucket(row['region'])
            bucket['graduates'] += row['graduates']
            bucket['local_supply'] += row['local_supply']
            bucket['realized'] += row['realized']
            bucket['unrealized'] += row['unrealized']
            bucket['training_ids'].update(row['trace']['training_ids'])
            bucket['job_ids'].update(row['trace']['job_ids'])
            bucket['person_ids'].update(row['trace']['person_ids'])
        for row in demand_rows:
            bucket = region_bucket(row['region'])
            bucket['openings'] += row['openings']
            bucket['unfilled'] += row['unfilled']
            bucket['job_ids'].update(row['trace']['job_ids'])
            for name, detail in row['causes'].items():
                merge_cause(bucket['causes'], name, detail)
        for (origin, target), persons in flows.items():
            region_bucket(origin)['outflow_persons'].update(persons)
            region_bucket(target)['inflow_persons'].update(persons)
        for row in incomparable_supply:
            merge_cause(region_bucket(row['region'])['causes'], 'dimension_incomparable', {'supply_graduates': int(row.get('graduates') or 0)})
        for row in incomparable_demand:
            merge_cause(region_bucket(row['region'])['causes'], 'dimension_incomparable', {'demand_openings': int(row.get('openings') or 0)})

        region_rows = []
        for region in sorted(regions):
            bucket = regions[region]
            causes = {name: detail for name, detail in sorted(bucket['causes'].items()) if any(detail.values())}
            region_rows.append({'region': region, 'graduates': bucket['graduates'],
                                'outflow': len(bucket['outflow_persons']), 'inflow_excluded': len(bucket['inflow_persons']),
                                'local_supply': bucket['local_supply'], 'openings': bucket['openings'],
                                'realized': bucket['realized'], 'unrealized': bucket['unrealized'], 'unfilled': bucket['unfilled'],
                                'causes': causes,
                                'industries': {'supply': dict(sorted(industry_supply.get(region, {}).items())),
                                               'demand': dict(sorted(industry_demand.get(region, {}).items()))},
                                'trace': {'training_ids': sorted(bucket['training_ids']),
                                          'job_ids': sorted(bucket['job_ids']),
                                          'person_ids': sorted(bucket['person_ids'])}})

        flow_rows = [{'origin': origin, 'target': target, 'persons': len(persons), 'person_ids': sorted(persons)}
                     for (origin, target), persons in sorted(flows.items())]
        return {'as_of': as_of_day.isoformat() if as_of_day is not None else None,
                'window_months': self.config.window_months,
                'cohorts': cohort_rows,
                'demand': demand_rows,
                'regions': region_rows,
                'flows': flow_rows,
                'incomparable': {'supply': incomparable_supply, 'demand': incomparable_demand, 'intents': intent_issues},
                'duplicates': {'jobs': duplicates},
                'counts': {'jobs': len(jobs), 'jobs_deduped': jobs_deduped, 'duplicate_job_observations': len(duplicates),
                           'trainings': len(trainings), 'trainings_used': len(supply) + len(incomparable_supply),
                           'intents': len(intents), 'intents_used': intents_used, 'persons_tracked': len(person_regions)}}

    # ---------- historical snapshots ----------
    def verify_historical_gaps(self, facts: Sequence[Mapping[str, Any]], published: Sequence[Mapping[str, Any]],
                               dimensions: Mapping[str, Any], grain: Sequence[str] = ('industry_key', 'region_key')) -> dict[str, Any]:
        '''Recompute published gap aggregates from their facts under the dimension
        caliber effective on each fact's own event date. Historical snapshots keep
        their original interpretation; this analysis never rewrites them.'''
        def normalize(row: Mapping[str, Any]) -> dict[str, Any]:
            item = dict(row)
            item['key'] = tuple(item['key'])
            return item
        ingested = self.mart.ingest(facts, dimensions)
        recomputed = {tuple(row['key']): row for row in self.mart.aggregate(ingested['accepted'], list(grain))}
        mismatches = []
        for row in published:
            item = normalize(row)
            other = recomputed.get(item['key'])
            if other != item:
                mismatches.append({'key': item['key'], 'published': item, 'recomputed': other})
        return {'unchanged': not mismatches, 'mismatches': mismatches, 'rejected': ingested['rejected']}
