"""Durable, replayable planner traces, independent of the plan transaction."""
from __future__ import annotations

import copy
import inspect
import json
import time

EXPECTED_STAGES = ('HP_COMFORT', 'INPUT_COVERAGE', 'RCE_RAW', 'FORECAST', 'WINDOWS', 'WINDOW_CANDIDATES',
                   'LOAD', 'PV', 'TARGET_COMMITMENT', 'SLOT_BALANCE', 'DISPATCH',
                   'REPLENISHMENT', 'FLEX_SURPLUS', 'SOC', 'VALIDATE')


class PlannerTrace:
    def __init__(self, attempt_id):
        self.attempt_id = attempt_id
        self.started = time.monotonic()
        self.entries = []

    def add(self, stage, status, **evidence):
        self.entries.append(dict(sequence=len(self.entries) + 1, stage=stage,
            status=status, elapsed_ms=round((time.monotonic()-self.started)*1000),
            **copy.deepcopy(evidence)))

    def optimizer(self, func, stage, *args, **kwargs):
        bound = inspect.signature(func).bind(*args, **kwargs)
        bound.apply_defaults()
        inputs = dict(bound.arguments)
        inputs.pop("abort_check", None)  # scheduling callback, not a physical model input
        self.add(stage, 'RUNNING', optimizer_inputs=inputs)
        entry = self.entries[-1]
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            entry.update(status='REJECTED', error=str(exc),
                         deficit=getattr(exc,'planner_evidence',None))
            raise
        flows = result.get('flows', [])
        summary = {'objective_pln':result.get('objective_pln'), 'slots':len(flows),
            'buy_kwh':sum(float(f.get('grid_charge_kwh') or 0) for f in flows),
            'grid_load_kwh':sum(float(f.get('grid_load_kwh') or 0) for f in flows),
            'pv_charge_kwh':sum(float(f.get('pv_to_bat_kwh') or 0) for f in flows),
            'sell_kwh':sum(float(f.get('battery_sell_kwh') or 0) for f in flows),
            'terminal_soc_pct':flows[-1].get('soc_end_pct') if flows else None}
        previous = next((e for e in reversed(self.entries[:-1]) if 'optimizer_result' in e), None)
        comparison = None
        if previous:
            before = previous['optimizer_inputs']
            comparison = {'previous_sequence':previous['sequence'],
                'changed_inputs':[k for k in inputs if inputs[k] != before.get(k)],
                'objective_delta_pln':None,
                'interpretation':'Koszt porównywalny tylko przy tych samych wejściach i wymaganiach.'}
            if not comparison['changed_inputs']:
                comparison['objective_delta_pln'] = result['objective_pln']-previous['optimizer_result']['objective_pln']
            weakened = []
            for key in ('minimum_soc_targets', 'required_soc_pcts', 'target_due_indices',
                        'hard_target_indices', 'terminal_soc_pct'):
                old, new = before.get(key), inputs.get(key)
                if old is None:
                    continue
                if new is None:
                    weakened.append(key)
                elif key in ('target_due_indices', 'hard_target_indices'):
                    if set(old)-set(new):
                        weakened.append(key)
                elif isinstance(old, (list, tuple)):
                    if len(old) != len(new) or any(b < a-1e-9 for a,b in zip(old,new)):
                        weakened.append(key)
                elif float(new) < float(old)-1e-9:
                    weakened.append(key)
            for key in ('allow_terminal_shortfall', 'allow_buy_target_shortfall'):
                if not before.get(key) and inputs.get(key):
                    weakened.append(key)
            comparison['weakened_contracts'] = weakened
        entry.update(status='OK', optimizer_result=copy.deepcopy(result),
                     summary=summary, comparison=comparison)
        return result

    def payload(self, attempt, status, result=None, error=None):
        seen = {e['stage'] for e in self.entries if e['status'] in ('OK', 'ACCEPTED')}
        missing = [s for s in EXPECTED_STAGES if s not in seen]
        return dict(schema_version=1, attempt_id=self.attempt_id,
            run_id=attempt.get('planner_run_id'), status=status,
            started_at=attempt['started_at'], run_type=attempt['run_type'],
            failed_stage=attempt.get('failed_stage') if error else None,
            error=str(error) if error else None,
            traceback=attempt.get('traceback') if error else None,
            complete=status == 'PUBLISHED' and not missing,
            missing_stages=missing, hp=attempt.get('hp_schedule'),
            result=result, stages=self.entries)


def encode_evidence(value):
    def encode(item):
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        if hasattr(item, 'isoformat'):
            return item.isoformat()
        return str(item)
    return json.dumps(value, ensure_ascii=False, default=encode)


