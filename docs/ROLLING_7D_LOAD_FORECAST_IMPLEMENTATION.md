# EMS-GPT Core — zbiorcza aktualizacja prognozy i wizualizacji

## Stan wdrożenia
Gałąź `feature/rolling-7d-load-forecast-batch`, PR #187. Kod roboczy, nie scalony do main.

### Zaimplementowane
- Estymator bazowego zużycia z maksymalnie 7 dni pomiarów w rozdzielczości 15 minut, mediana slotu i fallback z aktualnego tygodnia.
- Podłączenie estymatora do `materialization_service.learn_missing_load`; przy braku danych nie zastępuje prognozy zerem.
- Spójna atrybucja: DHW pozostaje w bazowym zużyciu domu, HEAT i EV są odejmowane z bazy, gdy są planowane osobno.
- Widok WWW „Prognoza zużycia”: wykres, MAE, bias, WAPE i sloty. Metryki diagnostyczne, nie sterują SOC.
- Widok „Ekonomika P”: bilans energii i przybliżony wynik zakupu/sprzedaży. Nie zastępuje pełnego rachunku.
- Usunięcie zakładki „Odrzucone plany” z nawigacji; dane audytowe i API zachowane.
- Testy estymatora oraz kontraktu atrybucji DHW/HEAT/EV.

### Ryzyka i zadania przed wydaniem
1. Zweryfikować z rzeczywistymi slotami, że `actual_heating_consumed_kwh` jest wypełniane wiarygodnie i nie zawiera DHW.
2. Potwierdzić, że uruchomienie `learn_missing_load` następuje przed przeliczeniem planu i nie nadpisuje niezaakceptowanych lub ręcznie ustawionych danych.
3. Testy bilansu 96 slotów i SOC_target w dobie ze znacznym HEAT oraz bez PV; zabezpieczyć brak zakupu poza BUY.
4. Widok ekonomiczny ma charakter uproszczony: nie uwzględnia wszystkich przepływów i strat baterii. Nie traktować wyniku PLN jako rachunku rozliczeniowego.
5. Zweryfikować dashboard w przeglądarce i na telefonie oraz testy CI po ostatnim commicie.
6. Przed scaleniem ocenić zgodność z otwartym PR #186 dotyczącym kwalifikacji nocnej HEAT.
7. Nie było siedmiodniowej obserwacji shadow nowego algorytmu; nie deklarować jej zakończenia.

### Kryterium publikacji
Jedno wydanie produkcyjne po testach, przeglądzie skutków w HA i walidacji SOC. Nie modyfikować PPD, egzekutora ani falownika bez osobnej walidacji.
