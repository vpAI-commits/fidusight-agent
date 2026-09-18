from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import io
import requests
import hashlib
from datetime import datetime, timedelta
from supabase import create_client, Client

app = FastAPI(title="FiduSight CAA 2026 Intelligence Engine")

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
        if any(b in name_lower for b in ["pen", "ozempic", "humira", "stelara", "keytruda"]):
            meta["brand_vs_generic"] = "Brand"
            
        if "ozempic" in name_lower or "trulicity" in name_lower: meta["therapeutic_class"] = "Antidiabetics - GLP-1"
        elif "humira" in name_lower: meta["therapeutic_class"] = "Immunology - Anti-TNF"
        elif "stelara" in name_lower: meta["therapeutic_class"] = "Immunology - IL-12/23"
        elif "keytruda" in name_lower: meta["therapeutic_class"] = "Oncology - PD-1 Inhibitors"
        elif "januvia" in name_lower: meta["therapeutic_class"] = "Antidiabetics - DPP-4"
        elif "statin" in name_lower: meta["therapeutic_class"] = "Cardiovascular - Statins"
        elif "eliquis" in name_lower or "xarelto" in name_lower: meta["therapeutic_class"] = "Cardiovascular - Anticoagulants"
        elif "symbicort" in name_lower: meta["therapeutic_class"] = "Respiratory - ICS/LABA"
    except requests.exceptions.RequestException:
        pass
    cache[ndc] = meta
    return meta

