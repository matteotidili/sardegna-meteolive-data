"""Palette e parametri pubblicati per ICON-2I Sardegna."""
PARAMETERS = {
    "t2m": {"source":"T_2M", "short":"2t", "label":"Temperatura a 2 m", "unit":"°C", "group":"Temperatura", "scale":"temp", "factor":1, "offset":-273.15,
        "stops":[[-15,[90,50,160]],[-5,[60,115,210]],[0,[50,185,235]],[10,[55,195,155]],[20,[240,220,55]],[30,[240,125,40]],[40,[185,35,75]]]},
    "gust10": {"source":"VMAX_10M", "short":"max_i10fg", "label":"Raffiche a 10 m", "unit":"km/h", "group":"Vento", "scale":"wind", "factor":3.6, "offset":0,
        "stops":[[0,[88,161,208]],[20,[46,191,183]],[40,[109,197,67]],[65,[241,202,52]],[95,[251,110,46]],[150,[167,42,110]]]},
    "mslp": {"source":"PMSL", "short":"pmsl", "label":"Pressione al livello del mare", "unit":"hPa", "group":"Atmosfera", "scale":"pressure", "factor":0.01, "offset":0,
        "stops":[[970,[72,68,162]],[990,[66,136,214]],[1000,[88,181,191]],[1010,[158,209,122]],[1020,[246,229,91]],[1030,[240,137,61]],[1045,[192,54,72]]]},
    "cape": {"source":"CAPE_ML", "short":"cape_ml", "label":"CAPE · energia convettiva disponibile", "unit":"J/kg", "group":"Instabilità", "scale":"cape", "factor":1, "offset":0,
        "stops":[[0,[99,141,192]],[250,[62,193,156]],[750,[205,217,67]],[1500,[247,144,43]],[2500,[230,55,48]],[4000,[155,42,141]]]},
    "cloud_total": {"source":"CLCT", "short":"clct", "label":"Nuvolosità totale", "unit":"%", "group":"Nuvolosità", "scale":"cloud", "factor":100, "offset":0,
        "stops":[[0,[121,137,151]],[20,[110,154,195]],[50,[77,137,198]],[75,[199,197,222]],[100,[247,247,255]]]},
    "rain_1h": {"source":"TOT_PREC", "short":"tp", "label":"Precipitazione nell’ora precedente", "unit":"mm", "group":"Precipitazioni", "scale":"precip", "factor":1, "offset":0, "accumulated":True,
        "stops":[[0.1,[30,190,245]],[0.5,[31,130,245]],[1,[0,190,210]],[2,[0,185,100]],[5,[240,220,25]],[10,[252,148,35]],[20,[240,49,43]],[40,[178,25,166]],[80,[255,110,244]]]},
}

BOUNDS={"west":7.5,"east":11.5,"south":37.8,"north":42.0}
STEP=0.01
