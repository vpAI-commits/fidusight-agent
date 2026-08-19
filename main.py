from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import io
import requests
from supabase import create_client, Client

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = "YOUR_SUPABASE_URL_HERE"
SUPABASE_KEY = "YOUR_SUPABASE_ANON_KEY_HERE"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- ENTERPRISE API ORCHESTRATION ---
def fetch_ndc_metadata(ndc: str, drug_name: str, cache: dict):
    """Hits the RxNorm API to enrich NDC data, with an in-memory cache to prevent throttling."""
    if ndc in cache:
        return cache[ndc]
    
    # Default baseline metadata
    meta = {
        "rxcui": "Unknown", 
        "brand_vs_generic": "Generic", # Default assumption
        "therapeutic_class": "Unclassified"
    }
    
    try:
        # 1. Fetch RxCUI from National Library of Medicine
        rxcui_resp = requests.get(f"https://rxnav.nlm.nih.gov/REST/rxcui.json?idtype=NDC&id={ndc}", timeout=2)
        if rxcui_resp.status_code == 200:
            data = rxcui_resp.json()
            if "rxnormId" in data.get("idGroup", {}):
                meta["rxcui"] = data["idGroup"]["rxnormId"][0]

        # 2. Heuristic Classification (Simulating a robust MAC list mapping)
        name_lower = str(drug_name).lower()
        if "pen" in name_lower or "ozempic" in name_lower or "humira" in name_lower:
            meta["brand_vs_generic"] = "Brand"
            
        if "ozempic" in name_lower: meta["therapeutic_class"] = "GLP-1 Agonist"
        elif "humira" in name_lower: meta["therapeutic_class"] = "Autoimmune / Biologic"
        elif "statin" in name_lower: meta["therapeutic_class"] = "Cardiovascular"
        elif "pril" in name_lower or "sartan" in name_lower: meta["therapeutic_class"] = "Cardiovascular"
        elif "cillin" in name_lower: meta["therapeutic_class"] = "Antibiotic"
        elif "thyroid" in name_lower: meta["therapeutic_class"] = "Endocrine"
        
    except requests.exceptions.RequestException:
        # Fail gracefully if the external API goes down; do not crash the ingestion pipeline
        pass 
        
    cache[ndc] = meta
    return meta

@app.post("/ingest")
async def ingest_file(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        
        if file.filename.endswith('.csv'): df = pd.read_csv(io.BytesIO(contents))
        elif file.filename.endswith('.xlsx'): df = pd.read_excel(io.BytesIO(contents))
        elif file.filename.endswith('.json'): df = pd.read_json(io.BytesIO(contents))
        else: raise HTTPException(status_code=400, detail="Unsupported format.")

        df.columns = [str(c).strip().lower().replace(' ', '_') for c in df.columns]

        valid_claims = []
        quarantined_claims = []
        ndc_memory_cache = {} # Initializes the caching layer for this file

        for index, row in df.iterrows():
            claim = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
            error_reasons = []

            if claim.get('amt_paid_pharmacy') is None or str(claim.get('amt_paid_pharmacy')).strip() == '':
                error_reasons.append("Missing Pharmacy Paid Amount")
            
            ndc = str(claim.get('ndc_11') or '').replace('.0', '').strip()
            if len(ndc) != 11 or not ndc.isdigit():
                error_reasons.append(f"Invalid NDC format: {ndc}")
            
            claim['ndc_11'] = ndc
            
            # --- TRIGGER REAL-TIME ENRICHMENT ---
            metadata = fetch_ndc_metadata(ndc, claim.get('drug_name', ''), ndc_memory_cache)
            claim['rxcui'] = metadata['rxcui']
            claim['brand_vs_generic'] = metadata['brand_vs_generic']
            claim['therapeutic_class'] = metadata['therapeutic_class']

            pharmacy_amt = float(claim.get('amt_paid_pharmacy') or 0)
            rebate = float(claim.get('rebate_passed_thru') or 0)
            claim['true_net_price'] = pharmacy_amt - rebate

            if error_reasons:
                claim['error_reason'] = " | ".join(error_reasons)
                claim['status'] = 'NEEDS_REVIEW'
                quarantined_claims.append(claim)
            else:
                valid_claims.append(claim)

        if valid_claims:
            supabase.table('claims').insert(valid_claims).execute()
        if quarantined_claims:
            supabase.table('quarantined_claims').insert(quarantined_claims).execute()

        return {
            "message": "Processing Complete", 
            "valid_rows_ingested": len(valid_claims), 
            "quarantined_rows": len(quarantined_claims),
            "filename": file.filename
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
