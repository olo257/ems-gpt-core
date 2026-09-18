# EMS-GPT Core 0.25.7

## Poprawka produkcyjna

- Planer odczytuje sześć programów TOU Deye i dla każdego slotu wyznacza aktywny sprzętowy próg SOC.
- Efektywna podłoga sprzedaży nie może być niższa od minimum EMS ani SOC aktywnego programu falownika.
- Niewykonalne `SELL_BAT` jest blokowane z powodem `TOU_FLOOR_BLOCK`; brak świeżego odczytu działa fail-safe.
- Wykonawca ponownie sprawdza rzeczywisty SOC i aktywny program bezpośrednio przed eksportem baterii.
- Przy blokadzie wykonawca wysyła bezpieczne `BATTERY_EXPORT=OFF` i zapisuje zdarzenie diagnostyczne.
- Programy SOC 1–6 pozostają wyłącznie do odczytu; aplikacja ich nie modyfikuje.

## Test regresyjny

- SOC równy progowi aktywnego programu nie może uruchomić eksportu baterii.
- Zmiana programu w czasie lokalnym jest uwzględniana przy kolejnym cyklu planera i każdej próbie wykonania.
- Brak danych TOU blokuje eksport baterii, lecz nie narusza pozostałych procesów.
