## 0.25.16

- Naprawiono pobieranie godzinowej prognozy z Home Assistant: `weather.get_forecasts` jest wywoływane z wymaganym `return_response`.
- Parametr odpowiedzi jest używany wyłącznie dla usług, które zwracają dane; wywołania skryptów wykonawcy pozostają bez zmian.
- Wykonawca pozostaje domyślnie wyłączony.

# EMS-GPT Core — changelog

# 0.25.8 — poprawny kafelek stanu aplikacji

- usunięto informację o pompie cyrkulacyjnej z górnego kafelka `Stan`;
- kafelek `Stan aplikacji` pokazuje status EMS-GPT Core, wersję i czas ostatniego heartbeat;
- odczyt stanu pompy pozostaje dostępny przez API procesu, bez eksponowania go w kafelku aplikacji.
- przy każdym uruchomieniu aplikacja przechodzi w tryb produkcyjny `LIVE`, jeżeli kompletny bezpieczny zestaw skryptów wykonawczych jest dostępny;
- operator nadal może wyłączyć wykonawcę (`OFF`) i ponownie włączyć go (`LIVE`) z kafelka;
- brak pełnego mapowania skryptów powoduje bezpieczny start `OFF` zamiast częściowego sterowania.

# 0.25.6 — uproszczenie panelu cyrkulacji

- usunięto z karty `MANUAL_CIRCULATION` dodatkową kontrolkę stanu pompy;
- rzeczywisty stan pompy przeniesiono do górnego kafelka `Stan`;
- kafelek pokazuje `DZIAŁA`, `WYŁĄCZONA` albo `BRAK DANYCH` i odświeża się co 5 sekund;
- pozostawiono przyciski procesu `Włącz / Blokuj / Auto`;
- encja `switch.sm_lite_1616r_2_pompa_cyrkulacyjna` oraz odczyt API pozostają dostępne poza kartą.

# 0.25.5 — stan cyrkulacji i poprawny status wykonawcy

- karta `MANUAL_CIRCULATION` pokazuje rzeczywisty stan przekaźnika pompy;
- wskaźnik rozróżnia `DZIAŁA`, `WYŁĄCZONA` oraz `BRAK DANYCH`;
- stan encji `switch.sm_lite_1616r_2_pompa_cyrkulacyjna` jest odświeżany co 5 sekund;
- aktywny i potwierdzony wykonawca pokazuje teraz `LIVE`, zamiast mylącego `CONNECTOR_REQUIRED`.
- naprawiono adapter usług HA: `script.turn_on` nie żąda już nieobsługiwanych danych zwrotnych, które powodowały `HTTP 400`;
- wyłączony wykonawca pokazuje teraz jednoznacznie `OFF`.

# 0.25.4 — porządek kart operatora

- usunięto kartę `HP_DHW` z panelu Procesy;
- proces `HP_DHW`, jego API, historia i skrypty „Wymuś ciepłą wodę” pozostają dostępne w backendzie;
- karta `HP_HEAT_DHW` nadal obsługuje automatyczne przełączenie `Heat+DHW` i powrót do `DHW only`.
- karta „Wykonawca” po kliknięciu udostępnia szybkie `Włącz LIVE` i `Wyłącz`;
- aktywacja LIVE wymaga potwierdzenia, kompletnego mapowania skryptów i jest zapisywana trwale bez restartu.

# 0.25.3 — właściwa polityka pompy ciepła

- Stan bazowy pompy pozostaje `DHW only`.
- `HP_HEAT_DHW` przełącza na `Heat+DHW` tylko w skonfigurowanym oknie HP, gdy minimalna prognozowana temperatura nocna 00:00–06:00 jest niższa od parametru `Nocny próg ogrzewania [°C]`.
- Brak prognozy temperatury działa fail-closed i blokuje automatyczne `Heat+DHW`.
- Po zakończeniu okna decyzja `OFF` przywraca `DHW only`.
- `HP_DHW ON/OFF` zachowuje osobną obsługę `Wymuś ciepłą wodę` i jest procesem wyłącznie na żądanie operatora.
- Executor pozostaje domyślnie w `DRY_RUN`; programy SOC 1–6 nie są modyfikowane.

# 0.25.2 — siódmy proces i komplet wykonawczy

