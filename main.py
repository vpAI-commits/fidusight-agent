from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import io
from supabase import create_client, Client

app = FastAPI()

# Allow your Vercel website to talk to this agent
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect to your Supabase Database
SUPABASE_URL = "https://iiomsxmqefxizdrclvxh.supabase.co"
SUPABASE_KEY = "sb_publishable_gU7PTU3YxS5CmgyvPNyNng_dKeahHum"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.post("/ingest")
async def ingest_file(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        
        # 1. Auto-detect file type
        if file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(contents))
        elif file.filename.endswith('.xlsx'):
            df = pd.read_excel(io.BytesIO(contents))
        elif file.filename.endswith('.json'):
            df = pd.read_json(io.BytesIO(contents))
        else:
            raise HTTPException(status_code=400, detail="Unsupported format.")

        # 2. Standardize column names
        df.columns = [str(c).strip().lower().replace(' ', '_') for c in df.columns]

        valid_claims = []
        quarantined_claims = []

        # 3. Row-by-Row Validation Engine
        for index, row in df.iterrows():
            # THE FIX: Convert Pandas NaN to Python None instantly so JSON doesn't crash
            claim = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
            error_reasons = []

            # Rule A: Check for missing financial data
            if claim.get('amt_paid_pharmacy') is None or str(claim.get('amt_paid_pharmacy')).strip() == '':
                error_reasons.append("Missing Pharmacy Paid Amount")
            
            # Rule B: Validate NDC-11 format
            ndc = str(claim.get('ndc_11') or '').replace('.0', '').strip()
            if len(ndc) != 11 or not ndc.isdigit():
                error_reasons.append(f"Invalid NDC format: {ndc}")
            
            claim['ndc_11'] = ndc # Clean it up for the DB

            # Execute True Net Price Math safely treating None as 0
            pharmacy_amt = float(claim.get('amt_paid_pharmacy') or 0)
            rebate = float(claim.get('rebate_passed_thru') or 0)
            claim['true_net_price'] = pharmacy_amt - rebate

            # 4. Route the data
            if error_reasons:
                claim['error_reason'] = " | ".join(error_reasons)
                claim['status'] = 'NEEDS_REVIEW'
                quarantined_claims.append(claim)
            else:
                valid_claims.append(claim)

        # 5. Push to Supabase safely
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
