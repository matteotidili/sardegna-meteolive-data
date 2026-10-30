#!/usr/bin/env python3
"""Scansione mirata del sud-est a budget limitato.

Riusa il circuito quota e il cursore della discovery principale.
Non effettuare mai una scansione completa da migliaia di interrogazioni.
"""
import os
from discover_wunderground import main

if __name__ == "__main__":
    os.environ["WU_DISCOVERY_REGION"] = "southeast"
    main()