- Dodano proces `HP_HEAT_DHW` jako niezależny od `HP_DHW`, z trwałymi decyzjami, override `AUTO/FORCE_ON/FORCE_OFF`, przebiegiem i kartą w panelu.
- `HP_HEAT_DHW` pozostaje domyślnie `ON_DEMAND`; automatyczna polityka godzinowa wymaga osobnego odbioru i nie jest aktywowana w tej wersji.
- Przygotowano mapowanie do osobnych, idempotentnych skryptów CORE `ON` i `OFF`; executor po aktualizacji nadal pozostaje w `DRY_RUN`.
- Naprawiono serializację dat w zdarzeniach override (`datetime is not JSON serializable`).
- Zachowano dynamiczne pobieranie marży zakupu z konfiguracji; `0,59 PLN/kWh` jest wyłącznie wartością domyślną.

# 0.25.1 — konfiguracja stałych operacyjnych

- Usunięto ostatnie wykonawcze zależności telemetrii od V1/V2/V3: RCE jest odczytywane z kanonicznego rekordu `ems_gpt_slots`, a moc baterii bezpośrednio z falownika Deye.
- Zachowano migrację starej opcji `legacy_helpers`; jest interpretowana jako `discharge_positive` i nie powoduje odczytu usuniętych helperów.
- Recovery SLOT/HOUR/DAILY obejmuje domyślnie 7 dni (konfigurowalne w panelu w zakresie 1–31 dni), a kolejka zamykania obsługuje do 2688 zaległych slotów.
- Brak bezpośredniego sensora baterii działa fail-closed: nie są tworzone zastępcze wartości ładowania ani rozładowania.

- Potwierdzono, że marża zakupu jest pobierana dynamicznie z ustawienia panelu; `0,59 PLN/kWh` pozostaje wyłącznie bezpieczną wartością domyślną dla nowej instalacji.
- Do panelu przeniesiono tolerancję okna zakupu, progi przepływów planowanych i technicznych oraz limity SOC floor/target.
- Konfigurowalne są okna sprzedaży oraz okna pracy HP dla dni roboczych i weekendów.
- Do panelu przeniesiono progi kompletności telemetrii, kwalifikacji do uczenia oraz progi ostrzeżeń AI Observera.
- Ujednolicono ocenę jakości SLOT, HOUR i backfillu tak, aby korzystała z jednego progu konfiguracyjnego.
- Pakiet przygotowany offline; bez publikacji, stagingu i restartu dodatku przed odbiorem RCE.

# 0.25.0 — migracja analiz, AI Observer i pełny horyzont RCE

- Rozszerzono analitykę V3 o bias PV/zużycia/importu/eksportu, błąd SOC oraz odchylenie wyniku PLN.
- Dodano AI Observer w trybie `SHADOW_READ_ONLY`: zapis wejścia, wyniku, autooceny i sugestii/TODO bez prawa zmiany planu, PPD lub urządzeń.
- Panel otrzymał osobny widok AI Observer oraz dodatkowe kolumny analityczne.
- Planer przetwarza wszystkie ciągłe przyszłe sloty z dostępnym RCE zamiast stałego limitu 96.
- Diagnostyka ocenia pełny dostępny horyzont RCE, z uwzględnieniem dób DST 92/96/100.
- Executor pozostaje wyłączony; brak zapisów do programów SOC 1–6.

# 0.24.3 — zgodność metadanych wersji

- Agregaty HOUR zapisują `source_version` wyliczany z bieżącej wersji aplikacji zamiast historycznej stałej `CORE_0_22_1`.
- Ujednolicono numer wersji runtime i manifestu lokalnego dodatku.
- Executor pozostaje wyłączony; brak zapisów do programów SOC 1–6.

# 0.24.2 — allocator PV w HOUR i DAILY

- Dodano osobne sumy PV→BAT, PV→CWU, PV→EV, PV→sieć i ograniczenia PV w HOUR.
- Dodano dobowe sumy PV→BAT, PV→CWU, PV→EV i ograniczenia PV w DAILY.
- Odbudowa po awarii wylicza pola ze SLOT bez imputacji wartości actual.
- Executor pozostaje wyłączony; brak zapisów do programów SOC 1–6.

# 0.24.1 — domknięcie audytu migracji

