# EMS-GPT Core — katalog helperów i encji Home Assistant

Status: wygenerowano na podstawie bieżącego kodu wersji 0.37.5. Ten katalog
opisuje faktyczne odczyty i zapisy. Core nie tworzy żadnej z wymienionych encji.

## 1. Helpery wymagane przez Core

| Encja | Kierunek | Wykorzystanie | Kod |
|---|---|---|---|
| `input_number.ems_gpt_cena_zakupu_biezaca` | zapis | cena zakupu aktywnego slotu, 3 miejsca po przecinku | `scheduler_service.py` |
| `input_number.ems_gpt_cena_sprzedazy_biezaca` | zapis | cena sprzedaży tego samego aktywnego slotu | `scheduler_service.py` |

Obie wartości są pobierane z jednego rekordu `ems_gpt_slots`. Brak jednej ceny
blokuje oba zapisy. Zapisy do Home Assistant są wykonywane kolejno; błąd jest
raportowany jako `HA_WRITE_FAILED`, a scheduler ponawia publikację w następnym
cyklu. API Home Assistant nie zapewnia transakcji obejmującej dwa helpery.

Core nie odczytuje i nie tworzy helpera progu nocnej temperatury. Próg pompy
ciepła jest ustawieniem aplikacji `night_heating_threshold_c` zapisanym w
konfiguracji runtime.

## 2. Podstawowa telemetria — tylko odczyt

| Encja | Wykorzystanie |
|---|---|
| `sensor.inverter_battery` | bieżący SOC; planer i bramy wykonawcy |
| `sensor.inverter_pv_power` | łączna moc PV |
| `sensor.inverter_pv1_power` | moc PV1 |
| `sensor.inverter_pv2_power` | moc PV2 |
| `sensor.inverter_load_power` | moc obciążenia domu |
| `sensor.inverter_grid_power` | moc wymiany z siecią |
| `sensor.inverter_battery_power` | bezpośrednia moc baterii; znak według `direct_battery_power_mode` |
| `sensor.sonoff_1002270ef4_power` | moc ładowania EV |
| `sensor.klimat_w_ogrodzie_temperature` | rzeczywista temperatura zewnętrzna |

Próbki są zapisywane do `ems_gpt_telemetry_snapshots`. Brak części encji daje
status próbki `PARTIAL`; brak wszystkich blokuje świeżość telemetrii.

## 3. Pompa ciepła — tylko odczyt

| Encja | Wykorzystanie |
|---|---|
| `sensor.panasonic_heat_pump_main_dhw_temp` | temperatura CWU |
| `sensor.panasonic_heat_pump_main_dhw_power_consumption` | pobór mocy CWU |
| `sensor.panasonic_heat_pump_main_main_outlet_temp` | temperatura zasilania |
| `sensor.panasonic_heat_pump_main_main_inlet_temp` | temperatura powrotu |
| `sensor.panasonic_heat_pump_main_compressor_freq` | częstotliwość sprężarki i rozpoznanie pracy |
| `sensor.panasonic_heat_pump_main_compressor_current` | prąd sprężarki |
| `sensor.panasonic_heat_pump_main_pump_flow` | przepływ pompy |
| `sensor.panasonic_heat_pump_main_heat_power_consumption` | pobór mocy ogrzewania |
| `sensor.panasonic_heat_pump_main_heat_power_production` | moc cieplna ogrzewania |
| `sensor.panasonic_heat_pump_main_dhw_power_production` | moc cieplna CWU |
| `sensor.panasonic_heat_pump_main_cool_power_consumption` | pobór mocy chłodzenia |
| `sensor.panasonic_heat_pump_main_cool_power_production` | moc chłodnicza |
| `sensor.panasonic_heat_pump_main_operations_counter` | licznik uruchomień |
| `sensor.panasonic_heat_pump_main_operations_hours` | licznik godzin pracy |

Encje energii do uzgadniania i odtwarzania danych:

| Encja | Wykorzystanie |
|---|---|
| `sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_ogrzewanie_pobrana` | energia pobrana na ogrzewanie |
| `sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_ogrzewanie_wytworzona` | energia wytworzona na ogrzewanie |
| `sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_cwu_pobrana` | energia pobrana na CWU |
| `sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_cwu_wytworzona` | energia wytworzona na CWU |
| `sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_chlodzenie_pobrana` | energia pobrana na chłodzenie |
| `sensor.kotlownia_aquarea_heatpump_ems_gpt_energia_chlodzenie_wytworzona` | energia wytworzona na chłodzenie |

## 4. Prognozy — tylko odczyt

| Encja | Wykorzystanie |
|---|---|
| `sensor.open_meteo_pv1_e_energy_production_today_remaining` | pozostała energia PV1 na dziś |
| `sensor.open_meteo_pv2_w_energy_production_today_remaining` | pozostała energia PV2 na dziś |
| `sensor.open_meteo_pv1_e_energy_production_tomorrow` | energia PV1 na jutro |
| `sensor.open_meteo_pv2_w_energy_production_tomorrow` | energia PV2 na jutro |
| `weather.dom` | godzinowa prognoza temperatury, zachmurzenia i opadów przez `weather.get_forecasts` |

