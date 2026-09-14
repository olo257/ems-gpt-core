import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui.html").read_text(encoding="utf-8")
API = (ROOT / "api_service.py").read_text(encoding="utf-8")


class WebUiContractTests(unittest.TestCase):
    def test_rce_chart_has_48_hour_and_365_day_modes(self):
        self.assertIn("data-view='rce-chart'", HTML)
        self.assertIn("data-rce-range='48h'", HTML)
        self.assertIn("data-rce-range='365d'", HTML)
        self.assertIn("api/rce-chart?range=", HTML)
        self.assertIn("chart-window-buy", HTML)
        self.assertIn("chart-window-sell", HTML)
        self.assertIn('path.endswith("/api/rce-chart")', API)
        self.assertIn("GROUP BY DATE(slot_start) ORDER BY label", API)

    def test_every_panel_request_disables_browser_cache(self):
        self.assertIn("window.fetch=(url,options={})=>nativeFetch(url,{cache:'no-store',...options})", HTML)
        self.assertIn('self.send_header("Cache-Control", "no-store")', API)

    def test_diagnostics_are_newest_first_and_show_refresh_time(self):
        self.assertIn("ems_gpt_core_diagnostic_reports ORDER BY created_at DESC", API)
        self.assertIn("Ostatnie odświeżenie:", HTML)
        self.assertIn("document.querySelectorAll('.tabs button').forEach(b=>b.disabled=true)", HTML)

    def test_time_format_is_stable_and_warsaw_specific(self):
        self.assertIn("timeZone:'Europe/Warsaw'", HTML)
        self.assertIn("toLocaleString('sv-SE'", HTML)
        self.assertIn("text.slice(0,19)", HTML)
        self.assertIn("p[0].padStart(2,'0')+':'+p[1]+':'+(p[2]||'00')", HTML)

    def test_todo_popup_has_full_lifecycle(self):
        self.assertIn("todo:[['title','Krótki opis'],['status','Status']]", HTML)
        self.assertIn("id='todoModal'", HTML)
        self.assertIn("row.source_ref||row.module_name", HTML)
        self.assertIn("row.occurrence_count", HTML)
        self.assertIn("row.consecutive_days", HTML)
        self.assertIn("data-decision='ACCEPTED'", HTML)
        self.assertIn("data-decision='REJECTED'", HTML)
        self.assertIn("data-decision='RESOLVED'", HTML)
        self.assertIn("note:todoNote.value", HTML)
        self.assertIn("e.key==='Escape'", HTML)
        self.assertIn("if(e.target===todoModal)closeTodo()", HTML)


if __name__ == "__main__":
    unittest.main()