- Nowe rekordy wykonania procesów otrzymują kanoniczny `slot_id` już przy zapisie; nie wymagają restartowego backfillu.
- Przepływ baterii poniżej 0,050 kWh/slot jest traktowany jako techniczny i nie uruchamia obserwacji procesu importu/eksportu.
- Diagnostyka przed publikacją cen następnego dnia używa realnie dostępnego horyzontu do końca doby zamiast fałszywego wymagania 96 cen.
- Otwarte TODO diagnostyczne są deduplikowane per dzień, moduł i tytuł.
- Status AI Observer jest jawny: `DISABLED` albo `NOT_IMPLEMENTED`; sam kontrakt tabeli nie jest raportowany jako działający moduł.
- Executor pozostaje wyłączony; nie zmieniono programów SOC 1–6.

# 0.22.1 — recovery SLOT/HOUR/DAILY po awarii

- Zaległe sloty są przetwarzane chronologicznie w zakresie do 384 rekordów na cykl.
- Slot bez telemetrii po zakończeniu otrzymuje terminalny stan `MISSING_OUTAGE`; wartości actual pozostają `NULL`.
- Slot odtworzony z zachowanych próbek otrzymuje jawny stan `RECOVERED` i ocenę pokrycia.
- HOUR jest przebudowywany dla bieżącej i poprzedniej doby oraz zapisuje liczniki expected/terminal/recovered/missing, pokrycie, stan zamknięcia i bramę uczenia.
- DAILY jest przebudowywany dla obu dotkniętych dób, obsługuje 92/96/100 slotów DST, a `closed_at` jest ustawiane tylko po terminalnym zamknięciu zakończonej doby.
- Niepełne i brakujące sloty nie zasilają profili uczenia.
- Panel HOUR i DAILY pokazuje kompletność, jakość, odzyskanie, braki oraz kwalifikację do uczenia.
- Executor pozostaje wyłączony, a recovery nie steruje urządzeniami.

# 0.22.0 — kompletna, jawna macierz PPD

- Dodano trwałe, osobne flagi PPD dla sieci: `BUY_ALLOWED`, `NO_BUY` i `NEUTRAL`.
- Dodano rozdzielone decyzje sprzedaży baterii i PV: `SELL_BAT/NO_SELL_BAT` oraz `SELL_PV/NO_SELL_PV`.
- Dodano trzy wzajemnie wykluczające się stany pompy ciepła: `PREFERRED`, `NEUTRAL` i `AVOID`.
- Walidacja planu odrzuca publikację, jeśli którakolwiek grupa PPD nie jest kompletna lub jednoznaczna.
- Macierz PPD jest zapisywana zarówno w stagingu planera, jak i w opublikowanych slotach.
- Executor pozostaje wyłączony; migracja PPD nie uruchamia sterowania urządzeniami.

# 0.21.1 — konfigurowalne i rozszerzone tabele

- Rozszerzono Planer o PV1/PV2, plan pompy ciepła, okna BUY/SELL, przepływy baterii, pełne decyzje PPD i przyczynę decyzji.
- Rozszerzono tabelę godzinową o SOC, liczbę slotów i czas aktualizacji.
- Rozszerzono tabelę dobową o baterię, eksport PV, pompę ciepła, CWU/CO, czasy produkcji oraz jakość i kompletność danych.
- Dodano edytor widoczności kolumn w karcie Konfiguracja, osobny dla każdego widoku tabeli.
- Wybór kolumn jest przechowywany lokalnie w przeglądarce i można go przywrócić do wartości domyślnych.
- Brak opcjonalnej encji bezpośredniej mocy baterii nie przerywa cyklu telemetrii po restarcie HA.

# 0.21.0 — pakiet offline do walidacji etapowej

- Dodano bezpieczny wybór źródła mocy baterii: dotychczasowe helpery pozostają domyślne, a `sensor.inverter_battery_power` można włączyć dopiero po potwierdzeniu znaku.
- PPD zachowuje złożoność O(n), uwzględnia konfigurowalną wartość końcowego SOC, wagę niepewności oraz bazowy cel około 60% o 20:00.
- Dodano trwałe ręczne override `AUTO/FORCE_ON/FORCE_OFF` dla sześciu procesów, z czasem ważności, przyczyną i historią.
- Dodano kontrakt komend z TTL, idempotencją intencji, wersją planu, źródłem decyzji i śladem bezpieczeństwa.
- Dodano potwierdzenia konektora `ACCEPTED/EXECUTED/REJECTED/FAILED`; wygasła komenda nie może zostać przyjęta ani wykonana.
- Executor pozostaje domyślnie wyłączony. Tryb techniczny jest domyślnie `DRY_RUN` i nie wywołuje usług Home Assistant.
- Zamknięcie slotu zapisuje plan, efektywny stan, obserwację i energię procesu; brak poboru EV jest oznaczany jako `IDLE_OR_DISCONNECTED`, a nie automatycznie jako błąd.
- Diagnostyka kontroluje wygasłe aktywne komendy, wielokrotne override oraz bramę executora.
- Alerty diagnostyczne tworzą deduplikowane po raporcie wpisy w trwałej tabeli TODO.
- Rozbudowano API o `overrides`, `commands`, `process-execution`, `todo`, zmianę override, staging komend i potwierdzenie konektora.
- Widok Procesy otrzymał responsywne sterowanie operatora dla sześciu procesów; ręczna cyrkulacja domyślnie trwa 45 minut.
- Dodano dokument etapowego odbioru. Pakiet nie jest przeznaczony do aktywacji executora przed ukończeniem bramek.

