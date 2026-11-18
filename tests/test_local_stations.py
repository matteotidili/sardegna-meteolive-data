import importlib.util
from pathlib import Path
import unittest

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


if __name__ == '__main__':
    unittest.main()
