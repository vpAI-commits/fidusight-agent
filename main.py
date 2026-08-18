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
SUPABASE_URL = "YOUR_SUPABASE_URL_HERE"
SUPABASE_KEY = "YOUR_SUPABASE_ANON_KEY_HERE"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.post("/ingest")
async def ingest_file(file: UploadFile = File(...)):
    try:
        # 1. Read the file into memory
        contents = await file.read()
        
        # 2. Agent Logic: Auto-detect file type
        if file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(contents))
        elif file.filename.endswith('.xlsx'):
            df = pd.read_excel(io.BytesIO(contents))
        elif file.filename.endswith('.json'):
            df = pd.read_json(io.BytesIO(contents))
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format. Please upload CSV, XLSX, or JSON.")

        # 3. Agent Logic: Clean and standardize column names
        df.columns = [str(c).strip().lower().replace(' ', '_') for c in df.columns]

        # Ensure the True Net Price column exists (Basic math calculation)
        if 'amt_paid_pharmacy' in df.columns and 'rebate_passed_thru' in df.columns:
            df['true_net_price'] = df['amt_paid_pharmacy'] - df['rebate_passed_thru']
        else:
            df['true_net_price'] = 0

        # Fill missing values so the database doesn't crash
        df = df.fillna(0)

        # Convert the dataframe to a list of dictionaries for Supabase
        records = df.to_dict(orient="records")

        # 4. Push data to Supabase (assuming columns match your 'claims' table)
        response = supabase.table('claims').insert(records).execute()

        return {"message": "Success", "rows_ingested": len(records), "filename": file.filename}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))