# 0.20.0

- Wykonanie zapisuje SOC początku, minimum, końca i zmianę SOC w slocie.
- Rzeczywista temperatura pochodzi z `sensor.klimat_w_ogrodzie_temperature`.
- Moce ogrzewania, CWU i chłodzenia HeishaMon są całkowane do energii kWh na slot.
- Zapisywane są energie pobrane/użytkowe oraz COP osobno dla trybów i łącznie.
- Dodano tryb HP, licznik uruchomień i godziny pracy sprężarki.
- Tabela Wykonanie pokazuje nowe pola SOC, temperatury i efektywności pompy.

# 0.19.0

- Telemetria odczytuje niezależne moce PV1 i PV2 bezpośrednio z falownika.
- Wykonanie zapisuje rzeczywistą energię PV1/PV2 w każdym slocie.
- Dodano temperatury zasilania i powrotu HP, delta T, częstotliwość i prąd sprężarki oraz przepływ.
- Slot zapisuje jednoznaczny status pracy pompy na podstawie częstotliwości sprężarki.
- Tabela Wykonanie pokazuje nowe pola PV i pompy; pola logiczne zachowują format `TAK/NIE`.
- Analityka liczy osobne WAPE dla PV1, PV2 i produkcji łącznej.

# 0.18.0

- Kompletny import 96 cen RCE automatycznie uruchamia jeden spójny cykl zależny.
- Po RCE odświeżane są prognozy PV i pogody, uzupełniane profile zużycia i publikowany Planer/PPD.
- Cykl RCE działa również podczas blokady zwykłych przeliczeń w godzinie 14:00.
- Ręczne odświeżenie RCE używa identycznego przebiegu i zwraca wynik publikacji planu.
- Niepełny import nie publikuje planu i jawnie zwraca `WAITING_FOR_96_RCE_ROWS`.

# 0.17.1

- Odtwarzanie po restarcie uzupełnia brakujące szczegóły Wykonania z telemetrii ostatnich 24 godzin.
- Migracja jest idempotentna i nie nadpisuje istniejących rekordów szczegółowych.
- Historyczne próbki CWU zapisane w kW są normalizowane podczas odtworzenia.

# 0.17.0

- Wykonanie zapisuje osobny, trwały rekord szczegółów każdego slotu.
- Dodano rzeczywistą energię EV i CWU, całkowity eksport sieciowy, liczbę próbek i procent pokrycia slotu.
- Eksport nie jest arbitralnie dzielony na PV/baterię; do czasu danych o trybie falownika ma status `UNRESOLVED`.
- Diagnostyka kontroluje średnie pokrycie telemetrii i liczbę slotów z pokryciem poniżej 80%.
- Tabela Wykonanie pokazuje nowe przepływy oraz jakość danych.
- Czujniki mocy są normalizowane do watów; obsługiwane są źródła raportujące W, kW i MW.
- Wszystkie widoki tabel mają jawny poziomy pasek przewijania.
- Binarne pola PPD są prezentowane jako `TAK` i `NIE`, bez zmiany zwykłych wartości liczbowych 0/1.
- Nagłówek tabeli i pierwsza kolumna `Slot` pozostają zablokowane podczas przewijania.
- Druga kolumna Planera i Wykonania pokazuje trwałą rekomendację planu.

## 0.6.0 — 2026-09-09

