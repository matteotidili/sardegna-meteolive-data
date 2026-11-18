import importlib.util
from pathlib import Path
import unittest
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location('local_stations', Path(__file__).parents[1] / 'scripts/update_local_stations.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def page(date='18/11/26', temperature='20.2'):
    return f"""<html><script>Temperatura 99 °C</script><body>
    <p>Dati aggiornati alle ore <b>17:05</b> del <strong>{date}</strong></p>
    <table><tr><td>Temperatura</td><td>{temperature}&nbsp;°C</td></tr>
    <tr><td>MAX</td><td>22.9 °C alle 11:36</td></tr>
    <tr><td>MIN</td><td>18.4 °C alle 0:00</td></tr>
    <tr><td>Umidita'</td><td>81 %</td></tr>
    <tr><td>Pressione</td><td>1009.5 hPa</td></tr>
    <tr><td>Precipitazione Oggi</td><td>0.0 mm</td></tr>
    <tr><td>Velocita' del vento</td><td>20.9 km/hr</td></tr>
    <tr><td>Direzione del vento</td><td>W</td></tr>
    <tr><td>Intensita' pioggia</td><td>0.0 mm/hr</td></tr>
    <tr><td>Max Vento</td><td>a 45.1 km/hr alle 13:02</td></tr></table>
    Estremi Mensili Max Vento a 56.3 km/hr</body></html>"""


class LocalStationsTests(unittest.TestCase):
    def test_observation_units_and_daily_extremes(self):
        row = collector.parse_domusnovas(page())
        self.assertEqual(row['last'], '2026-11-18T16:05:00+00:00')
        self.assertEqual((row['temp'], row['tmin'], row['tmax']), (20.2, 18.4, 22.9))
        self.assertEqual((row['rh'], row['pressure_hpa']), (81, 1009.5))
        self.assertEqual((row['wind_kmh'], row['wind_dir'], row['gust_kmh']), (20.9, 270, 45.1))
        self.assertEqual((row['rain_today'], row['rain_rate']), (0, 0))
        self.assertIsNone(row['rain_48h'])

    def test_summer_timezone_and_original_date(self):
        self.assertEqual(collector.parse_domusnovas(page('18/07/26'))['last'], '2026-07-18T15:05:00+00:00')
        self.assertEqual(collector.parse_domusnovas(page(temperature='-2,5'))['temp'], -2.5)

    def test_missing_or_invalid_data_is_not_published(self):
        with self.assertRaises(ValueError):
            collector.parse_domusnovas(page().replace('Dati aggiornati alle ore', 'Aggiornamento'))
        with self.assertRaises(ValueError):
            collector.parse_domusnovas(page(temperature='---'))
        with self.assertRaises(ValueError):
            collector.parse_domusnovas(page(temperature='999'))


def alberti_payload():
    stamp = datetime(2026, 11, 18, 16, 30, tzinfo=timezone.utc).timestamp()
    def record(name, converted, unit, raw=None):
        return dict(sensorDataName=name, convertedValue=converted, unitLabel=unit, value=raw)
    return dict(
        lastReceived=stamp * 1000,
        currConditionValues=[record('Temp', '21,8', '°C', 71.3), record('Hum', '75,6', '%'),
                             record('Wind Speed', '22,5', 'km/h', 14), record('Wind Direction', '5760', '', 256),
                             record('Barometer', '1.009,1', 'hPa'), record('Rain Rate', '0,0', 'mm/h')],
        highLowValues=[record('High Temp', '24,1', '°C'), record('Low Temp', '19,4', '°C'),
                       record('High Wind Speed', '46,7', 'km/h'),
                       record('High Temp Time', '', '', stamp - 3600), record('Low Temp Time', '', '', stamp - 7200)],
        aggregatedValues=[dict(sensorDataName='Rain', unitLabel='mm', convertedValues=dict(DAY='0,0', MONTH='13,7'))],
    )


class AlbertiTests(unittest.TestCase):
    def test_public_summary_metric_units_and_raw_wind_direction(self):
        row = collector.parse_alberti(alberti_payload())
        self.assertEqual(row['station_id'], 'liceo-alberti-cagliari')
        self.assertEqual(row['last'], '2026-11-18T16:30:00+00:00')
        self.assertEqual((row['temp'], row['tmin'], row['tmax']), (21.8, 19.4, 24.1))
        self.assertEqual((row['wind_kmh'], row['wind_dir'], row['gust_kmh']), (22.5, 256, 46.7))
        self.assertEqual((row['rain_today'], row['rain_rate'], row['pressure_hpa']), (0, 0, 1009.1))

    def test_explicit_imperial_units_are_converted_once(self):
        payload = alberti_payload()
        payload['currConditionValues'][0].update(convertedValue='71.6', unitLabel='°F')
        payload['currConditionValues'][2].update(convertedValue='10', unitLabel='mph')
        payload['aggregatedValues'][0].update(unitLabel='in', convertedValues=dict(DAY='1.0'))
        row = collector.parse_alberti(payload)
        self.assertEqual(row['temp'], 22)
        self.assertEqual(row['wind_kmh'], 16.1)
        self.assertEqual(row['rain_today'], 25.4)

    def test_old_extremes_are_not_attached_to_a_new_observation_day(self):
        payload = alberti_payload()
        payload['highLowValues'][3]['value'] -= 86400
        self.assertIsNone(collector.parse_alberti(payload)['tmax'])

    def test_missing_timestamp_or_unavailable_public_data_is_rejected(self):
        payload = alberti_payload()
        payload['lastReceived'] = None
        with self.assertRaises(ValueError):
            collector.parse_alberti(payload)
        with self.assertRaises(ValueError):
            collector.parse_alberti(dict(noAccess=True))

    def test_one_source_failure_preserves_original_time_and_other_station(self):
        old = collector.parse_domusnovas(page())
        new = collector.parse_alberti(alberti_payload())
        def unavailable():
            raise OSError('source down')
        document = collector.collect([('domusnovas-meteo', unavailable), ('liceo-alberti-cagliari', lambda: new)], dict(stations=[old]))
        self.assertEqual(document['stations'], [old, new])
        self.assertEqual(document['errors'], {'domusnovas-meteo': 'OSError'})


if __name__ == '__main__':
    unittest.main()
