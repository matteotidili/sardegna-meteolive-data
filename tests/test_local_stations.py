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

    def test_geremeas_identity_and_console_rain_rate_unit(self):
        payload = alberti_payload()
        payload['currConditionValues'][-1].update(sensorDataTypeId=615, convertedValue='2,4', unitLabel='mm')
        row = collector.parse_alberti(payload, dict(station_id='geremeas-weatherlink', name='Geremeas', lat=39.164, lon=9.386, elev=10, source_url=collector.GEREMEAS_URL))
        self.assertEqual(row['station_id'], 'geremeas-weatherlink')
        self.assertEqual(row['rain_rate'], 2.4)
        self.assertEqual(row['source_url'], collector.GEREMEAS_URL)
        self.assertEqual(row['temp'], 21.8)

    def test_missing_extreme_time_does_not_discard_live_observation(self):
        payload = alberti_payload()
        payload['highLowValues'][3]['value'] = '--'
        payload['highLowValues'][4]['value'] = 0
        row = collector.parse_alberti(payload)
        self.assertEqual((row['temp'], row['tmin'], row['tmax']), (21.8, 19.4, 24.1))

    def test_legacy_hhmm_extreme_times_are_not_unix_dates(self):
        payload = alberti_payload()
        payload['highLowValues'][3]['value'] = 1344
        payload['highLowValues'][4]['value'] = 1
        payload['highLowValues'].append(dict(sensorDataName='High Wind Speed Time', value=344))
        row = collector.parse_alberti(payload)
        self.assertEqual((row['tmin'], row['tmax'], row['gust_kmh']), (19.4, 24.1, 46.7))

    def test_gross_erroneous_minimum_does_not_replace_valid_current_data(self):
        payload = alberti_payload()
        payload['highLowValues'][1]['convertedValue'] = '-28,5'
        row = collector.parse_alberti(payload)
        self.assertIsNone(row['tmin'])
        self.assertEqual((row['temp'], row['tmax']), (21.8, 24.1))

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


def ecowitt_share_payload():
    def record(value, unit, time='Today 18:40'):
        return dict(value=str(value), unit=unit, time=time)
    return dict(errcode='0', data={
        'temp': {'data': {'tempf': record(20.1, '℃'), 'min_daily_tempf': record(18.8, '℃'), 'max_daily_tempf': record(20.8, '℃'), 'humidity': record(89, '%')}},
        'tempin': {'data': {'tempf': record(99, '℃')}},
        'wind': {'data': {'windspeedmph': record(7.2, 'km/h'), 'winddir': record(262, 'º'), 'max_daily_windgustmph': record(51.5, 'km/h')}},
        'rain': {'data': {'dailyrainin': record(0, 'mm'), 'rainratein': record(0, 'mm/hr')}},
        'pressure': {'data': {'baromrelin': record(1014.7, 'hPa')}},
    })


class EcowittSharedStationTests(unittest.TestCase):
    received = datetime(2026, 11, 18, 17, 40, 8, tzinfo=timezone.utc)

    def test_outdoor_observation_time_units_and_extremes(self):
        row = collector.parse_iglesias_share(ecowitt_share_payload(), self.received)
        self.assertEqual(row['last'], '2026-11-18T17:40:00+00:00')
        self.assertEqual((row['temp'], row['tmin'], row['tmax']), (20.1, 18.8, 20.8))
        self.assertEqual((row['wind_kmh'], row['wind_dir'], row['gust_kmh']), (7.2, 262, 51.5))
        self.assertEqual(row['pressure_hpa'], 1014.7)
        self.assertEqual(row['location_precision'], 'street')
        self.assertNotIn('tempin', row)

    def test_yesterdays_sensor_keeps_its_date_and_omits_todays_totals(self):
        payload = ecowitt_share_payload()
        payload['data']['temp']['data']['tempf']['time'] = 'Yesterday 23:55'
        row = collector.parse_iglesias_share(payload, self.received)
        self.assertEqual(row['last'], '2026-11-17T22:55:00+00:00')
        self.assertIsNone(row['tmin'])
        self.assertIsNone(row['rain_today'])

    def test_bad_or_missing_sensor_data_is_rejected(self):
        payload = ecowitt_share_payload()
        del payload['data']['temp']['data']['tempf']['time']
        with self.assertRaises(ValueError):
            collector.parse_iglesias_share(payload, self.received)
        with self.assertRaises(ValueError):
            collector.parse_iglesias_share(dict(errcode='1'), self.received)



