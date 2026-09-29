"""Human-readable TODO details shared by Core Observer and model worker."""
from __future__ import annotations

import json


METRIC_LABELS = {
    "export_at_nonpositive_price": "eksport przy cenie sprzedaży <= 0",
    "import_outside_buy_window": "import poza oknem BUY",
    "pv_forecast_underestimation_7d": "niedoszacowanie całej prognozy PV przez 7 dni",
    "pv1_forecast_underestimation_7d": "niedoszacowanie prognozy PV1 przez 7 dni",
    "pv2_forecast_underestimation_7d": "niedoszacowanie prognozy PV2 przez 7 dni",
    "load_forecast_underestimation_7d": "niedoszacowanie prognozy zużycia przez 7 dni",
    "heat_pump_outside_window": "plan HP poza dozwolonym oknem",
    "end_of_day_soc_below_target_range": "SOC na koniec doby poniżej oczekiwanego zakresu",
    "planned_end_soc_below_required": "planowany SOC poniżej wymaganego",
    "pv_wape_pct": "błąd prognozy PV (WAPE)",
    "load_wape_pct": "błąd prognozy zużycia (WAPE)",
    "soc_mae_pct": "błąd SOC (MAE)",
    "net_cost_variance_pln": "odchylenie wyniku finansowego",
    "quality_score": "jakość i kompletność telemetrii",
}
CHECK_LABELS = {
    "plan_vs_execution": "plan względem wykonania",
    "PV1/PV2/load_forecast": "prognozy PV1, PV2 i zużycia",
    "export_economics": "ekonomika eksportu",
    "nonpositive_export": "zakaz eksportu przy cenie <= 0",
    "buy_windows": "zakupy tylko w oknie BUY",
    "SOC_terminal": "SOC na koniec doby i wymagany target",
    "HP/CWU/EV_policy": "polityki HP, CWU i EV",
    "telemetry_quality": "jakość telemetrii",
}


def metric_label(metric: str) -> str:
    return METRIC_LABELS.get(metric, metric.replace("_", " "))


def format_observer_todo(*, scope: dict, metric: str, severity: str, error: str,
                         conclusion: str, recommendation: str, evidence,
                         run_id: str, source_ref: str, worker_id: str | None = None) -> str:
    history_days = scope.get("history_days", "—")
    completed = scope.get("completed_slots_analyzed", scope.get("completed_slots", "—"))
    future = scope.get("future_slots_analyzed", scope.get("future_slots", "—"))
    areas = scope.get("areas") or scope.get("checks") or []
    areas = [CHECK_LABELS.get(str(area), str(area)) for area in areas]
    lines = [
        "ZAKRES ANALIZY",
        f"• Okres historii: {history_days} dni",
        f"• Zamknięte sloty: {completed}",
        f"• Przyszłe sloty planu: {future}",
        f"• Sprawdzone obszary: {', '.join(map(str, areas)) if areas else 'szczegóły w danych dowodowych'}",
        "",
        "WYKRYTY PROBLEM",
        f"• Kontrola: {metric_label(metric)} ({metric})",
        f"• Ważność: {severity}",
        f"• Opis: {error}",
        "",
        "DOWODY",
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str) if evidence else "Brak szczegółowego przykładu; sprawdź metryki przebiegu Observera.",
        "",
        "WNIOSEK",
        conclusion,
        "",
        "CO SPRAWDZIĆ",
        recommendation,
        "",
        f"Przebieg Observera: {run_id}",
        f"Przebieg analityki: {source_ref}",
        "Tryb: SHADOW_READ_ONLY — raport nie zmienia planu ani sterowania.",
    ]
    if worker_id:
        lines.insert(-1, f"Worker analityczny: {worker_id}")
    return "\n".join(lines)
