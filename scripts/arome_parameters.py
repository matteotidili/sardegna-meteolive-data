"""AROME 0,01°: prodotti derivati da SP1/SP2/SP3 per il Mediterraneo occidentale.

Mappe in WebP RGBA (palette con trasparenza); i dati non validi restano vuoti.
NON attribuire i campi diagnostici del modello a misure radar/satellite.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
import numpy as np
from PIL import Image
from eccodes import codes_grib_new_from_file, codes_release, codes_get, codes_get_values

PARAMETERS = {
    "t2m": {"label":"Temperatura 2 m", "unit":"°C", "group":"Temperature", "scale":"temp"},
    "tmaxrun": {"label":"T max 2 m dall'inizio corsa (campionamento orario)", "unit":"°C", "group":"Temperature", "scale":"temp"},
    "tminrun": {"label":"T min 2 m dall'inizio corsa (campionamento orario)", "unit":"°C", "group":"Temperature", "scale":"temp"},
    "windchill": {"label":"Temperatura percepita wind chill (quando applicabile)", "unit":"°C", "group":"Temperature", "scale":"temp"},
    "humidex": {"label":"Indice di calore Humidex (quando applicabile)", "unit":"indice", "group":"Temperature", "scale":"heat"},
    "td2m": {"label":"Punto di rugiada 2 m (calcolato)", "unit":"°C", "group":"Temperature", "scale":"temp"},
    "rh2m": {"label":"Umidità relativa 2 m", "unit":"%", "group":"Atmosfera", "scale":"humidity"},
    "wind10": {"label":"Velocità vento 10 m", "unit":"km/h", "group":"Vento", "scale":"wind"},
    "gust10": {"label":"Raffiche 10 m, stima da componenti massime", "unit":"km/h", "group":"Vento", "scale":"gust"},
    "gustmaxrun": {"label":"Raffica massima stimata dall'inizio corsa", "unit":"km/h", "group":"Vento", "scale":"gust"},
    "cape": {"label":"CAPE diagnostica (CAPE_INS)", "unit":"J/kg", "group":"Instabilità", "scale":"cape"},
    "cloud_low": {"label":"Nuvolosità bassa", "unit":"%", "group":"Nuvolosità", "scale":"cloud"},
    "cloud_mid": {"label":"Nuvolosità media", "unit":"%", "group":"Nuvolosità", "scale":"cloud"},
    "cloud_high": {"label":"Nuvolosità alta", "unit":"%", "group":"Nuvolosità", "scale":"cloud"},
    "sim_refl": {"label":"Riflettività radar SIMULATA dal modello", "unit":"dBZ (indicativa)", "group":"Instabilità", "scale":"reflectivity"},
    "rain_acc": {"label":"Precipitazioni cumulate dall'inizio corsa", "unit":"mm", "group":"Precipitazioni", "scale":"precip"},
    "snow_acc": {"label":"Neve cumulata (equivalente in acqua)", "unit":"mm eq.", "group":"Precipitazioni", "scale":"precip"},
    "graupel_acc": {"label":"Graupel cumulato (equivalente in acqua)", "unit":"mm eq.", "group":"Precipitazioni", "scale":"precip"},
    "frozen_acc": {"label":"Neve + graupel cumulati (acqua equivalente)", "unit":"mm eq.", "group":"Precipitazioni", "scale":"precip"},
    "precip_phase": {"label":"Fase prevalente delle precipitazioni (diagnostica)", "unit":"categoria", "group":"Precipitazioni", "scale":"phase"},
    "sim_ir": {"label":"Temperatura di brillanza IR simulata", "unit":"°C", "group":"Nuvolosità", "scale":"ir"}
}

# Le soglie sono esplicite, non autoadattive tra i frame.
STOPS = {
 "temp":[(-25,(80,33,124)),(-10,(59,92,195)),(0,(69,176,242)),(10,(75,201,152)),(20,(246,223,65)),(30,(242,135,45)),(40,(194,34,56)),(48,(103,22,73))],
 "heat":[(24,(56,187,126)),(29,(245,211,65)),(35,(244,125,42)),(42,(224,45,51)),(55,(124,38,123))],
 "humidity":[(0,(183,105,57)),(30,(241,194,90)),(60,(101,182,181)),(80,(62,133,214)),(100,(59,59,152))],
 "wind":[(0,(88,161,208)),(15,(46,191,183)),(30,(109,197,67)),(50,(241,202,52)),(75,(251,110,46)),(110,(167,42,110))],
 "gust":[(0,(88,161,208)),(20,(46,191,183)),(40,(109,197,67)),(65,(241,202,52)),(95,(251,110,46)),(150,(167,42,110))],
 "cape":[(0,(99,141,192)),(250,(62,193,156)),(750,(205,217,67)),(1500,(247,144,43)),(2500,(230,55,48)),(4000,(155,42,141))],
 "cloud":[(0,(121,137,151)),(20,(110,154,195)),(50,(77,137,198)),(75,(199,197,222)),(100,(247,247,255))],
 "reflectivity":[(0,(83,130,185)),(10,(46,191,194)),(20,(76,208,86)),(35,(239,216,52)),(45,(245,115,42)),(55,(221,53,81)),(70,(170,61,195))],
 "precip":[(0.1,(30,190,245)),(0.5,(31,130,245)),(1,(0,190,210)),(2,(0,185,100)),(5,(240,220,25)),(10,(252,148,35)),(20,(240,49,43)),(40,(178,25,166)),(80,(255,110,244))],
 "ir":[(-85,(235,55,49)),(-65,(254,174,48)),(-45,(245,231,110)),(-30,(157,215,227)),(-10,(81,133,191)),(5,(79,86,135)),(35,(72,81,86))]
}
PHASE_RGB={1:(30,170,231),2:(245,245,255),3:(235,120,42),4:(197,74,174)}

def unpack(blob, pack, lead, win):
    keep={
        "SP1":{"2t","2r","10u","10v","max_10efg","max_10nfg"},
        "SP2":{"CAPE_INS","lcc","mcc","hcc","tirf","tsnowp","tgrp"},
        "SP3":set()
    }
    values={}
    r0,r1,c0,c1=win
    with tempfile.TemporaryFile(mode="w+b") as f:
        f.write(blob);f.seek(0)
        while (gid:=codes_grib_new_from_file(f)) is not None:
            try:
                name=str(codes_get(gid,"shortName"))
                if pack=="SP2" and name=="unknown" and (
                        int(codes_get(gid,"parameterCategory"))==16 and
                        int(codes_get(gid,"parameterNumber"))==193):
                    name="sim_refl"
                if pack=="SP3" and name=="unknown" and (
                        int(codes_get(gid,"parameterCategory"))==5 and
                        int(codes_get(gid,"parameterNumber"))==7):
                    name="sim_ir"
                if name not in keep[pack] and name not in {"sim_refl","sim_ir"}:continue
                if name in values:raise RuntimeError(f"AROME {pack} H+{lead}: {name} duplicate")
                nx,ny=int(codes_get(gid,"Ni")),int(codes_get(gid,"Nj"))
                lon0=float(codes_get(gid,"longitudeOfFirstGridPointInDegrees"))
                lat0=float(codes_get(gid,"latitudeOfFirstGridPointInDegrees"))
                lon0=lon0-360 if lon0>180 else lon0
                if nx!=2801 or ny!=1791 or abs(lon0+12)>.001 or abs(lat0-55.4)>.001:
                    raise RuntimeError(f"Unexpected geometry {name}: {nx}x{ny} {lon0},{lat0}")
                unit=str(codes_get(gid,"units"))
                step=str(codes_get(gid,"stepType"))
                start,end=int(codes_get(gid,"startStep")),int(codes_get(gid,"endStep"))
                if end!=lead:raise RuntimeError(f"Bad lead for {name}: {end}")
                if name in {"tirf","tsnowp","tgrp"} and (start!=0 or step!="accum" or unit!="kg m**-2"):
                    raise RuntimeError(f"Bad accumulation for {name}: {start}-{end}, {unit}")
                if name in {"max_10efg","max_10nfg"} and (step!="max" or start!=lead-1):
                    raise RuntimeError(f"Bad gust period for {name}: {start}-{end}")
                grid=np.asarray(codes_get_values(gid),dtype=np.float32).reshape(ny,nx)[r0:r1,c0:c1].copy()
                grid[~np.isfinite(grid)|(grid>=9998)]=np.nan
                values[name]=grid
            finally:codes_release(gid)
    required=keep[pack]|({"sim_refl"} if pack=="SP2" else {"sim_ir"} if pack=="SP3" else set())
    if set(values)!=required:raise RuntimeError(f"Incomplete {pack} H+{lead}: missing {required-set(values)}")
    return values

def palette_image(value, key):
    v=np.asarray(value,dtype=np.float32)
    valid=np.isfinite(v)
    if key=="phase":
        i=np.nan_to_num(v,nan=0).astype(np.int8)
        rgba=np.zeros((*v.shape,4),dtype=np.uint8)
        for n,c in PHASE_RGB.items():
            rgba[i==n,:3]=c
            rgba[i==n,3]=210
        return Image.fromarray(rgba,"RGBA")
    stops=STOPS[key]
    xs=np.array([p[0] for p in stops],dtype=np.float32)
    palette=np.stack([np.interp(np.linspace(xs[0],xs[-1],2048),xs,
       [c[channel] for _,c in stops]).astype(np.uint8) for channel in range(3)],axis=-1)
    normalized=np.nan_to_num(v,nan=xs[0],posinf=xs[-1],neginf=xs[0])
    idx=np.clip(((normalized-xs[0])*(2047/(xs[-1]-xs[0]))).astype(np.int32),0,2047)
    rgba=np.empty((*v.shape,4),dtype=np.uint8)
    rgba[...,:3]=palette[idx]
    minimum={"heat":25,"wind":2,"gust":4,"cape":25,"cloud":10,
       "reflectivity":5,"precip":.1}.get(key,None)
    if minimum is None:mask=valid
    else:mask=valid&(v>=minimum)
    rgba[...,3]=np.where(mask,210,0).astype(np.uint8)
    return Image.fromarray(rgba,"RGBA")

class Renderer:
    def __init__(self,out):
        self.root=Path(out)/"products"
        self.old={}
        self.running_max=None
        self.running_min=None
        self.running_gust=None
        self.frames=0
        self.bytes=0

    def write(self, name, value, lead):
        target=self.root/name/f"h{lead:02d}.webp"
        target.parent.mkdir(parents=True,exist_ok=True)
        palette_image(value,PARAMETERS[name]["scale"]).save(target,"WEBP",quality=62,method=2,exact=True)
        self.bytes+=target.stat().st_size
        self.frames+=1

    def frame(self,lead,sp1,sp2,sp3,previous_sp2):
        required={"2t","2r","10u","10v","max_10efg","max_10nfg"}
        if set(sp1)!=required:raise RuntimeError("SP1 incomplete")
        temp=sp1["2t"]-273.15
        rh=np.clip(sp1["2r"],1,100)
        u,v=sp1["10u"],sp1["10v"]
        wind=np.hypot(u,v)*3.6
        gust=np.hypot(sp1["max_10efg"],sp1["max_10nfg"])*3.6
        self.running_max=np.fmax(self.running_max,temp) if self.running_max is not None else temp.copy()
        self.running_min=np.fmin(self.running_min,temp) if self.running_min is not None else temp.copy()
        self.running_gust=np.fmax(self.running_gust,gust) if self.running_gust is not None else gust.copy()
        gamma=np.log(rh/100)+17.625*temp/(243.04+temp)
        dewpoint=243.04*gamma/(17.625-gamma)
        wchill=(13.12+0.6215*temp-11.37*np.power(np.maximum(wind,0.1),0.16)+
                0.3965*temp*np.power(np.maximum(wind,0.1),0.16))
        wchill=np.where((temp<=10)&(wind>4.8),wchill,np.nan)
        e=6.11*np.exp(5417.7530*(1/273.16-1/(dewpoint+273.15)))
        humidex=temp+0.5555*(e-10)
        humidex=np.where((temp>=20)&(humidex>=25),humidex,np.nan)
        cum_rain=sp2["tirf"]
        cum_snow=sp2["tsnowp"]
        cum_graupel=sp2["tgrp"]
        accum=cum_rain+cum_snow+cum_graupel
        if previous_sp2:
            pr=np.maximum(0,cum_rain-previous_sp2["tirf"])
            ps=np.maximum(0,cum_snow-previous_sp2["tsnowp"])
            pg=np.maximum(0,cum_graupel-previous_sp2["tgrp"])
        else:
            pr,ps,pg=cum_rain,cum_snow,cum_graupel
        total=pr+ps+pg
        # Categoria diagnostica dalle tre componenti orarie (non classificazione ufficiale Météo-France):
        # 1 pioggia, 2 neve, 3 graupel, 4 mista (nessuna fase >=75%).
        phase=np.where(total>=.2,4,0).astype(np.float32)
        phase=np.where((total>=.2)&(pr>=.75*total),1,phase)
        phase=np.where((total>=.2)&(ps>=.75*total),2,phase)
        phase=np.where((total>=.2)&(pg>=.75*total),3,phase)
        phase=np.where(np.isfinite(total),phase,np.nan)
        products={
            "t2m":temp,"tmaxrun":self.running_max,"tminrun":self.running_min,
            "windchill":wchill,"humidex":humidex,"td2m":dewpoint,"rh2m":sp1["2r"],
            "wind10":wind,"gust10":gust,"gustmaxrun":self.running_gust,
            "cape":sp2["CAPE_INS"],"cloud_low":sp2["lcc"],
            "cloud_mid":sp2["mcc"],"cloud_high":sp2["hcc"],
            "sim_refl":sp2["sim_refl"],"rain_acc":accum,
            "snow_acc":cum_snow,"graupel_acc":cum_graupel,
            "frozen_acc":cum_snow+cum_graupel,"precip_phase":phase,
            "sim_ir":sp3["sim_ir"]-273.15
        }
        for name,array in products.items():
            self.write(name,array,lead)
        print(f"Products H+{lead:02d}: {len(products)} WebP; total output {self.bytes/1048576:.1f} MB",flush=True)

    def manifest(self):
        # La stessa scala dei raster è pubblicata nel JSON per una legenda fedele.
        return {name:{**p,
                     "stops":STOPS.get(p["scale"],[]),
                     "phase_colors":PHASE_RGB if p["scale"]=="phase" else None}
                for name,p in PARAMETERS.items()}
