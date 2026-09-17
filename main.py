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

SUPABASE_URL = "https://iiomsxmqefxizdrclvxh.supabase.co"
SUPABASE_KEY = "sb_publishable_gU7PTU3YxS5CmgyvPNyNng_dKeahHum"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def fetch_ndc_metadata(ndc: str, drug_name: str, cache: dict):
    if ndc in cache:
        return cache[ndc]
        
    meta = {
        "rxcui": "Unknown", 
        "brand_vs_generic": "Generic", 
        "therapeutic_class": "Unclassified"
    }
    
    try:
        rxcui_resp = requests.get(f"https://rxnav.nlm.nih.gov/REST/rxcui.json?idtype=NDC&id={ndc}", timeout=2)
        if rxcui_resp.status_code == 200:
            data = rxcui_resp.json()
            if "rxnormId" in data.get("idGroup", {}):
                meta["rxcui"] = data["idGroup"]["rxnormId"][0]
                
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
        ndc_memory_cache = {}
        
        for index, row in df.iterrows():
            claim = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
            error_reasons = []
            
            # Capture Plan Identifier (defaults to vendor code + '_COMM' if missing)
            pbm_vendor = str(claim.get('pbm_vendor_id') or 'CVS').upper()
            plan_code = str(claim.get('pbm_plan_id') or claim.get('plan_code') or f"{pbm_vendor}_COMM").upper()
            claim['pbm_plan_id'] = plan_code
            claim['pbm_vendor_id'] = pbm_vendor

            raw_pharmacy_amt = claim.get('amt_paid_pharmacy')
            if raw_pharmacy_amt is None or str(raw_pharmacy_amt).strip() == '':
                error_reasons.append("Missing Pharmacy Paid Amount")
                
            ndc = str(claim.get('ndc_11') or '').replace('.0', '').strip()
            if len(ndc) != 11 or not ndc.isdigit():
                error_reasons.append(f"Invalid NDC format: {ndc}")
                
            claim['ndc_11'] = ndc
            
            # Enrichment
            metadata = fetch_ndc_metadata(ndc, claim.get('drug_name', ''), ndc_memory_cache)
            claim['rxcui'] = metadata['rxcui']
            claim['brand_vs_generic'] = metadata['brand_vs_generic']
            claim['therapeutic_class'] = metadata['therapeutic_class']
            
            # Safe casting
            try:
                pharmacy_amt = float(raw_pharmacy_amt) if raw_pharmacy_amt is not None and str(raw_pharmacy_amt).strip() != '' else 0.0
                claim['amt_paid_pharmacy'] = pharmacy_amt
            except (ValueError, TypeError):
                claim['amt_paid_pharmacy'] = None
                error_reasons.append(f"Invalid Pharmacy Paid Amount: {raw_pharmacy_amt}")
                pharmacy_amt = 0.0
                
            raw_rebate = claim.get('rebate_passed_thru')
            try:
                rebate = float(raw_rebate) if raw_rebate is not None and str(raw_rebate).strip() != '' else 0.0
                claim['rebate_passed_thru'] = rebate
            except (ValueError, TypeError):
                claim['rebate_passed_thru'] = None
                error_reasons.append(f"Invalid Rebate Amount: {raw_rebate}")
                rebate = 0.0
                
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
            "message": "Processing Complete with Plan Granularity", 
            "valid_rows_ingested": len(valid_claims), 
            "quarantined_rows": len(quarantined_claims),
            "filename": file.filename
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