def oroseimeteo_payload():
    return dict(ok=True, generated_at='2026-11-18T19:00:58+01:00', stations={
        'orosei': dict(name='Orosei Centro', updated_at='2026-11-18 19:00:47',
            temperature=22.2, temp_min=20.7, temp_max=24.8, humidity=68,
            pressure=1007.4, wind_speed=24.1, wind_dir_deg=289,
            gust=24.1, gust_day=48.3, rain_day=0, rain_rate=0),
    })


class OroseiMeteoTests(unittest.TestCase):
    def test_original_sensor_time_metric_values_and_daily_gust(self):
        row = collector.parse_oroseimeteo(oroseimeteo_payload(), 'orosei')
        self.assertEqual(row['last'], '2026-11-18T18:00:47+00:00')
        self.assertEqual((row['temp'], row['tmin'], row['tmax']), (22.2, 20.7, 24.8))
        self.assertEqual((row['wind_kmh'], row['wind_dir'], row['gust_kmh']), (24.1, 289, 48.3))
        self.assertEqual((row['rain_today'], row['rain_rate']), (0, 0))
        self.assertEqual(row['elev'], 19)

    def test_summer_time_and_previous_day_do_not_invent_current_daily_extremes(self):
        p = oroseimeteo_payload()
        p['generated_at'] = '2026-07-19T00:05:00+02:00'
        p['stations']['orosei']['updated_at'] = '2026-07-18 23:59:00'
        row = collector.parse_oroseimeteo(p, 'orosei')
        self.assertEqual(row['last'], '2026-07-18T21:59:00+00:00')
        self.assertIsNone(row['tmin'])
        self.assertIsNone(row['rain_today'])

    def test_missing_observation_or_changed_identity_rejected(self):
        p = oroseimeteo_payload()
        with self.assertRaises(ValueError):
            collector.parse_oroseimeteo(p, 'aratha')
        p['stations']['orosei']['name'] = 'Another station'
        with self.assertRaises(ValueError):
            collector.parse_oroseimeteo(p, 'orosei')



class QuartuMeteoTests(unittest.TestCase):
    def test_metric_outdoor_values_daily_gust_and_original_utc_time(self):
        current = dict(timestamp='2026-11-18T18:10:14.652Z', outsideTempC=20.833333,
            insideTempC=99, outsideHumidityPct=82, barometerHpa=1009.855,
            windSpeedKmh=6.437, windDirectionDeg=255, dayRainMm=0, rainRateMmPerHour=0)
        extremes = dict(timestamp='2026-11-18T18:06:59Z', dayHighOutsideTempC=24.333,
            dayLowOutsideTempC=18.4444, dayHighWindGustKmh=41.84284)
        row = collector.parse_quartumeteo(current, extremes)
        self.assertEqual(row['last'], '2026-11-18T18:10:14.652000+00:00')
        self.assertEqual((row['temp'], row['tmin'], row['tmax']), (20.8, 18.4, 24.3))
        self.assertEqual((row['wind_kmh'], row['gust_kmh'], row['pressure_hpa']), (6.4, 41.8, 1009.9))
        self.assertNotIn('insideTempC', row)

    def test_stale_extremes_omitted_and_missing_outdoor_rejected(self):
        current = dict(timestamp='2026-11-18T23:10:00Z', outsideTempC=18)
        extremes = dict(timestamp='2026-11-18T18:00:00Z', dayHighOutsideTempC=24)
        self.assertIsNone(collector.parse_quartumeteo(current, extremes)['tmax'])
        self.assertIsNone(collector.parse_quartumeteo(current, {})['tmin'])
        with self.assertRaises(ValueError):
            collector.parse_quartumeteo(dict(timestamp=current['timestamp'], insideTempC=20), {})



if __name__ == '__main__':
    unittest.main()
