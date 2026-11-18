"""Collect public local-station readings, retaining the provider's observation time."""
import json
import re
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

DOMUSNOVAS_URL = 'http://domusnovasmeteo.altervista.org/Domusmeteo_attuale.htm'
OUTPUT = Path('data/local_stations.json')


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, text):
        if not self.hidden and text.strip():
            self.parts.append(text.strip())


def parse_domusnovas(page):
    parser = PageText()
    parser.feed(page)
    text = ' '.join(' '.join(parser.parts).split())
    stamp = re.search(r'Dati aggiornati alle ore (\d{1,2}:\d{2}) del (\d{2}/\d{2}/\d{2})\b', text)
    if not stamp:
        raise ValueError('Domusnovas: missing observation time')
    observed = datetime.strptime(f'{stamp[2]} {stamp[1]}', '%d/%m/%y %H:%M').replace(tzinfo=ZoneInfo('Europe/Rome'))

    def number(label, unit=None):
        pattern = label + r'\s*([+-]?\d+(?:[.,]\d+)?)' + (r'\s*' + unit if unit else '')
        match = re.search(pattern, text, re.I)
        return float(match[1].replace(',', '.')) if match else None

    direction = re.search(r'Direzione del vento\s*([NSEWO]{1,3})\b', text, re.I)
    compass = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    direction = direction[1].upper().replace('O', 'W') if direction else None
    temperature = number(r'Temperatura', '°C')
    if temperature is None or not -60 <= temperature <= 65:
        raise ValueError('Domusnovas: missing or invalid temperature')
    return dict(
        station_id='domusnovas-meteo', name='Domusnovas Meteo',
        lat=39.3167, lon=8.65, elev=138,
        last=observed.astimezone(timezone.utc).isoformat(),
        source_url=DOMUSNOVAS_URL,
        observation_type='Domusnovas Meteo · Davis Vantage Pro2 Wireless',
        temp=temperature, tmin=number('MIN', '°C'), tmax=number('MAX', '°C'),
        rh=number(r"Umidit[aà]['’]?", '%'),
        pressure_hpa=number('Pressione', 'hPa'),
        wind_kmh=number(r"Velocit[aà]['’]? del vento", 'km/hr'),
        wind_dir=compass.index(direction) * 22.5 if direction in compass else None,
        gust_kmh=number(r'Max Vento\s*(?:[NSEWO]+\s*)?a', 'km/hr'),
        rain_today=number('Precipitazione Oggi', 'mm'),
        rain_rate=number(r"Intensit[aà]['’]? pioggia", 'mm/hr'),
        rain_48h=None,
    )


def main():
    with urllib.request.urlopen(DOMUSNOVAS_URL, timeout=40) as response:
        # The legacy Davis page uses Latin-1 (degree signs and Italian accents).
        page = response.read().decode(response.headers.get_content_charset() or 'latin-1')
    row = parse_domusnovas(page)
    payload = dict(source='Stazioni locali · Domusnovas Meteo',
                   generated_at=datetime.now(timezone.utc).isoformat(), stations=[row])
    # On fetch/parse failure the previously published dataset stays intact.
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    print('Domusnovas observation:', row['last'])


if __name__ == '__main__':
    main()
