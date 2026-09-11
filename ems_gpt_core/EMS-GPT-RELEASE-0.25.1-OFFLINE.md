# EMS-GPT Core 0.25.1 — pakiet poprawek offline

Data przygotowania: 2026-09-10  
Status: **GOTOWY DO ODBIORU, NIEOPUBLIKOWANY**

## Cel wydania

Usunięcie aktywnych stałych operacyjnych z logiki planera, PPD, agregacji jakości i AI Observera. Parametry są dostępne w panelu **Konfiguracja** i zapisywane w ustawieniach runtime.

## Najważniejsze ustalenie dotyczące marży

- `purchase_margin_pln_kwh` jest odczytywana przy każdym przeliczeniu z bieżącej konfiguracji.
- `0,59 PLN/kWh` w kodzie jest wyłącznie wartością domyślną dla instalacji, która nie ma jeszcze zapisanego ustawienia.
- Aktualna wartość ustawiona przez operatora nie jest nadpisywana podczas aktualizacji.

## Nowe ustawienia panelu

| Grupa | Parametry |
| --- | --- |
| Ceny i ekonomia | tolerancja okna zakupu, próg istotnego przepływu planu, próg przepływu technicznego |
| Bateria | maksymalny SOC floor, maksymalny SOC target |
| Okna czasowe | poranne i wieczorne okno sprzedaży, okna HP dla dni roboczych i weekendów |
| Jakość danych | minimalna liczba próbek slotu, minimalne pokrycie do uczenia |
| AI Observer | progi PV WAPE, Load WAPE, SOC MAE, odchylenia kosztu i minimalnej jakości |

## Spójność działania

- SLOT, backfill i HOUR korzystają z tego samego konfigurowalnego progu jakości.
- Planner i PPD korzystają z konfigurowalnych progów przepływu oraz limitów SOC.
- AI Observer pozostaje `SHADOW_READ_ONLY`; nowe progi nie nadają mu prawa do zmiany planu ani urządzeń.
- Executor pozostaje domyślnie wyłączony i w trybie dry-run.
- Brak zapisów do programów SOC 1–6.
- Telemetria nie zależy już od sensorów RCE ani mocy baterii utworzonych przez V1/V2/V3.
- Cena RCE pochodzi z kanonicznego slotu SQL, a moc baterii bezpośrednio z Deye.
- Recovery SLOT/HOUR/DAILY obejmuje domyślnie 7 dni i można je ustawić w panelu na 1–31 dni.

## Walidacja offline

- kompilacja Python: PASS,
- składnia JavaScript panelu: PASS,
- walidacja YAML: PASS,
- 25 testów kontraktowych: PASS,
- kontrola dawnych aktywnych porównań stałych: PASS.

## Stan publikacji

Nie wykonano kopiowania do `/share` ani `/addons`, przeładowania lokalnego sklepu, aktualizacji dodatku, przebudowy, restartu ani zmian w Supervisorze. Produkcyjna wersja pozostaje bez zmian.

## Procedura po zakończeniu RCE

1. Zapisać wynik bieżącego RCE i wartości ustawień użyte przez produkcyjną wersję.
2. Porównać decyzje BUY/SELL/NEUTRAL oraz PPD z oczekiwaniem operatora.
3. Dopiero po akceptacji wystawić 0.25.1 jako aktualizację do ręcznej instalacji.
4. Po instalacji sprawdzić zachowanie zapisanych ustawień, healthcheck, plan, PPD, analitykę i AI Observera.
