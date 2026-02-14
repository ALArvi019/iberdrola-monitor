#!/usr/bin/env python3
import os
from iberdrola_api import IberdrolaAPI
import json

def test_api():
    print("Testing Iberdrola API...")
    api = IberdrolaAPI()
    cupr_ids = [int(x) for x in os.getenv('CHARGER_IDS', '1234,5678').split(',')]
    
    print(f"Fetching details for {cupr_ids}...")
    result = api.obtener_detalles_cargador(cupr_ids)
    
    if result:
        print("✅ API Success!")
        print(json.dumps(result, indent=2))
    else:
        print("❌ API Failed")

if __name__ == "__main__":
    test_api()