- wdrożono aplikację Supervisor bez oznaczenia V3/V4;
- dodano własną ikonę i panel Ingress;
- utworzono odseparowaną bazę MariaDB `ems_gpt`;
- zmigrowano 50 tabel `ems_gpt_*` 1:1;
- odcięto konto aplikacji od bazy rekordera po migracji;
- przeniesiono zegar slotów, telemetrię i wznowienie po restarcie;
- przeniesiono etapowy planer oraz atomową publikację;
- przeniesiono dynamiczne SOC floor/target i polityki PPD;
- dodano bezpośredni import RCE PSE z dynamiczną marżą;
- dodano zamykanie wykonania, agregację godzinową i otwartą dobę;
- dodano profilowanie brakującego zużycia;
- wykonawca urządzeń pozostaje wyłączony do czasu wdrożenia konektora.

## Testy odbiorowe

- kompilacja Python: OK;
- instalacja i healthcheck: OK;
- MariaDB i odczyt HA: OK;
- migracja 50 tabel: OK;
- plan manualny: ACCEPTED, 54 otwarte sloty opublikowane;
- zapis wykonania: OK, slot zamknięty z 17 próbek;
- agregacja godzinowa: OK;
- agregacja dobowa: OK;
- restart wyłącznie aplikacji: OK, plan i baza zachowane;
- brak poleceń do urządzeń: potwierdzony.
# 0.16.0

- Konfigurację przeniesiono do ostatniej zakładki głównego panelu tabel.
- Parametry są grupowane w sekcje: Ceny i ekonomia, Bateria oraz Prognozy i procesy.
- Układ zakładki jest przygotowany do rozbudowy o kolejne grupy ustawień.

# 0.15.1

- Parametry operacyjne PPD usunięto z konfiguracji Supervisor po potwierdzeniu działania karty.
- Konfiguracja dodatku zawiera wyłącznie ustawienia techniczne; wartości PPD pozostają w trwałym pliku runtime.

# 0.15.0

- Panel zawiera kartę Parametry PPD z walidowanymi polami i przyciskiem Zastosuj.
- Parametry operacyjne są zapisywane trwale w `/data/runtime-settings.json`.
- Zmiany obowiązują od następnego przeliczenia bez restartu aplikacji.
- Każda aktualizacja parametrów jest zapisywana w dzienniku zdarzeń MariaDB.

# 0.14.1

- Po imporcie nowej doby RCE aplikacja natychmiast uzupełnia prognozę zużycia z profili.
- Endpoint RCE przyjmuje parametr `day=YYYY-MM-DD`, co umożliwia bezpieczne odświeżenie otwartych slotów bieżącej doby po zmianie marży.

# 0.14.0

- Harmonogram używa rzeczywistej minuty lokalnej zamiast minuty zaokrąglonego slotu.
- Naprawiono automatyczne uruchomienia planera, analityki i diagnostyki.
- Import następnej doby RCE jest ponawiany co 5 minut od 14:00 do 16:59 aż do uzyskania 96 rekordów.
- Po kompletnym imporcie RCE aplikacja odświeża Open‑Meteo przed uruchomieniem PPD.

# 0.13.0

- Pojemność, minimalny SOC, moc baterii, minimalną marżę i P80 przeniesiono do konfiguracji aplikacji.
- Progi PV→CWU i PV→EV są konfigurowalne; wartości startowe to 2,0 kW i 1,5 kW.
- CWU otrzymuje nadwyżkę po osiągnięciu dynamicznego celu SOC, bez wcześniejszego sztucznego wymogu 90%.
- EV zachowuje priorytet po CWU i rezerwę 20 punktów procentowych ponad cel SOC.

# 0.12.1

- Do konfiguracji dodano sprawność ładowania i rozładowania baterii.
- Do konfiguracji dodano koszt degradacji magazynu energii w PLN/kWh.
- PPD oraz okna ekonomiczne RCE nie zależą już od helperów V3 dla tych parametrów.

# 0.12.0

- Marża zakupu została przeniesiona do edytowalnej konfiguracji aplikacji.
- Domyślna wartość `purchase_margin_pln_kwh` wynosi 0,59 PLN/kWh.
- Import RCE nie zależy już od zerowego helpera pozostałego po V3.

# 0.11.1

- Źródło PV1/PV2 zmieniono na dedykowane encje Open‑Meteo.
- Prognoza temperatury, zachmurzenia i opadów pochodzi z godzinowej prognozy `weather.dom` (Open‑Meteo).
- Usunięto zależność prognoz aplikacji od Forecast.Solar.

# 0.11.0