def safe_float(val, default=0.0):
    if val is None or str(val).strip() == '':
        return default, False
    try:
        return float(val), True
    except (ValueError, TypeError):
        return default, False

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
        audit_anomalies = []
        ndc_memory_cache = {}
        now_dt = datetime.utcnow()
        statutory_deadline = (now_dt + timedelta(days=30)).isoformat()

        for idx, row in df.iterrows():
            claim = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
            error_reasons = []

            # 1. Identity & Plan Details
            pbm_vendor = str(claim.get('pbm_vendor_id') or 'CVS').upper()
            plan_code = str(claim.get('pbm_plan_id') or claim.get('plan_code') or f"{pbm_vendor}_COMM").upper()
            claim_ref = str(claim.get('claim_ref') or f"CLM-{now_dt.strftime('%Y%m')}-{idx+1001:04d}")
            claim['claim_ref'] = claim_ref
            claim['pbm_vendor_id'] = pbm_vendor
            claim['pbm_plan_id'] = plan_code

            # 2. NDC Validation
            ndc = str(claim.get('ndc_11') or '').replace('.0', '').strip()
            if len(ndc) != 11 or not ndc.isdigit():
                error_reasons.append(f"Invalid NDC format: {ndc}")
            claim['ndc_11'] = ndc

            # 3. RxNorm Enrichment
            metadata = fetch_ndc_metadata(ndc, claim.get('drug_name', ''), ndc_memory_cache)
            claim['rxcui'] = metadata['rxcui']
            claim['brand_vs_generic'] = metadata['brand_vs_generic']
            claim['therapeutic_class'] = metadata['therapeutic_class']

            # 4. Safe Numerical Conversions for Full Economic Waterfall
            wac_price, _ = safe_float(claim.get('wac_unit_price'))
            awp_price, _ = safe_float(claim.get('awp_unit_price'))
            nadac_price, _ = safe_float(claim.get('nadac_unit_price'))
            days_supply, _ = safe_float(claim.get('days_supply'), 30)
            quantity_dispensed, _ = safe_float(claim.get('quantity_dispensed'), 30)

            ing_paid, ok1 = safe_float(claim.get('ingredient_cost_paid'))
            disp_fee, ok2 = safe_float(claim.get('dispensing_fee_paid'))
            member_copay, _ = safe_float(claim.get('member_copay_paid'))
            billed_plan, ok3 = safe_float(claim.get('amt_billed_plan'))

            # Pharmacy Paid Amount = Ingredient Cost + Dispensing Fee
            pharmacy_amt, ok4 = safe_float(claim.get('amt_paid_pharmacy'))
            if not ok4:
                pharmacy_amt = ing_paid + disp_fee

            # Rebate Waterfall
            mfg_rebate, _ = safe_float(claim.get('mfg_rebate_total'))
            retained_rebate, _ = safe_float(claim.get('pbm_retained_rebate'))
            passed_rebate, ok5 = safe_float(claim.get('rebate_passed_thru'))
            if not ok5 and mfg_rebate > 0:
                passed_rebate = max(0.0, mfg_rebate - retained_rebate)

            bfsf_fee, _ = safe_float(claim.get('bfsf_admin_fee'), 3.50)

            # True Spread Amount
            if billed_plan > 0 and pharmacy_amt > 0:
                spread = max(0.0, billed_plan - pharmacy_amt)
            else:
                spread, _ = safe_float(claim.get('spread_amount'), 0.0)

            # Sanitized Assignments
            claim['awp_unit_price'] = awp_price
            claim['wac_unit_price'] = wac_price
            claim['nadac_unit_price'] = nadac_price
            claim['days_supply'] = int(days_supply)
            claim['quantity_dispensed'] = quantity_dispensed
            claim['ingredient_cost_paid'] = ing_paid
            claim['dispensing_fee_paid'] = disp_fee
            claim['member_copay_paid'] = member_copay
            claim['amt_paid_pharmacy'] = pharmacy_amt
            claim['amt_billed_plan'] = billed_plan if billed_plan > 0 else pharmacy_amt + spread
            claim['spread_amount'] = spread
            claim['mfg_rebate_total'] = mfg_rebate
            claim['pbm_retained_rebate'] = retained_rebate
            claim['rebate_passed_thru'] = passed_rebate
            claim['bfsf_admin_fee'] = bfsf_fee

            # Standard True Net Price (TNP)
            tnp = (pharmacy_amt + bfsf_fee) - passed_rebate
            claim['true_net_price'] = max(0.0, tnp)

            # Validation checks
            if not ok1 and not ok4:
                error_reasons.append("Missing Pharmacy Reimbursement / Ingredient Cost")
            if claim.get('amt_paid_pharmacy') is None:
                error_reasons.append("Invalid or Missing Pharmacy Paid Amount")

            # --- CAA 2026 STATUTORY AUDIT ENGINE ---
            if not error_reasons:
                # Violation Rule 1: Prohibited Spread Pricing
                if spread > 1.00:
                    audit_anomalies.append({
                        "claim_ref": claim_ref,
                        "pbm_plan_id": plan_code,
                        "anomaly_type": "VIOLATION_SPREAD_PRICING",
                        "severity": "CRITICAL_ERISA_BREACH",
                        "description": f"PBM retained ${spread:.2f} spread margin on claim ({pbm_vendor} billed ${claim['amt_billed_plan']:.2f}, pharmacy received ${pharmacy_amt:.2f}). Violates pass-through statutory mandate.",
                        "dollar_amount": spread,
                        "status": "OPEN",
                        "legal_statute": "ERISA §408(b)(2) / CAA 2026 Pass-Through Standard",
                        "statutory_deadline": statutory_deadline
                    })

                # Violation Rule 2: Unremitted Rebate Leakage (GPO / Aggregator Retention)
                if retained_rebate > 5.00:
                    audit_anomalies.append({
                        "claim_ref": claim_ref,
                        "pbm_plan_id": plan_code,
                        "anomaly_type": "AGGREGATOR_LEAKAGE_AUDIT",
                        "severity": "HIGH",
                        "description": f"PBM/GPO retained ${retained_rebate:.2f} of ${mfg_rebate:.2f} manufacturer rebate. CAA 2026 mandates 100% pass-through of all direct and indirect manufacturer remuneration.",
                        "dollar_amount": retained_rebate,
                        "status": "OPEN",
                        "legal_statute": "CAA 2026 §201 Direct Remittance Rule",
                        "statutory_deadline": statutory_deadline
                    })

                # Violation Rule 3: Inflated BFSF Admin Fee
                if bfsf_fee > 15.00:
                    audit_anomalies.append({
                        "claim_ref": claim_ref,
                        "pbm_plan_id": plan_code,
                        "anomaly_type": "NON_FMV_ADMIN_FEE",
                        "severity": "MEDIUM",
                        "description": f"Bona Fide Service Fee (${bfsf_fee:.2f}) exceeds fair market value safe harbor threshold ($5.00-$10.00/claim).",
                        "dollar_amount": bfsf_fee - 5.00,
                        "status": "OPEN",
                        "legal_statute": "ERISA Reasonable Compensation Test",
                        "statutory_deadline": statutory_deadline
                    })

            if error_reasons:
                claim['error_reason'] = " | ".join(error_reasons)
                claim['status'] = 'NEEDS_REVIEW'
                quarantined_claims.append(claim)
            else:
                valid_claims.append(claim)

        # Batch Operations to Supabase
        if valid_claims:
            supabase.table('claims').insert(valid_claims).execute()
        if quarantined_claims:
            supabase.table('quarantined_claims').insert(quarantined_claims).execute()
        if audit_anomalies:
            supabase.table('audit_anomalies').insert(audit_anomalies).execute()

            # Record tamper-evident SHA-256 ledger entry for fiduciary safe harbor
            summary_str = f"{file.filename}:{len(audit_anomalies)}:{now_dt.isoformat()}"
            audit_hash = hashlib.sha256(summary_str.encode()).hexdigest()
            supabase.table('audit_log').insert([{
                "action": "CAA_2026_AUTOMATED_AUDIT_DETECTED",
                "actor_role": "FiduSight Compliance Engine",
                "entity_type": "PBM_CLAIMS_BATCH",
                "details": {
                    "filename": file.filename,
                    "violations_found": len(audit_anomalies),
                    "total_fiduciary_exposure": sum(a['dollar_amount'] for a in audit_anomalies)
                },
                "hash_current": audit_hash,
                "created_at": now_dt.isoformat()
            }]).execute()

        return {
            "message": "CAA 2026 Multi-PBM Adjudication Complete", 
            "valid_claims_ingested": len(valid_claims), 
            "quarantined_claims": len(quarantined_claims),
            "statutory_violations_detected": len(audit_anomalies),
            "filename": file.filename
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