def persist_trace(db, payload):
    with db() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO ems_gpt_planner_traces
          (attempt_id,run_id,created_at,status,trace_json)
          VALUES(%s,%s,NOW(6),%s,%s)''',
          (payload['attempt_id'], payload.get('run_id'), payload['status'],
           encode_evidence(payload)))
        cur.execute('DELETE FROM ems_gpt_planner_traces WHERE created_at<NOW(6)-INTERVAL 7 DAY')


STAGE_LABELS = {
    'PRIMARY_BUY':'Pierwsza próba: pierwotne BUY',
    'PRIMARY_DEFICIT':'Deficyt pierwotnych BUY',
    'NEUTRAL_CANDIDATES':'Druga próba: kandydaci NEUTRAL',
    'NEUTRAL_SELECTED':'Weryfikacja wybranych NEUTRAL',
    'NEUTRAL_SELECTION':'Wybrane dodatkowe zakupy',
    'PRIMARY_CONTRACT':'Weryfikacja wykonania pierwotnych BUY',
    'NEUTRAL_CONTRACT':'Weryfikacja wykonania rozszerzonego BUY',
    'OPTIONAL_SALE':'Ocena opcjonalnej sprzedaży',
    'SALE_CONTRACT':'Weryfikacja wykonania sprzedaży',
    'INPUT_COVERAGE':'Kompletność danych wejściowych',
    'HP_COMFORT':'Harmonogram CO i CWU', 'INPUTS':'Dane wejściowe',
    'RCE_RAW':'Ceny RCE', 'FORECAST':'Prognozy PV i zużycia',
    'WINDOWS':'Pierwotne okna rynkowe', 'HP_ENERGY_INPUT':'Obciążenie pompy w bilansie',
    'BUY_PREPARATION':'Przygotowanie i rozszerzenie BUY',
    'WINDOW_CANDIDATES':'Kandydaci okien', 'LOAD':'Zapotrzebowanie instalacji',
    'PV':'Bilans fotowoltaiki', 'TARGET_COMMITMENT':'Zobowiązania ładowania',
    'SLOT_BALANCE':'Bilans slotów', 'DISPATCH':'Przepływy energii',
    'REPLENISHMENT':'Uzupełnienie energii', 'FLEX_SURPLUS':'Nadwyżki PV',
    'SOC':'Trajektoria SOC', 'VALIDATE':'Walidacja publikacji'}


def explain_entry(entry):
    if entry.get('error'):
        return 'Odrzucono: '+entry['error']
    summary = entry.get('summary')
    if summary:
        text = (f"Koszt celu {summary['objective_pln']:.3f} zł; zakup do baterii "
                f"{summary['buy_kwh']:.3f} kWh; zasilanie odbiorów z sieci "
                f"{summary['grid_load_kwh']:.3f} kWh; PV do baterii "
                f"{summary['pv_charge_kwh']:.3f} kWh; końcowy SOC {summary['terminal_soc_pct']}%.")
        comparison = entry.get('comparison') or {}
        if comparison.get('weakened_contracts'):
            text += ' Utracono ograniczenia: '+', '.join(comparison['weakened_contracts'])+'.'
        if comparison.get('changed_inputs'):
            text += ' Zmieniono wejścia: '+', '.join(comparison['changed_inputs'])+'.'
        return text
    if entry['stage'] == 'INPUT_COVERAGE':
        return 'Wykryte braki danych: '+str(entry.get('issues') or 'brak')
    if entry['stage'] == 'HP_COMFORT':
        details = entry.get('details') or {}
        return ('Harmonogram zapisany niezależnie od planu energii; CWU dostępne. '
                f"Sloty ogrzewania: {details.get('heat_slots')}; niedobory: {details.get('shortfalls', [])}."
                if details.get('status') == 'COMMITTED' else
                'Odświeżenie harmonogramu HP nieudane: '+str(details.get('error')))
    return str(entry.get('details') or ('Etap rozpoczęty.' if entry['status'] == 'RUNNING'
                else 'Etap zakończony; szczegóły w dowodach.'))


def diagnostic_rows(payload):
    """Flat Polish table, with full inputs/flows available in each evidence cell."""
    return [dict(attempt_id=payload['attempt_id'], run_id=payload.get('run_id'),
        started_at=payload.get('started_at'), outcome=payload['status'],
        complete='TAK' if payload['complete'] else 'NIE',
        missing_stages=payload['missing_stages'], sequence=e['sequence'],
        stage=STAGE_LABELS.get(e['stage'], e['stage']), status=e['status'],
        explanation=explain_entry(e), elapsed_ms=e['elapsed_ms'],
        evidence=encode_evidence({k:v for k,v in e.items() if k not in
            ('sequence','stage','status','elapsed_ms')}))
        for e in payload['stages']]