Do automatycznego `HP_HEAT_DHW` kwalifikuje rzeczywista temperatura z
`sensor.klimat_w_ogrodzie_temperature`: wymagane są co najmniej 3 zapisy z
okna 00:00–06:00. Prognoza z `weather.dom` nie bierze udziału w tej decyzji.

## 5. Encje falownika Deye

### Odczyt

| Wzorzec encji | Zakres | Wykorzystanie |
|---|---:|---|
| `time.inverter_program_{n}_time` | `n=1..6` | granice sześciu programów TOU |
| `number.inverter_program_{n}_soc` | `n=1..6` | progi SOC programów TOU |
| `select.inverter_program_{n}_charging` | aktywny/zmieniony program | potwierdzenie źródła ładowania |
| `switch.inverter_battery_grid_charging` | jedna encja | potwierdzenie ładowania z sieci |
| `select.inverter_work_mode` | jedna encja | potwierdzenie trybu eksportu |

### Zapis — tylko gdy executor jest jawnie włączony

| Wzorzec encji | Usługa | Wykorzystanie |
|---|---|---|
| `number.inverter_program_{n}_soc` | `number.set_value` | ustawienie targetu BUY albo floor SELL aktywnego programu; później odtworzenie wartości bazowej |
| `select.inverter_program_{n}_charging` | `select.select_option` | `Grid` dla BUY, `Disabled` po zakończeniu lub dla SELL |

Core nie zapisuje programów czasu TOU. Zapis SOC i źródła ładowania jest
chroniony flagami `executor_enabled`, `executor_dry_run` i potwierdzeniem
`EMS_CONNECTOR_ACCEPTED`.

## 6. Skrypty wykonawcze — konfigurowalne

Nazwy skryptów nie są zaszyte w kodzie. Użytkownik podaje je w
`connector_service_map_json` dla decyzji `ON` i `OFF` procesów:

- `BATTERY_IMPORT`;
- `BATTERY_EXPORT`;
- `PV_CWU`;
- `PV_EV`;
- `MANUAL_CIRCULATION`;
- `HP_HEAT_DHW`.

Każda wartość musi zaczynać się od `script.`. Core wywołuje wyłącznie
`script.turn_on`. Mapowanie zawierające nazwę programu falownika lub `soc` jest
odrzucane jako próba obejścia ochrony programów TOU.

## 7. Liczniki urządzeń — konfigurowalne, tylko odczyt

| Urządzenie | Domyślne encje | Stan domyślny |
|---|---|---|
| Zmywarka | `sensor.zmywarka_energy`, `sensor.zmywarka_power`, `switch.zmywarka` | włączone |
| Pralka | `sensor.pralka_daily_energy_consumption`, `sensor.pralnia_pralka_daily_water_consumption` | włączone |
| Suszarka | `sensor.suszarka_do_ubran_daily_energy_consumption` | włączone |
| Duża lodówka | brak — do ustawienia | wyłączone |
| Mała lodówka | brak — do ustawienia | wyłączone |
| Zamrażarka | brak — do ustawienia | wyłączone |

Dla każdego urządzenia panel pozwala ustawić encję energii, mocy, wody i
stanu. Moduł zapisuje wyniki analityczne do MariaDB, ale nie steruje tymi
encjami. `switch.zmywarka` jest więc tylko odczytywany.

## 8. Encje nieużywane i zabronione jako duplikaty

Bieżący kod nie korzysta z:

- `input_number.ems_gpt_cena_zakupu_poczatku_slotu`;
- `input_number.temperatura_nocna_pompy_ciepla`;
- `sensor.gpt_ems_cena_zakupu`;
- `sensor.gpt_ems_cena_sprzedazy`;
- `sensor.ems_gpt_rce_pse_current`;
- dawnych helperów `input_number.optymalizator_deye_*`;
- dawnych helperów mocy ładowania i rozładowania baterii.

Nie należy ich tworzyć dla EMS-GPT Core ani dodawać jako ścieżki zgodnościowe.

## 9. Podsumowanie odpowiedzialności

- Core wymaga dokładnie dwóch helperów `input_number` i oba tylko aktualizuje.
- Wszystkie sensory telemetrii, prognoz i liczników muszą istnieć wcześniej w
  Home Assistant.
- Encje Deye pochodzą z integracji falownika; zapis jest możliwy tylko w trybie
  LIVE executora.
- Encje `script.*` są lokalną konfiguracją instalacji i nie są generowane przez
  Core.
- Parametry planera, w tym próg nocnej temperatury, są ustawieniami aplikacji,
  a nie helperami Home Assistant.