- PPD zapisuje trwałą macierz sześciu procesów wraz z decyzją, przyczyną i ważnością.
- Dodano procesy BATTERY_IMPORT, BATTERY_EXPORT, PV_CWU, PV_EV, MANUAL_CIRCULATION i HP_DHW.
- Sprzedaż baterii jest wyznaczana niezależnie od starego planu V3 oraz blokowana poniżej podłogi SOC.
- Panel otrzymał widok Procesy; wszystkie decyzje pozostają niewykonywalne bez przyszłego konektora.

# 0.10.0

- Aplikacja tworzy sezonowe profile rozkładu produkcji PV z rzeczywistych wykonań.
- Dzienne prognozy Forecast.Solar PV1/PV2 są rozkładane na kwadranse z zachowaniem dokładnej sumy energii.
- Prognozy PV nie zależą już od ciągłego zapisu wykonywanego przez produkcyjny V3.

# 0.9.0

- Analityka tworzy 672 profile zużycia: dzień tygodnia × godzina × kwadrans.
- Profile przechowują średnią, średnią obciętą, P80, liczbę próbek i WAPE.
- Prognoza brakującego zużycia korzysta z profilu rzeczywistych wykonań po minimum trzech próbkach.

# 0.8.0

- Wykonanie: bezpośrednie próbkowanie dokładnej mocy ładowania i rozładowania baterii.
- Wykonanie: trwała energia ładowania/rozładowania w zamykanym slocie.
- Telemetria: dodano moc EV i CWU do danych źródłowych przyszłej analityki procesów.
- Odczyty Home Assistant są wykonywane równolegle, aby skrócić cykl próbkowania.

# 0.7.1

- Idempotentne odzyskiwanie przerwanych przebiegów planera i analityki po restarcie aplikacji.

# 0.7.0

- PPD: pełny, ciągły horyzont 96 slotów i wyłącznie ceny `PSE_API`.
- PPD: liniowy przebieg wsteczny dla przyszłych cen i ryzyka pogodowego.
- PPD: koszt odtworzenia energii uwzględnia sprawność, degradację i minimalną marżę.
- Analityka: trwałe przebiegi, jakość slotów i WAPE dla PV, zużycia, importu i eksportu.
- Diagnostyka: raporty świeżości telemetrii, kompletności planu, cen, wykonania i zablokowanych przebiegów.
- Panel: nowe widoki Analityka i Diagnostyka.
# 0.21.2

- Dodano tabelę „Przebiegi” z planem, stanem obserwowanym, energią i źródłem sterowania.
- Rozbieżność plan–obserwacja jest jawnie klasyfikowana; aktywność bez komendy CORE otrzymuje oznaczenie „ZEWNĘTRZNE / RĘCZNE”.
- Bieżący rekord dobowy bez zakończonej kontroli jakości jest pokazywany jako OPEN.
- Kolumny nowej tabeli można konfigurować w ostatniej karcie „Konfiguracja”.
# 0.23.0

- Dodano kanoniczny kalendarz slotów oparty o UTC z `slot_id`, offsetem, foldem i lokalnym indeksem doby.
- Doby Europe/Warsaw mają automatycznie 92, 96 albo 100 slotów podczas zmian DST.
- RCE zachowuje ceny ujemne, oczekuje liczby slotów właściwej dla doby i jest ponawiane od 14:00 co 10 minut.
- Usunięto pełne uruchomienie planera o północy; nowy plan powstaje po kompletnym imporcie następnej doby RCE.
- Naprawiono kwalifikację procesu BATTERY_IMPORT dla polityki `BUY_ALLOWED`.
- Migracja jest addytywna: dotychczasowy `slot_start` pozostaje kluczem zgodności do osobnego odbioru przełączenia historycznych relacji SQL.
# 0.24.0

- Rozdzielono `SOC przed`, `SOC po`, `SOC floor` i `SOC target`; target nie jest już wymuszany do wartości floor.
- Końcowy przebieg SOC jest liczony sekwencyjnie, z ciągłością między sąsiednimi slotami.
- Dodano ilościowy allocator PV→BAT→CWU→EV→sieć/ograniczenie.
- Rozszerzono relacje tabel zależnych o kanoniczny `slot_id` i bezpieczny backfill danych historycznych.
- Naprawiono formatter panelu usuwający literę T z nazw BATTERY, NEUTRAL i podobnych wartości.
# 0.25.7

- Planer i wykonawca respektują sprzętowy próg SOC aktywnego programu TOU Deye.
- Niewykonalna sprzedaż baterii jest blokowana z jawną diagnostyką bez zapisu programów SOC 1–6.
